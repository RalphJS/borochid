"""Device package manifest (``manifest.json`` at the package root).

Packages are data only: nothing in a package is ever executed. Behaviour
comes from a driver, which is either built into the service (``declarative``)
or a plugin installed through the system package manager. A package names the
driver it needs and which system package provides it::

    "driver": {
      "type": "corsair-v2w",
      "version": ">=0.1,<1",
      "provided_by": "borochid-driver-corsair-v2w"
    }

When the driver is missing, the service reports ``needs_driver`` and the GUI
offers to install ``provided_by`` through PackageKit, from signed
repositories only.
"""

from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from borochid.common.models import Bus, DeviceIdentity, MatchRule

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)([.-][0-9A-Za-z.-]+)?$")
_SPEC_RE = re.compile(r"^(>=|<=|==|>|<)\s*(\d+(?:\.\d+){0,2})$")
# Packages a manifest may ask the user to install. Constrained so a manifest
# can never steer PackageKit toward arbitrary system packages.
DRIVER_PACKAGE_RE = re.compile(r"^borochid-driver-[a-z0-9][a-z0-9-]*$")
# What kind of device a package describes; the GUI picks the device's icon
# from the desktop icon theme by category, so packages never carry images.
# Unknown values read as "other", so a newer package still loads here.
CATEGORIES = frozenset(
    {"headset", "headphones", "speaker", "microphone", "keyboard", "keypad", "mouse", "gamepad", "tablet", "webcam", "other"}
)


class ManifestError(ValueError):
    pass


def version_key(version: str) -> tuple[int, int, int]:
    m = _VERSION_RE.match(version)
    if not m:
        raise ManifestError(f"invalid version {version!r}")
    return int(m[1]), int(m[2]), int(m[3])


@dataclass(frozen=True)
class VersionSpec:
    """Comma-separated clauses like ``">=1.0,<2"``. Empty allows anything."""

    spec: str = ""

    def __post_init__(self) -> None:
        for clause in self._clauses():
            if not _SPEC_RE.match(clause):
                raise ManifestError(f"invalid version spec {self.spec!r}")

    def _clauses(self) -> list[str]:
        return [c.strip() for c in self.spec.split(",") if c.strip()]

    def allows(self, version: str) -> bool:
        have = version_key(version)
        for clause in self._clauses():
            op, ver = _SPEC_RE.match(clause).groups()  # type: ignore[union-attr]
            want = tuple(int(x) for x in ver.split(".")) + (0,) * (2 - ver.count("."))
            if not {">=": have >= want, "<=": have <= want, ">": have > want, "<": have < want, "==": have == want}[op]:
                return False
        return True


_CAPTURE_RE = re.compile(r"^\{([a-z_][a-z0-9_]*)(?::(upper|any))?\}$")
MAX_DISPLAY_NAME = 64


@dataclass(frozen=True)
class DisplayNameRule:
    """Turns the name a device reports into a friendlier one, e.g.
    ``CORSAIR VIRTUOSO SE Wireless Gaming Headset`` -> ``Corsair Virtuoso SE``.

    ``match`` is compared word by word: literal words ignore case, ``{name}``
    captures one word, ``{name:upper}`` captures one word only if it is in
    capitals or digits (so "Wireless" is never taken for a model suffix), and
    a final ``*`` matches whatever is left. Deliberately not a regex: this
    data comes from a package and runs in the service, and a word matcher
    runs in linear time, where a hostile regex can take exponential time.
    """

    tokens: tuple[str, ...]
    format: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> DisplayNameRule:
        tokens = tuple(d["match"].split())
        if not tokens or len(tokens) > 16:
            raise ManifestError("display_name.match needs 1-16 words")
        if "*" in tokens[:-1]:
            raise ManifestError("display_name.match: '*' is only allowed as the last word")
        captures = {m[1] for t in tokens if (m := _CAPTURE_RE.match(t))}
        for t in tokens:
            if t.startswith("{") and not _CAPTURE_RE.match(t):
                raise ManifestError(f"display_name.match: bad capture {t!r} (use {{name}} or {{name:upper}})")
        # Only bare capture names: str.format on package data could otherwise
        # reach attributes ({model.__class__}) or positional arguments.
        for _, f, spec, conv in string.Formatter().parse(d["format"]):
            if f is not None and (spec or conv or f not in captures):
                raise ManifestError(f"display_name.format may only use plain captures from match, not {{{f}}}")
        if len(d["format"]) > MAX_DISPLAY_NAME:
            raise ManifestError("display_name.format is too long")
        return cls(tokens, d["format"])

    def apply(self, name: str) -> str | None:
        words = name.split()[:32]
        captured: dict[str, str] = {}
        for i, token in enumerate(self.tokens):
            if token == "*":
                break
            if i >= len(words):
                return None
            if m := _CAPTURE_RE.match(token):
                word = words[i]
                if m[2] == "upper" and not (word.upper() == word and any(c.isalnum() for c in word)):
                    return None
                captured[m[1]] = word
            elif token.casefold() != words[i].casefold():
                return None
        else:
            if len(words) != len(self.tokens):
                return None
        return " ".join(self.format.format(**captured).split())[:MAX_DISPLAY_NAME] or None


