"""Integrity and authenticity checks for downloaded packages.

* Every archive is checked against the sha256 published in the index shard.
* Every archive also needs a detached ed25519 signature (base64,
  ``<archive>.sig``) by a key pinned for its registry, or by one in a
  trusted-keys directory (``*.pub`` files, base64 raw 32-byte public key).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class TrustError(Exception):
    pass


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_digest(data: bytes, expected: str) -> None:
    actual = sha256_hex(data)
    if actual != expected.lower():
        raise TrustError(f"sha256 mismatch: expected {expected}, got {actual}")


def load_trusted_keys(dirs: list[Path]) -> list[bytes]:
    keys = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.pub")):
            try:
                raw = base64.b64decode(f.read_text().strip(), validate=True)
            except ValueError:
                log.warning("ignoring malformed trusted key %s", f)
                continue
            if len(raw) == 32:
                keys.append(raw)
            else:
                log.warning("ignoring trusted key %s: expected 32 bytes, got %d", f, len(raw))
    return keys


def verify_signature(data: bytes, signature_b64: str, trusted_keys: list[bytes]) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise TrustError("signature verification requires the 'signing' extra (cryptography)") from None

    if not trusted_keys:
        raise TrustError("no trusted keys configured")
    try:
        signature = base64.b64decode(signature_b64.strip(), validate=True)
    except ValueError:
        raise TrustError("malformed signature") from None
    for key in trusted_keys:
        try:
            Ed25519PublicKey.from_public_bytes(key).verify(signature, data)
            return
        except InvalidSignature:
            continue
    raise TrustError("signature does not match any trusted key")
