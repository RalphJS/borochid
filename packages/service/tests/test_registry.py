import asyncio
import base64
import subprocess
import sys

import pytest

from borochid.common.models import Bus, DeviceIdentity
from borochid.service.config import Config, RegistrySource
from borochid.service.registry.client import Registry
from borochid.service.registry.http import Response
from borochid.service.registry.trust import TrustError

MACROPAD = DeviceIdentity(Bus.USB, "usb:1-1", vid=0x1209, pid=0xB0C1)
UNKNOWN = DeviceIdentity(Bus.USB, "usb:1-3", vid=0xDEAD, pid=0x0001)


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "borochid.service.registry.build", *map(str, args)], cwd=cwd, capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def built(tmp_path_factory, examples):
    root = tmp_path_factory.mktemp("reg")
    assert cli("keygen", "publisher", cwd=root).returncode == 0
    assert cli("keygen", "attacker", cwd=root).returncode == 0
    assert cli("build", examples, "-o", root / "out", "--key", root / "publisher.key", cwd=root).returncode == 0
    return root


def make_registry(built, tmp_path, *, key="publisher", requests=None, tamper=None):
    out = built / "out"

    class FakeHttp:
        async def get(self, url, headers=None):
            path = "/" + url.split("://", 1)[1].split("/", 1)[1]
            if requests is not None:
                requests.append(path)
            f = out / path.removeprefix("/v1/")
            if not f.is_file():
                return Response(404)
            data = f.read_bytes()
            return Response(200, tamper(path, data) if tamper else data)

        async def aclose(self):
            pass

    pub = (built / f"{key}.pub").read_text().strip()
    cfg = Config(
        registries=[RegistrySource("test", "https://reg.example/v1", keys=[pub])],
        cache_dir=tmp_path / "cache",
        local_packages_dir=tmp_path / "none",
        trusted_keys_dirs=[],
    )
    return Registry(cfg, FakeHttp())


def test_signed_package_resolves_and_downloads(built, tmp_path):
    async def go():
        reg = make_registry(built, tmp_path)
        cand = await reg.resolve(MACROPAD)
        assert (cand.package, cand.source) == ("acme.macropad", "test")
        manifest, path = await reg.fetch(cand)
        assert manifest.id == "acme.macropad" and (path / "manifest.json").exists()
        # Cached copy is re-verified and reused.
        assert (await reg.fetch(cand))[1] == path

    asyncio.run(go())


def test_package_signed_by_unpinned_key_is_rejected(built, tmp_path):
    async def go():
        reg = make_registry(built, tmp_path, key="attacker")
        with pytest.raises(TrustError, match="signature"):
            await reg.fetch(await reg.resolve(MACROPAD))

    asyncio.run(go())


def test_consistently_tampered_archive_is_rejected(built, tmp_path):
    """A compromised registry can rewrite both archive and index hash; only the signature catches it."""
    import hashlib
    import json

    evil = b"PK-not-the-real-archive"

    def tamper(path, data):
        if path.endswith(".zip"):
            return evil
        if "/match/" in path:
            shard = json.loads(data)
            for e in shard["entries"]:
                e["sha256"] = hashlib.sha256(evil).hexdigest()
            return json.dumps(shard).encode()
        return data

    async def go():
        reg = make_registry(built, tmp_path, tamper=tamper)
        with pytest.raises(TrustError, match="signature"):
            await reg.fetch(await reg.resolve(MACROPAD))

    asyncio.run(go())


def test_unknown_vendor_is_negatively_cached(built, tmp_path):
    async def go():
        requests = []
        reg = make_registry(built, tmp_path, requests=requests)
        assert await reg.resolve(UNKNOWN) is None
        assert await reg.resolve(UNKNOWN) is None
        assert requests == ["/v1/match/usb/dead.json"]

    asyncio.run(go())


def test_concurrent_lookups_share_one_fetch(built, tmp_path):
    async def go():
        requests = []
        reg = make_registry(built, tmp_path, requests=requests)
        await asyncio.gather(*(reg.resolve(MACROPAD) for _ in range(20)))
        assert requests.count("/v1/match/usb/1209.json") == 1

    asyncio.run(go())


def test_build_refuses_code_and_requires_key(built, tmp_path, examples):
    pkg = tmp_path / "evil.pkg"
    pkg.mkdir()
    (pkg / "manifest.json").write_text((examples / "acme.macropad" / "manifest.json").read_text())
    (pkg / "driver.py").write_text("import os\n")
    result = cli("build", pkg, "-o", tmp_path / "out", "--key", built / "publisher.key", cwd=tmp_path)
    assert result.returncode != 0 and "data only" in result.stderr
    assert cli("build", examples, "-o", tmp_path / "out", cwd=tmp_path).returncode != 0


def test_build_is_deterministic(built, tmp_path, examples):
    assert cli("build", examples / "acme.macropad", "-o", tmp_path / "out", "--key", built / "publisher.key", cwd=tmp_path).returncode == 0
    assert (tmp_path / "out/packages/acme.macropad/1.0.0.zip").read_bytes() == (
        built / "out/packages/acme.macropad/1.0.0.zip"
    ).read_bytes()


def test_registry_config_rejects_insecure_sources():
    with pytest.raises(ValueError, match="https"):
        RegistrySource("x", "http://example.com/")
    with pytest.raises(ValueError):
        RegistrySource("x", "https://example.com/", keys=[base64.b64encode(b"short").decode()])


def test_http_client_refuses_https_downgrade_redirects():
    import urllib.request

    from borochid.service.registry.http import HttpClient, HttpError

    opener = HttpClient()._build_opener()
    handler = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
    req = urllib.request.Request("https://registry.example/v1/x")
    with pytest.raises(HttpError, match="refusing redirect"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/x")
