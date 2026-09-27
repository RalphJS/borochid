"""Interprets data-only packages. Manifest sections::

    "state":   {"battery": null, "brightness": 128}          initial values
    "inputs":  [{"prefix": "02",                             match on leading bytes
                 "fields": {"battery": {"offset": 1, "format": "B"}}}]
    "actions": {"set_brightness": {
                  "params": {"value": {"type": "int", "min": 0, "max": 255}},
                  "write": [{"format": "BB", "values": [3, "$value"]}],
                  "state": {"brightness": "$value"}}}      optimistic update
    "init":    ["query_battery"]                          actions run on connect

Formats are ``struct`` codes (default little-endian). Values are literals or
``$param`` references; color params also expose ``$param.r/.g/.b``. Fields
may add ``"scale"`` (multiplier) or ``"map"`` ({raw: label}).
"""

from __future__ import annotations

import re
import struct
from typing import Any

from borochid.service.drivers import Driver, DriverError

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _fmt(f: str) -> str:
    return f if f[:1] in "<>!=@" else "<" + f


class DeclarativeDriver(Driver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        raw = self.manifest.raw
        self.state = dict(raw.get("state", {}))
        self._actions: dict[str, dict[str, Any]] = raw.get("actions", {})
        self._inputs = [
            (bytes.fromhex(i.get("prefix", "")), i.get("fields", {})) for i in raw.get("inputs", [])
        ]

    async def start(self) -> None:
        for action in self.manifest.raw.get("init", []):
            await self.invoke(action, {})

    def on_data(self, data: bytes) -> None:
        changes: dict[str, Any] = {}
        for prefix, fields in self._inputs:
            if not data.startswith(prefix):
                continue
            for name, f in fields.items():
                fmt = _fmt(f["format"])
                offset = int(f.get("offset", 0))
                if offset + struct.calcsize(fmt) > len(data):
                    continue
                (value,) = struct.unpack_from(fmt, data, offset)
                if "scale" in f:
                    value = value * f["scale"]
                if "map" in f:
                    value = f["map"].get(str(value), value)
                changes[name] = value
        if changes:
            self.publish(changes)

    async def invoke(self, action: str, params: dict[str, Any]) -> Any:
        try:
            spec = self._actions[action]
        except KeyError:
            raise DriverError(f"unknown action {action!r}") from None
        env = self._bind_params(spec.get("params", {}), params)

        packet = b"".join(
            struct.pack(_fmt(part["format"]), *(self._resolve(v, env) for v in part["values"]))
            for part in spec.get("write", [])
        )
        if packet:
            await self.channel.write(packet)
        if "state" in spec:
            self.publish({k: self._resolve(v, env) for k, v in spec["state"].items()})
        return None

    @staticmethod
    def _bind_params(schema: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
        env: dict[str, Any] = {}
        for name, p in schema.items():
            if name not in given:
                if "default" in p:
                    given = {**given, name: p["default"]}
                else:
                    raise DriverError(f"missing parameter {name!r}")
            value, kind = given[name], p.get("type", "int")
            if kind == "int":
                value = int(value)
            elif kind == "float":
                value = float(value)
            elif kind == "bool":
                value = bool(value)
            elif kind == "color":
                if not isinstance(value, str) or not _COLOR_RE.match(value):
                    raise DriverError(f"parameter {name!r} must be #rrggbb")
                env[f"{name}.r"] = int(value[1:3], 16)
                env[f"{name}.g"] = int(value[3:5], 16)
                env[f"{name}.b"] = int(value[5:7], 16)
            elif kind == "enum":
                if value not in p.get("options", []):
                    raise DriverError(f"parameter {name!r} must be one of {p.get('options')}")
            if kind in ("int", "float"):
                if "min" in p and value < p["min"] or "max" in p and value > p["max"]:
                    raise DriverError(f"parameter {name!r}={value} out of range")
            env[name] = value
        return env

    @staticmethod
    def _resolve(value: Any, env: dict[str, Any]) -> Any:
        if isinstance(value, str) and value.startswith("$"):
            try:
                return env[value[1:]]
            except KeyError:
                raise DriverError(f"unbound reference {value}") from None
        return value
