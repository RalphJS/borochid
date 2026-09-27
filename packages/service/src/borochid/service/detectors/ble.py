"""Bluetooth LE detection driven by BlueZ events. It never scans on its own.

At start it reads the devices BlueZ already knows (paired or connected) in one
call, then waits for signals: ``InterfacesAdded``/``InterfacesRemoved`` when
devices appear or are forgotten, and ``PropertiesChanged`` when one pairs,
connects or disconnects. Between signals the process sleeps.

Continuous discovery costs radio time and wakes the process for every
advertisement nearby, so it only runs on request: ``scan(seconds)`` runs a
bounded LE discovery (the GUI's "Scan Bluetooth"), reports what it finds, and
forgets unpaired finds when it ends. Desktop Bluetooth settings behave the
same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from borochid.common.models import Bus, DeviceIdentity
from borochid.service.detectors import Detector

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
DEVICE = "org.bluez.Device1"
ADAPTER = "org.bluez.Adapter1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"

MAX_SCAN_SECONDS = 60


def _plain(props: dict[str, Any]) -> dict[str, Any]:
    return {k: v.value if isinstance(v, Variant) else v for k, v in props.items()}


class BleDetector(Detector):
    async def start(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._devices: dict[str, dict[str, Any]] = {}  # object path -> Device1 props
        self._reported: dict[str, str] = {}  # object path -> uid reported to the sink
        self._adapters: list[str] = []
        self._scan_task: asyncio.Task | None = None
        for rule in (
            f"type='signal',sender='{BLUEZ}',interface='{OBJECT_MANAGER}'",
            f"type='signal',sender='{BLUEZ}',interface='{PROPERTIES}',member='PropertiesChanged',arg0='{DEVICE}'",
        ):
            await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s", [rule])
        self._bus.add_message_handler(self._on_message)

        reply = await self._call(BLUEZ, "/", OBJECT_MANAGER, "GetManagedObjects")
        for path, ifaces in reply.body[0].items():
            if ADAPTER in ifaces:
                self._adapters.append(path)
            if DEVICE in ifaces:
                self._devices[path] = _plain(ifaces[DEVICE])
                self._evaluate(path)
        log.info("bluetooth: %d adapter(s), %d known device(s)", len(self._adapters), len(self._devices))

    async def stop(self) -> None:
        if self._scan_task:
            self._scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scan_task
        self._bus.disconnect()

    async def _call(self, dest: str, path: str, iface: str, member: str, sig: str = "", body: list | None = None) -> Message:
        reply = await self._bus.call(
            Message(destination=dest, path=path, interface=iface, member=member, signature=sig, body=body or [])
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name} {reply.body}")
        return reply

    # -- signals ---------------------------------------------------------------

    def _on_message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "InterfacesAdded" and DEVICE in msg.body[1]:
            self._devices[msg.body[0]] = _plain(msg.body[1][DEVICE])
            self._evaluate(msg.body[0])
        elif msg.member == "InterfacesRemoved" and DEVICE in msg.body[1]:
            self._devices.pop(msg.body[0], None)
            self._evaluate(msg.body[0])
        elif msg.member == "PropertiesChanged" and msg.path in self._devices:
            changed = _plain(msg.body[1])
            props = self._devices[msg.path]
            was_connected = props.get("Connected")
            props.update(changed)
            self._evaluate(msg.path)
            if changed.get("Connected") and not was_connected and msg.path in self._reported:
                # Back in range: let the service retry a device whose bring-up failed.
                self.sink.device_changed(self._uid(props))

    def _wanted(self, props: dict[str, Any] | None) -> bool:
        if props is None:
            return False
        return bool(props.get("Paired") or props.get("Connected") or self._scanning)

    @property
    def _scanning(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    @staticmethod
    def _uid(props: dict[str, Any]) -> str:
        return f"ble:{props.get('Address', '?')}"

    def _evaluate(self, path: str) -> None:
        props = self._devices.get(path)
        wanted, reported = self._wanted(props), path in self._reported
        if wanted and not reported:
            self._reported[path] = self._uid(props)
            self.sink.device_added(
                DeviceIdentity(
                    bus=Bus.BLE,
                    uid=self._uid(props),
                    name=props.get("Name") or props.get("Alias") or "",
                    attrs={
                        "address": props.get("Address"),
                        "service_uuids": list(props.get("UUIDs", [])),
                        "company_ids": list((props.get("ManufacturerData") or {}).keys()),
                        "object_path": path,
                    },
                )
            )
        elif reported and not wanted:
            self.sink.device_removed(self._reported.pop(path))

    # -- on-demand discovery -----------------------------------------------------

    def scan(self, seconds: float) -> float:
        """Start a bounded LE discovery; returns the duration actually used."""
        seconds = max(5.0, min(float(seconds), MAX_SCAN_SECONDS))
        if not self._scanning:
            self._scan_task = asyncio.create_task(self._scan(seconds))
        return seconds

    async def _scan(self, seconds: float) -> None:
        started = []
        try:
            for adapter in self._adapters:
                try:
                    await self._call(BLUEZ, adapter, ADAPTER, "SetDiscoveryFilter", "a{sv}", [{"Transport": Variant("s", "le")}])
                    await self._call(BLUEZ, adapter, ADAPTER, "StartDiscovery")
                    started.append(adapter)
                except RuntimeError as e:
                    log.warning("bluetooth scan on %s failed: %s", adapter, e)
            await asyncio.sleep(seconds)
        finally:
            for adapter in started:
                with contextlib.suppress(Exception):
                    await self._call(BLUEZ, adapter, ADAPTER, "StopDiscovery")
            self._scan_task = None
            for path in list(self._reported):
                self._evaluate(path)  # drops unpaired, unconnected finds
