"""BlueZ signal handling, with synthesized D-Bus messages (no Bluetooth needed)."""

import pytest

pytest.importorskip("dbus_fast")

from dbus_fast import Message, MessageType, Variant  # noqa: E402

from borochid.service.detectors.ble import DEVICE, BleDetector  # noqa: E402

PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


class Sink:
    def __init__(self):
        self.events = []

    def device_added(self, ident):
        self.events.append(("added", ident.uid))

    def device_removed(self, uid):
        self.events.append(("removed", uid))

    def device_changed(self, uid):
        self.events.append(("changed", uid))


def detector():
    d = BleDetector(Sink())
    d._devices, d._reported, d._adapters, d._scan_task = {}, {}, [], None
    return d


def signal(member, body, path="/", sig=""):
    return Message(message_type=MessageType.SIGNAL, path=path, interface="org.freedesktop.DBus.Properties", member=member, signature=sig, body=body)


def props(**kw):
    base = {"Address": "AA:BB:CC:DD:EE:FF", "Name": "Pad", "UUIDs": [], "Paired": False, "Connected": False}
    base.update(kw)
    return {k: Variant({str: "s", bool: "b", list: "as"}[type(v)], v) for k, v in base.items()}


def test_unpaired_devices_seen_outside_a_scan_are_ignored():
    d = detector()
    d._on_message(signal("InterfacesAdded", [PATH, {DEVICE: props()}]))
    assert d.sink.events == []


def test_pairing_connecting_and_forgetting():
    d = detector()
    d._on_message(signal("InterfacesAdded", [PATH, {DEVICE: props()}]))
    d._on_message(signal("PropertiesChanged", [DEVICE, {"Paired": Variant("b", True)}, []], path=PATH))
    d._on_message(signal("PropertiesChanged", [DEVICE, {"Connected": Variant("b", True)}, []], path=PATH))
    d._on_message(signal("InterfacesRemoved", [PATH, [DEVICE]]))
    uid = "ble:AA:BB:CC:DD:EE:FF"
    assert d.sink.events == [("added", uid), ("changed", uid), ("removed", uid)]


def test_scan_finds_are_forgotten_when_the_scan_ends():
    import asyncio

    async def go():
        d = detector()
        d._scan_task = asyncio.create_task(asyncio.sleep(1))  # pretend a scan is running
        d._on_message(signal("InterfacesAdded", [PATH, {DEVICE: props()}]))
        d._scan_task.cancel()
        d._scan_task = None
        for path in list(d._reported):
            d._evaluate(path)
        return d.sink.events

    events = asyncio.run(go())
    assert events == [("added", "ble:AA:BB:CC:DD:EE:FF"), ("removed", "ble:AA:BB:CC:DD:EE:FF")]
