"""Data types shared by the service, the GUI and the registry tooling."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Bus(StrEnum):
    USB = "usb"
    BLE = "ble"


class DeviceStatus(StrEnum):
    DETECTED = "detected"        # seen, registry lookup pending
    RESOLVING = "resolving"      # fetching index shard / package
    UNSUPPORTED = "unsupported"  # no registry entry matched
    NEEDS_DRIVER = "needs_driver"  # package found; its driver plugin is not installed
    BLOCKED = "blocked"          # matched, but package failed trust checks
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"  # dropped off the bus; may return in a moment
    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True)
class DeviceIdentity:
    """What a detector knows about a physical device before any package is loaded.

    ``attrs`` carries bus-specific details used for matching and for opening
    channels (sysfs path, BLE address, service UUIDs, ...).
    """

    bus: Bus
    uid: str  # stable per physical device, e.g. "usb:1-3.2" or "ble:AA:BB:..."
    vid: int | None = None
    pid: int | None = None
    name: str = ""
    serial: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    def shard_keys(self) -> list[str]:
        """Registry index shards that may contain matches for this device.

        Sharding keeps each lookup to one small file no matter how many
        packages the registry holds.
        """
        if self.bus is Bus.USB and self.vid is not None:
            return [f"{self.bus}/{self.vid:04x}"]
        if self.bus is Bus.BLE:
            keys = [f"ble/service/{u.lower()}" for u in self.attrs.get("service_uuids", [])]
            keys += [f"ble/company/{c:04x}" for c in self.attrs.get("company_ids", [])]
            return keys
        return []

    def to_json(self) -> dict[str, Any]:
        return {
            "bus": str(self.bus),
            "uid": self.uid,
            "vid": self.vid,
            "pid": self.pid,
            "name": self.name,
            "serial": self.serial,
        }


def parse_int(value: int | str) -> int:
    """Accept ints or strings like "0x046d" in manifests and index files."""
    return value if isinstance(value, int) else int(value, 0)


@dataclass(frozen=True)
class MatchRule:
    bus: Bus
    vid: int | None = None
    pid: int | None = None
    pid_range: tuple[int, int] | None = None
    service_uuid: str | None = None
    company_id: int | None = None
    name_prefix: str | None = None
    # False for ``"channel": null``: the device is recognised (e.g. a dongle
    # with no headset linked) but there is nothing to open or talk to.
    channel: bool = True
    # Picture for the models this rule matches, overriding the package's.
    image: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> MatchRule:
        pr = d.get("pid_range")
        return cls(
            bus=Bus(d["bus"]),
            vid=parse_int(d["vid"]) if "vid" in d else None,
            pid=parse_int(d["pid"]) if "pid" in d else None,
            pid_range=(parse_int(pr[0]), parse_int(pr[1])) if pr else None,
            service_uuid=d.get("service_uuid", "").lower() or None,
            company_id=parse_int(d["company_id"]) if "company_id" in d else None,
            name_prefix=d.get("name_prefix"),
            channel=d.get("channel", True) is not None,
            image=d.get("image"),
        )

    def score(self, ident: DeviceIdentity) -> int:
        """0 if the rule does not match, otherwise higher means more specific."""
        if self.bus != ident.bus:
            return 0
        score = 1
        if self.vid is not None:
            if self.vid != ident.vid:
                return 0
            score += 1
        if self.pid is not None:
            if self.pid != ident.pid:
                return 0
            score += 4
        if self.pid_range is not None:
            if ident.pid is None or not self.pid_range[0] <= ident.pid <= self.pid_range[1]:
                return 0
            score += 2
        if self.service_uuid is not None:
            if self.service_uuid not in [u.lower() for u in ident.attrs.get("service_uuids", [])]:
                return 0
            score += 3
        if self.company_id is not None:
            if self.company_id not in ident.attrs.get("company_ids", []):
                return 0
            score += 1
        if self.name_prefix is not None:
            if not ident.name.startswith(self.name_prefix):
                return 0
            score += 2
        return score