def display_name(rules: tuple[DisplayNameRule, ...], reported: str) -> str | None:
    """First matching rule's result, or None to keep the reported name."""
    for rule in rules:
        if (result := rule.apply(reported)) is not None:
            return result
    return None


@dataclass(frozen=True)
class DriverRef:
    type: str
    version: VersionSpec
    provided_by: str | None
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> DriverRef:
        provided_by = d.get("provided_by")
        if provided_by is not None and not DRIVER_PACKAGE_RE.match(provided_by):
            raise ManifestError(f"driver.provided_by must match {DRIVER_PACKAGE_RE.pattern}")
        return cls(d["type"], VersionSpec(d.get("version", "")), provided_by, d)


@dataclass(frozen=True)
class BatterySpec:
    """Which driver state holds the battery, so every client can show it the
    same way (a battery icon for the level, a bolt while charging)::

        "battery": {"level": "battery", "charging": "charging",
                    "refresh": "refresh_battery", "refresh_if": "online"}

    ``level`` is a percentage. ``refresh`` names an action that asks the
    device for a new reading, offered while ``refresh_if`` is truthy.
    """

    level: str
    charging: str | None = None
    refresh: str | None = None
    refresh_if: str | None = None

    @classmethod
    def from_json(cls, d: Any) -> BatterySpec:
        if not isinstance(d, dict) or not isinstance(d.get("level"), str):
            raise ManifestError("battery.level must name a state key")
        extra = {k: d.get(k) for k in ("charging", "refresh", "refresh_if")}
        if any(v is not None and not isinstance(v, str) for v in extra.values()):
            raise ManifestError("battery.charging, refresh and refresh_if must be strings")
        return cls(d["level"], **extra)

    @property
    def keys(self) -> frozenset[str]:
        """State keys whose changes alter what clients show."""
        return frozenset(k for k in (self.level, self.charging, self.refresh_if) if k)


def state_key(value: Any) -> str:
    """How a state value is written in manifest maps: ``true``, ``null``, ``3``."""
    return str(value).lower() if isinstance(value, bool) or value is None else str(value)


