"""Build and maintain a static registry of signed data packages.

    borochid-registry keygen mykey             # mykey.key (private) + mykey.pub
    borochid-registry check SRC...
    borochid-registry build SRC... -o OUT --key mykey.key

``build`` is incremental: it merges into an existing OUT tree, replacing index
entries only for the packages being published, so CI can publish one package
at a time. Archives are deterministic, so rebuilding unchanged sources yields
identical hashes. Every archive is signed. Upload OUT to any static host and
give users the public key to pin in their registry config.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

from borochid.common import images
from borochid.common.manifest import Manifest, ManifestError, shard_key_for_rule
from borochid.service.registry.trust import sha256_hex

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
# Packages are data. Refuse anything that looks like code so a stray driver
# module can never be published by accident.
_ALLOWED_SUFFIXES = {".json", ".svg", ".png", ".md", ".txt"}


def find_packages(sources: list[Path]) -> list[Path]:
    dirs = []
    for src in sources:
        if (src / "manifest.json").exists():
            dirs.append(src)
        else:
            dirs.extend(sorted(p.parent for p in src.glob("*/manifest.json")))
    return dirs


def package_files(pkg_dir: Path) -> list[Path]:
    files = []
    for f in sorted(p for p in pkg_dir.rglob("*") if p.is_file()):
        rel = f.relative_to(pkg_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if f.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise ManifestError(f"{rel}: packages are data only (allowed: {sorted(_ALLOWED_SUFFIXES)})")
        files.append(f)
    return files


def check_images(pkg_dir: Path, manifest: Manifest) -> None:
    """Every PNG in the package must be a valid device image, and every image
    the manifest names must exist, so a bad picture is caught before signing."""
    for f in package_files(pkg_dir):
        if f.suffix.lower() == ".png":
            try:
                images.check_png(f.read_bytes())
            except images.ImageError as e:
                raise ManifestError(f"{f.relative_to(pkg_dir)}: {e}") from None
    for rel in {manifest.image, *(r.image for r in manifest.match)} - {None}:
        if not (pkg_dir / rel).is_file():
            raise ManifestError(f"image {rel} is missing from the package")


def build_archive(pkg_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in package_files(pkg_dir):
            info = zipfile.ZipInfo(f.relative_to(pkg_dir).as_posix(), _ZIP_EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, f.read_bytes())
    return buf.getvalue()


def sign(data: bytes, key_path: Path) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key_path.read_text().strip()))
    return base64.b64encode(key.sign(data)).decode()


def cmd_keygen(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    raw_priv = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    raw_pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    priv, pub = Path(f"{args.name}.key"), Path(f"{args.name}.pub")
    priv.write_text(base64.b64encode(raw_priv).decode() + "\n")
    priv.chmod(0o600)
    pub.write_text(base64.b64encode(raw_pub).decode() + "\n")
    print(f"wrote {priv} (keep secret) and {pub} (users pin this in their registry config)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    ok = True
    for d in find_packages(args.sources):
        try:
            m = Manifest.load(d)
            for rule in m.match:
                shard_key_for_rule(rule)
            check_images(d, m)
            print(f"ok   {m.id} {m.version} (driver {m.driver.type})")
        except (ManifestError, OSError) as e:
            ok = False
            print(f"FAIL {d}: {e}", file=sys.stderr)
    return 0 if ok else 1


def cmd_build(args: argparse.Namespace) -> int:
    out: Path = args.output
    built: dict[str, list[dict]] = defaultdict(list)
    rebuilt_ids: set[str] = set()

    for d in find_packages(args.sources):
        m = Manifest.load(d)
        check_images(d, m)
        data = build_archive(d)
        rel = f"packages/{m.id}/{m.version}.zip"
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(data)
        (out / f"{rel}.sig").write_text(sign(data, args.key) + "\n")
        entry = {"package": m.id, "version": m.version, "url": rel, "sha256": sha256_hex(data), "sig": f"{rel}.sig"}
        for rule_json, rule in zip(m.raw["match"], m.match):
            built[shard_key_for_rule(rule)].append({"match": rule_json, **entry})
        rebuilt_ids.add(m.id)
        print(f"built {m.id} {m.version}")

    # Merge: drop stale entries for rebuilt packages from every existing shard.
    match_dir = out / "match"
    existing = {p.relative_to(match_dir).with_suffix("").as_posix() for p in match_dir.rglob("*.json")} if match_dir.exists() else set()
    for key in existing | set(built):
        path = match_dir / f"{key}.json"
        entries = json.loads(path.read_text())["entries"] if path.exists() else []
        entries = [e for e in entries if e["package"] not in rebuilt_ids] + built.get(key, [])
        if entries:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"entries": entries}, indent=1) + "\n")
        else:
            path.unlink(missing_ok=True)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="borochid-registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("keygen", help="create an ed25519 signing keypair")
    p.add_argument("name")
    p.set_defaults(func=cmd_keygen)
    p = sub.add_parser("check", help="validate package sources")
    p.add_argument("sources", nargs="+", type=Path)
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("build", help="build, sign and merge packages into a static registry tree")
    p.add_argument("sources", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, required=True)
    p.add_argument("--key", type=Path, required=True, help="ed25519 signing key (from keygen)")
    p.set_defaults(func=cmd_build)
    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
