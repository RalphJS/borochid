"""XDG-compliant locations. Every path can be overridden via environment."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def config_dir() -> Path:
    return Path(os.environ.get("BOROCHID_CONFIG_DIR") or _xdg("XDG_CONFIG_HOME", ".config") / "borochid")


def cache_dir() -> Path:
    return Path(os.environ.get("BOROCHID_CACHE_DIR") or _xdg("XDG_CACHE_HOME", ".cache") / "borochid")


def data_dir() -> Path:
    return Path(os.environ.get("BOROCHID_DATA_DIR") or _xdg("XDG_DATA_HOME", ".local/share") / "borochid")


def socket_path() -> Path:
    if p := os.environ.get("BOROCHID_SOCKET"):
        return Path(p)
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/borochid-{os.getuid()}"
    return Path(runtime) / "borochid.sock"