@dataclass(frozen=True)
class AvailabilitySpec:
    """When a device that is plugged in can actually be used, e.g. a wireless
    dongle whose headset is switched off cannot::

        "available": "online"                                   # truthy state
        "available": {"state": "link", "values": ["online", "wired"]}

    While it is unavailable, clients fade its picture, hide its battery and
    disable its settings.
    """

    state: str
    values: frozenset[str] | None = None

    @classmethod
    def from_json(cls, d: Any) -> AvailabilitySpec:
        if isinstance(d, str):
            return cls(d)
        if isinstance(d, dict) and isinstance(d.get("state"), str):
            values = d.get("values")
            if values is None or (isinstance(values, list) and values):
                return cls(d["state"], None if values is None else frozenset(state_key(v) for v in values))
        raise ManifestError('available must be a state key or {"state": key, "values": [...]}')

    def __call__(self, state: dict[str, Any]) -> bool:
        value = state.get(self.state)
        return bool(value) if self.values is None else state_key(value) in self.values


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    name: str
    match: tuple[MatchRule, ...]
    channel: dict[str, Any]
    driver: DriverRef
    raw: dict[str, Any]
    display_names: tuple[DisplayNameRule, ...] = ()
    category: str = "other"
    image: str | None = None
    battery: BatterySpec | None = None
    available: AvailabilitySpec | None = None

    @property
    def ui(self) -> list[dict[str, Any]]:
        return self.raw.get("ui", [])

    def best_rule(self, ident: DeviceIdentity) -> MatchRule | None:
        scored = [(r.score(ident), r) for r in self.match]
        score, rule = max(scored, key=lambda sr: sr[0], default=(0, None))
        return rule if score else None

    def image_for(self, ident: DeviceIdentity) -> str | None:
        """Package-relative path of the device's picture, if the package has one."""
        rule = self.best_rule(ident)
        return (rule.image if rule else None) or self.image

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Manifest:
        try:
            if d.get("schema", SCHEMA_VERSION) > SCHEMA_VERSION:
                raise ManifestError(f"manifest schema {d['schema']} is newer than supported ({SCHEMA_VERSION})")
            pkg_id, version = d["id"], d["version"]
            if not _ID_RE.match(pkg_id):
                raise ManifestError(f"invalid package id {pkg_id!r} (expected reverse-dns like 'vendor.product')")
            version_key(version)
            if "tier" in d or "kind" in d:
                raise ManifestError("packages are data only; code ships as a system-packaged driver plugin")
            rules = tuple(MatchRule.from_json(r) for r in d["match"])
            if not rules:
                raise ManifestError("manifest needs at least one match rule")
            channel = dict(d["channel"])
            if "type" not in channel:
                raise ManifestError("channel.type is required")
            driver = DriverRef.from_json(d.get("driver", {"type": "declarative"}))
            names = tuple(DisplayNameRule.from_json(r) for r in d.get("display_name", []))
            category = d.get("category", "other")
            if not isinstance(category, str):
                raise ManifestError("category must be a string")
            battery = BatterySpec.from_json(d["battery"]) if "battery" in d else None
            available = AvailabilitySpec.from_json(d["available"]) if "available" in d else None
            image = _image_path(d.get("image"))
            for rule in rules:
                _image_path(rule.image)
        except KeyError as e:
            raise ManifestError(f"missing required field {e.args[0]!r}") from None
        category = category if category in CATEGORIES else "other"
        return cls(pkg_id, version, d.get("name", pkg_id), rules, channel, driver, d, names, category, image, battery, available)

    @classmethod
    def load(cls, package_dir: Path) -> Manifest:
        path = package_dir / MANIFEST_NAME
        try:
            return cls.from_json(json.loads(path.read_text()))
        except json.JSONDecodeError as e:
            raise ManifestError(f"{path}: {e}") from None


def _image_path(path: Any) -> str | None:
    """A PNG inside the package, as a plain relative path."""
    if path is None:
        return None
    p = PurePosixPath(path) if isinstance(path, str) else None
    if p is None or p.is_absolute() or ".." in p.parts or "\\" in path or p.suffix != ".png":
        raise ManifestError(f"image must be a relative path to a .png inside the package, got {path!r}")
    return p.as_posix()


def shard_key_for_rule(rule: MatchRule) -> str:
    """Inverse of DeviceIdentity.shard_keys(): which shard a rule is published in."""
    if rule.bus is Bus.USB:
        if rule.vid is None:
            raise ManifestError(f"{rule.bus} match rules must specify vid")
        return f"{rule.bus}/{rule.vid:04x}"
    if rule.bus is Bus.BLE:
        if rule.service_uuid:
            return f"ble/service/{rule.service_uuid}"
        if rule.company_id is not None:
            return f"ble/company/{rule.company_id:04x}"
        raise ManifestError("ble match rules must specify service_uuid or company_id")
    raise ManifestError(f"unsupported bus {rule.bus}")
