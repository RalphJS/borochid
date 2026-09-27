"""Entry-point based plugin discovery.

Built-in detectors, channels and drivers are registered in pyproject.toml the
same way system-packaged ones are, so there is a single extension mechanism.
"""

from __future__ import annotations

import importlib
import logging
from functools import cache
from importlib.metadata import EntryPoint, entry_points

log = logging.getLogger(__name__)

DETECTORS = "borochid.detectors"
CHANNELS = "borochid.channels"
DRIVERS = "borochid.drivers"


@cache
def available(group: str) -> dict[str, EntryPoint]:
    """Map plugin name -> entry point (not loaded, so optional deps stay optional)."""
    return {ep.name: ep for ep in entry_points(group=group)}


def rescan() -> None:
    """Pick up plugins installed while the service is running (e.g. a driver
    package the user just installed through PackageKit)."""
    importlib.invalidate_caches()
    available.cache_clear()


def load(group: str, name: str) -> type:
    try:
        ep = available(group)[name]
    except KeyError:
        raise LookupError(f"no {group} plugin named {name!r}") from None
    return ep.load()


def version(group: str, name: str) -> str | None:
    ep = available(group).get(name)
    return ep.dist.version if ep is not None and ep.dist is not None else None
