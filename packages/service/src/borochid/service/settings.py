"""Per-device settings that drivers persist across reconnects and restarts.

Stored as ``<data_dir>/device-settings/<package id>/<device key>.<namespace>.json``
(the driver and each host service get their own namespace), where
the device key is the serial number when the device reports one (so settings
follow the device between ports) and the detector uid otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from borochid.common.models import DeviceIdentity

log = logging.getLogger(__name__)


class SettingsStore:
    def __init__(self, root: Path, package_id: str, ident: DeviceIdentity, namespace: str = "driver"):
        key = re.sub(r"[^A-Za-z0-9._-]", "_", ident.serial or ident.uid)
        self.path = root / "device-settings" / package_id / f"{key}.{namespace}.json"

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            log.warning("ignoring unreadable settings %s: %s", self.path, e)
            return {}

    def save(self, settings: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=1, sort_keys=True))
        os.replace(tmp, self.path)


class MemoryStore:
    """Non-persistent store, the default when a driver is built without one."""

    def __init__(self, initial: dict[str, Any] | None = None):
        self.data = dict(initial or {})

    def load(self) -> dict[str, Any]:
        return dict(self.data)

    def save(self, settings: dict[str, Any]) -> None:
        self.data = dict(settings)
