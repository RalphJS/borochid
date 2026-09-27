"""Service configuration, read from ``$XDG_CONFIG_HOME/borochid/config.toml``.

Example::

    detectors = ["udev", "ble"]

    [[registries]]
    name = "official"
    url = "https://registry.example.com/v1/"
    # Publisher keys trusted to sign packages from THIS registry. Pinned
    # here, never fetched: a compromised registry cannot introduce its own key.
    keys = ["base64 raw ed25519 public key"]

    [detector.ble]
    stale_seconds = 45

Every package downloaded from a registry must be signed by one of that
registry's pinned keys, or a key in a trusted-keys directory. Packages are
data only, and the service never executes downloaded code: drivers are Python
plugins installed through the system package manager.
"""

from __future__ import annotations

import base64
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from borochid.common import paths


@dataclass
class RegistrySource:
    name: str
    url: str
    keys: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.url.endswith("/"):
            self.url += "/"
        if not self.url.startswith("https://") and not self.url.startswith("http://localhost"):
            raise ValueError(f"registry {self.name!r} must use https")
        for k in self.keys:
            if len(base64.b64decode(k, validate=True)) != 32:
                raise ValueError(f"registry {self.name!r}: keys must be base64 raw ed25519 public keys")

    def key_bytes(self) -> list[bytes]:
        return [base64.b64decode(k) for k in self.keys]


@dataclass
class Config:
    detectors: list[str] = field(default_factory=lambda: ["udev", "ble"])
    detector_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    registries: list[RegistrySource] = field(default_factory=list)
    local_packages_dir: Path = field(default_factory=lambda: paths.data_dir() / "local-packages")
    cache_dir: Path = field(default_factory=paths.cache_dir)
    data_dir: Path = field(default_factory=paths.data_dir)
    trusted_keys_dirs: list[Path] = field(
        default_factory=lambda: [paths.config_dir() / "trusted-keys", Path("/etc/borochid/trusted-keys")]
    )
    # How long an index file (including "not found") is trusted before refetching.
    index_ttl_seconds: int = 6 * 3600
    socket_path: Path = field(default_factory=paths.socket_path)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or paths.config_dir() / "config.toml"
        cfg = cls()
        if not path.exists():
            return cfg
        raw = tomllib.loads(path.read_text())
        if "detectors" in raw:
            cfg.detectors = list(raw["detectors"])
        cfg.detector_options = dict(raw.get("detector", {}))
        cfg.registries = [RegistrySource(**r) for r in raw.get("registries", [])]
        for key in ("local_packages_dir", "cache_dir", "data_dir", "socket_path"):
            if key in raw:
                setattr(cfg, key, Path(raw[key]).expanduser())
        if "trusted_keys_dirs" in raw:
            cfg.trusted_keys_dirs = [Path(p).expanduser() for p in raw["trusted_keys_dirs"]]
        cfg.index_ttl_seconds = int(raw.get("index_ttl_seconds", cfg.index_ttl_seconds))
        return cfg
