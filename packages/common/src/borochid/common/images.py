"""Device images shipped in packages.

A package may show a picture of the device instead of the generic theme
icon. Images are package data, so they are covered by the package
signature, but they are still checked here before anyone decodes them:
PNG only, bounded in bytes and pixels. Only the header is read; nothing is
decoded, so the check is cheap and safe to run in the service.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from pathlib import Path

MAX_BYTES = 256 * 1024
# Shown at up to 192 logical pixels (the home grid); 384 covers 2x displays.
MAX_SIDE = 384
_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImageError(ValueError):
    pass


def check_png(data: bytes) -> tuple[int, int]:
    """Return (width, height) if ``data`` is an acceptable device image."""
    if len(data) > MAX_BYTES:
        raise ImageError(f"image is {len(data) // 1024} KiB, the limit is {MAX_BYTES // 1024} KiB")
    # Signature, then the IHDR chunk: length (13), type, width, height.
    if len(data) < 24 or data[:8] != _SIGNATURE or data[12:16] != b"IHDR":
        raise ImageError("image must be a PNG file")
    width, height = struct.unpack(">II", data[16:24])
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE):
        raise ImageError(f"image is {width}x{height}, the limit is {MAX_SIDE}x{MAX_SIDE}")
    return width, height


# -- image store --------------------------------------------------------------
#
# The service exports each checked image to ``<store>/<sha256>.png``; the
# summary of a device names the digest and clients read the file directly.
# Files are content-addressed, so they never change once written, and a
# reader re-checks both the digest and the header before decoding.

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def store(store_dir: Path, data: bytes) -> str:
    """Write a checked image to the store and return its digest."""
    check_png(data)
    digest = hashlib.sha256(data).hexdigest()
    path = store_dir / f"{digest}.png"
    if not path.exists():
        store_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic: readers never see a partial file
    return digest


def load(store_dir: Path, digest: str) -> bytes | None:
    """The stored image with this digest, or None if absent or not valid."""
    if not _DIGEST_RE.match(digest):
        return None
    try:
        with (store_dir / f"{digest}.png").open("rb") as f:
            data = f.read(MAX_BYTES + 1)
        check_png(data)
    except (OSError, ImageError):
        return None
    return data if hashlib.sha256(data).hexdigest() == digest else None
