"""Resolves detected devices to packages and fetches them on demand.

Registry layout (static files, any HTTPS host)::

    <base>/match/usb/046d.json              index shard, one per vid / BLE key
    <base>/packages/<id>/<version>.zip      package archive (data only)
    <base>/packages/<id>/<version>.zip.sig  detached ed25519 signature

A shard is ``{"entries": [{"match": {...}, "package": id, "version": v,
"url": relpath, "sha256": hex, "sig": relpath}, ...]}``. Missing shards
(404) mean "nothing for this vendor" and are cached like hits, so plugging
in unknown devices does not hammer the registry.

Every archive must be signed by a key pinned for the registry that served it
(see Config). The sha256 only guards against corruption: it comes from the
same server as the archive, so on its own it proves nothing about origin.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin

from borochid.common.manifest import MANIFEST_NAME, Manifest, ManifestError
from borochid.common.models import DeviceIdentity, MatchRule
from borochid.service.config import Config, RegistrySource
from borochid.service.registry import trust
from borochid.service.registry.http import HttpClient, HttpError

log = logging.getLogger(__name__)

LOCAL = "local"


@dataclass(frozen=True)
class Candidate:
    """A registry entry (or local package) that matches a device."""

    source: str  # registry name, or "local"
    package: str
    version: str
    score: int
    url: str | None = None
    sha256: str | None = None
    sig_url: str | None = None
    local_dir: Path | None = None


class ResolveError(Exception):
    pass


class Registry:
    def __init__(self, cfg: Config, http: HttpClient | None = None):
        self.cfg = cfg
        self.http = http or HttpClient()
        self._index_dir = cfg.cache_dir / "index"
        self._pkg_dir = cfg.cache_dir / "packages"
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._local: list[tuple[Manifest, Path]] = []
        self._stale_before = 0.0
        self._sources = {s.name: s for s in cfg.registries}
        self.rescan_local()

    async def aclose(self) -> None:
        await self.http.aclose()

    def invalidate_index(self) -> None:
        """Treat every cached shard as stale on next lookup (still revalidated via ETag)."""
        self._stale_before = time.time()

    # -- local packages -------------------------------------------------------

    def rescan_local(self) -> None:
        """Unsigned local packages are accepted: they are data only, and
        anything able to write this directory already runs as the user."""
        self._local = []
        root = self.cfg.local_packages_dir
        if not root.is_dir():
            return
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            try:
                self._local.append((Manifest.load(d), d))
            except (ManifestError, OSError) as e:
                log.warning("skipping local package %s: %s", d, e)
        log.info("loaded %d local package(s) from %s", len(self._local), root)

    # -- resolution ------------------------------------------------------------

    async def resolve(self, ident: DeviceIdentity) -> Candidate | None:
        """Best match across local packages, then registries in priority order.

        The first source with any match wins, which lets private registries
        (listed first) override public ones.
        """
        local = [
            Candidate(LOCAL, m.id, m.version, s, local_dir=d)
            for m, d in self._local
            if (s := max(r.score(ident) for r in m.match))
        ]
        if local:
            return max(local, key=lambda c: c.score)

        for src in self.cfg.registries:
            best: Candidate | None = None
            for key in ident.shard_keys():
                for entry in (await self._shard(src, key)).get("entries", []):
                    try:
                        score = MatchRule.from_json(entry["match"]).score(ident)
                        if score and (best is None or score > best.score):
                            best = Candidate(
                                source=src.name,
                                package=entry["package"],
                                version=entry["version"],
                                score=score,
                                url=urljoin(src.url, entry["url"]),
                                sha256=entry["sha256"],
                                sig_url=urljoin(src.url, entry["sig"]) if entry.get("sig") else None,
                            )
                    except (KeyError, ValueError):
                        continue
            if best:
                return best
        return None

    async def _shard(self, src: RegistrySource, key: str) -> dict[str, Any]:
        return await self._coalesce(f"shard:{src.name}:{key}", lambda: self._fetch_shard(src, key))

    async def _fetch_shard(self, src: RegistrySource, key: str) -> dict[str, Any]:
        path = self._index_dir / src.name / f"{key}.json"
        meta_path = path.with_suffix(".meta.json")
        meta: dict[str, Any] = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            fetched = meta.get("fetched_at", 0)
            if fetched > self._stale_before and time.time() - fetched < self.cfg.index_ttl_seconds:
                return _read_cached(path, meta)

        headers = {"If-None-Match": meta["etag"]} if meta.get("etag") else {}
        try:
            resp = await self.http.get(urljoin(src.url, f"match/{key}.json"), headers=headers)
        except HttpError as e:
            log.warning("registry %s unreachable (%s); using cached %s", src.name, e, key)
            return _read_cached(path, meta)

        path.parent.mkdir(parents=True, exist_ok=True)
        if resp.status == 304:
            meta["fetched_at"] = time.time()
        elif resp.status == 404:
            meta = {"fetched_at": time.time(), "missing": True}
            path.unlink(missing_ok=True)
        elif 200 <= resp.status < 300:
            path.write_bytes(resp.content)
            meta = {"fetched_at": time.time(), "etag": resp.headers.get("etag")}
        else:
            log.warning("registry %s returned %s for %s", src.name, resp.status, key)
            return _read_cached(path, meta)
        meta_path.write_text(json.dumps(meta))
        return _read_cached(path, meta)

    # -- fetching --------------------------------------------------------------

    async def fetch(self, cand: Candidate) -> tuple[Manifest, Path]:
        """Return the manifest and on-disk directory for a candidate, downloading if needed."""
        if cand.local_dir is not None:
            return Manifest.load(cand.local_dir), cand.local_dir
        return await self._coalesce(f"pkg:{cand.sha256}", lambda: self._download(cand))

    async def _download(self, cand: Candidate) -> tuple[Manifest, Path]:
        assert cand.url and cand.sha256
        src = self._sources[cand.source]
        # Content-addressed so a re-published version can never reuse a stale cache.
        dest = self._pkg_dir / cand.package / f"{cand.version}-{cand.sha256[:16]}"
        archive, sig_file = dest.with_name(dest.name + ".zip"), dest.with_name(dest.name + ".sig")
        if (dest / MANIFEST_NAME).exists() and archive.exists() and sig_file.exists():
            # Re-verified on every load, so removing a key from the config
            # immediately stops packages it signed.
            self._verify(archive.read_bytes(), sig_file.read_text(), cand, src)
            return Manifest.load(dest), dest

        if not cand.sig_url:
            raise trust.TrustError(f"{cand.package} {cand.version} from {cand.source} is unsigned")
        log.info("downloading %s %s from %s", cand.package, cand.version, cand.source)
        resp, sig = await self.http.get(cand.url), await self.http.get(cand.sig_url)
        if resp.status != 200 or sig.status != 200:
            raise ResolveError(f"{cand.package} {cand.version}: HTTP {resp.status}/{sig.status}")
        data = resp.content
        self._verify(data, sig.text, cand, src)

        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        _safe_extract(data, tmp)
        manifest = Manifest.load(tmp)
        if (manifest.id, manifest.version) != (cand.package, cand.version):
            shutil.rmtree(tmp)
            raise ResolveError(
                f"archive contains {manifest.id} {manifest.version}, index said {cand.package} {cand.version}"
            )
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        archive.write_bytes(data)
        sig_file.write_text(sig.text)
        return manifest, dest

    def _verify(self, data: bytes, signature: str, cand: Candidate, src: RegistrySource) -> None:
        trust.verify_digest(data, cand.sha256 or "")
        trust.verify_signature(data, signature, src.key_bytes() + trust.load_trusted_keys(self.cfg.trusted_keys_dirs))

    async def _coalesce(self, key: str, factory):
        """Share one in-flight task among concurrent callers (e.g. 10 identical devices)."""
        if fut := self._inflight.get(key):
            return await asyncio.shield(fut)
        fut = asyncio.ensure_future(factory())
        self._inflight[key] = fut
        try:
            return await fut
        finally:
            self._inflight.pop(key, None)


def _read_cached(path: Path, meta: dict[str, Any]) -> dict[str, Any]:
    if meta.get("missing") or not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("corrupt cached shard %s", path)
        return {}


def _safe_extract(data: bytes, dest: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            p = PurePosixPath(info.filename)
            if p.is_absolute() or ".." in p.parts:
                raise trust.TrustError(f"unsafe path in archive: {info.filename}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise trust.TrustError(f"symlink in archive: {info.filename}")
        dest.mkdir(parents=True)
        zf.extractall(dest)
