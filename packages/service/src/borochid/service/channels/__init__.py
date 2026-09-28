from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from borochid.common.models import DeviceIdentity

log = logging.getLogger(__name__)


class ChannelError(Exception):
    pass


class ChannelNotReady(ChannelError):
    """The device node is missing or not yet accessible.

    Normal for a moment after a device appears: udev creates interface nodes
    and applies access rules after announcing the USB device itself. The
    detector reports when a node is ready, and bring-up is retried then.
    """


class Channel(ABC):
    """Byte-level I/O with one device, configured by the manifest's ``channel`` section.

    Incoming data is pushed to ``on_data``; ``on_closed`` fires if the device
    goes away underneath us. Both are set by the device manager before ``open``.
    """

    # True when the detector may lose sight of a device we are still talking to
    # (BLE peripherals stop advertising once connected).
    outlives_detection = False

    def __init__(self, ident: DeviceIdentity, spec: dict[str, Any]):
        self.ident = ident
        self.spec = spec
        self.on_data: Callable[[bytes], None] = lambda _data: None
        self.on_closed: Callable[[Exception | None], None] = lambda _exc: None

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    def _deliver(self, data: bytes) -> None:
        try:
            self.on_data(data)
        except Exception:
            log.exception("driver failed handling data from %s", self.ident.uid)


class NullChannel(Channel):
    """For devices a package recognises but cannot talk to in their current
    mode (``"channel": null`` in the matching rule). Nothing is opened."""

    async def open(self) -> None:
        pass

    async def write(self, data: bytes) -> None:
        raise ChannelError("this device has no channel in its current mode")

    async def close(self) -> None:
        pass


class PairedChannel(Channel):
    """A device behind a receiver that the kernel doesn't split out (a
    headset on its dongle, say), announced by the receiver's driver
    (``Driver.pair``). Writes go through the receiver's channel, and the
    receiver's driver hands over this device's input with ``deliver()``.
    Opening and closing touch nothing: the receiver owns the node.

    ``shared`` is whatever the receiver's driver passes along for the paired
    device's driver (both come from the same plugin), e.g. the request/reply
    session both must use so their replies don't cross."""

    def __init__(self, ident: DeviceIdentity, receiver: Channel, shared: Any = None):
        super().__init__(ident, {})
        self.receiver = receiver
        self.shared = shared

    async def open(self) -> None:
        pass

    async def write(self, data: bytes) -> None:
        await self.receiver.write(data)

    async def close(self) -> None:
        pass

    def deliver(self, data: bytes) -> None:
        self._deliver(data)


def find_usb_child(ident: DeviceIdentity, subsystem: str, interface: int | list[int] | None):
    """Locate a device node (hidraw, tty, ...) belonging to a USB device.

    ``interface`` selects the USB interface number when the device exposes
    several nodes of the same kind. A list is an order of preference: the
    first interface number the device actually has wins.
    """
    import pyudev

    ctx = pyudev.Context()
    parent = ident.attrs["sys_path"]
    by_iface: dict[int | None, list[str]] = {}
    for dev in ctx.list_devices(subsystem=subsystem):
        if not dev.sys_path.startswith(parent + "/") or not dev.device_node:
            continue
        iface = dev.find_parent("usb", "usb_interface")
        num = int(iface.attributes.asstring("bInterfaceNumber"), 16) if iface else None
        by_iface.setdefault(num, []).append(dev.device_node)
    return pick_node(by_iface, interface, f"{subsystem} node for {ident.uid}")


def pick_node(by_iface: dict[int | None, list[str]], interface: int | list[int] | None, what: str) -> str:
    if interface is None:
        nodes = [n for ns in by_iface.values() for n in ns]
    else:
        prefs = interface if isinstance(interface, list) else [interface]
        nodes = next((by_iface[i] for i in prefs if i in by_iface), [])
    if not nodes:
        raise ChannelNotReady(f"no {what} (interface={interface})")
    return sorted(nodes)[0]
