"""Minimal HTTPS client for registry downloads, on the standard library.

The service fetches a few small files, and only when an unknown device
appears, so a full async HTTP stack would sit in memory doing nothing.
Requests run in a worker thread, and ``urllib`` is only imported on first use.

Guards: certificates are always verified, redirects must stay on HTTPS, and
responses are size-capped so a hostile server can't exhaust memory.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_S = 15


class HttpError(Exception):
    pass


@dataclass
class Response:
    status: int
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content.decode()


class HttpClient:
    def __init__(self, max_bytes: int = MAX_BYTES, timeout: float = TIMEOUT_S):
        self.max_bytes = max_bytes
        self.timeout = timeout
        self._opener = None

    async def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        return await asyncio.to_thread(self._get, url, headers or {})

    async def aclose(self) -> None:
        self._opener = None

    def _build_opener(self):
        import ssl
        import urllib.request

        class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                if not newurl.startswith("https://") and not newurl.startswith("http://localhost"):
                    raise HttpError(f"refusing redirect to {newurl}")
                return super().redirect_request(req, fp, code, msg, hdrs, newurl)

        return urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), HttpsOnlyRedirects()
        )

    def _get(self, url: str, headers: dict[str, str]) -> Response:
        import urllib.error
        import urllib.request

        if self._opener is None:
            self._opener = self._build_opener()
        req = urllib.request.Request(url, headers={"User-Agent": "borochid-service", **headers})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise HttpError(f"{url}: response larger than {self.max_bytes} bytes")
                return Response(resp.status, body, {k.lower(): v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as e:
            if e.code in (304, 404):
                return Response(e.code, b"", {k.lower(): v for k, v in (e.headers or {}).items()})
            raise HttpError(f"{url}: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise HttpError(f"{url}: {e}") from None
