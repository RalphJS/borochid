"""Per-device settings that drivers persist across reconnects and restarts.

Stored as ``<data_dir>/device-settings/<package id>/<device key>.<namespace>.json``
(the driver and each host service get their own namespace). The device key
starts as the USB serial number when there is one, the detector uid
otherwise. Once the driver reads the device's own ID from it (``rekey``,
through ``Driver.identify``), the key is ``id-<that ID>``: one set of
settings however the device is connected (receiver or cable) and on
whichever port.
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


def _safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


class SettingsStore:
    def __init__(self, root: Path, package_id: str, ident: DeviceIdentity, namespace: str = "driver"):
        self.namespace = namespace
        self.path = root / "device-settings" / package_id / f"{_safe(ident.serial or ident.uid)}.{namespace}.json"

    def rekey(self, device_id: str) -> bool:
        """Keep the settings under the device's own ID from now on. The first
        time, what was kept so far moves there; after that, what is there
        wins. True when that means different settings than were loaded."""
        new = self.path.with_name(f"id-{_safe(device_id)}.{self.namespace}.json")
        if new == self.path:
            return False
        old, self.path = self.path, new
        if new.exists():
            # The device's own settings win; what this connection kept
            # before it knew the ID would never be read again.
            old.unlink(missing_ok=True)
            return True
        if old.exists():
            os.replace(old, new)  # one set of settings: no copy left behind
        return False

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

    def rekey(self, device_id: str) -> bool:
        return False
