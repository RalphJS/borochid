from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples" / "packages"


@pytest.fixture(scope="session")
def examples() -> Path:
    return EXAMPLES


def _png(width: int, height: int, rgba: tuple[int, int, int, int] = (40, 90, 200, 255)) -> bytes:
    """A real, decodable PNG of one colour, without an imaging library."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


@pytest.fixture(scope="session")
def png():
    return _png
