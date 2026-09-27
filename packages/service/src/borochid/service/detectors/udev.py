"""USB detection through udev. Covers HID, CDC serial and vendor (libusb)
devices alike: matching happens on the USB device, and the package's channel
spec later picks the interface/node to talk to.

udev announces a USB device before it has created and set permissions on
its interface nodes (hidraw, tty), so the detector also watches those
subsystems. When a node under a known device is ready it reports
``device_changed``, and a bring-up that found no usable node is retried at
that moment instead of on a timer."""

from __future__ import annotations

import asyncio
import logging

import pyudev

from borochid.common.models import Bus, DeviceIdentity
from borochid.service.detectors import Detector

log = logging.getLogger(__name__)


def identity_from_udev(dev: pyudev.Device) -> DeviceIdentity | None:
    props = dev.properties
    try:
        vid = int(props["ID_VENDOR_ID"], 16)
        pid = int(props["ID_MODEL_ID"], 16)
    except (KeyError, ValueError):
        return None
    name = (dev.attributes.get("product") or b"").decode(errors="replace") or props.get("ID_MODEL", "")
    return DeviceIdentity(
        bus=Bus.USB,
        uid=f"usb:{dev.sys_name}",
        vid=vid,
        pid=pid,
        name=name.replace("_", " "),
        serial=props.get("ID_SERIAL_SHORT"),
        attrs={"sys_path": dev.sys_path, "busnum": props.get("BUSNUM"), "devnum": props.get("DEVNUM")},
    )


NODE_SUBSYSTEMS = ("hidraw", "tty")


class UdevDetector(Detector):
    async def start(self) -> None:
        self._ctx = pyudev.Context()
        self._monitor = pyudev.Monitor.from_netlink(self._ctx)
        self._monitor.filter_by("usb", device_type="usb_device")
        for subsystem in NODE_SUBSYSTEMS:
            self._monitor.filter_by(subsystem)
        self._monitor.start()
        loop = asyncio.get_running_loop()
        loop.add_reader(self._monitor.fileno(), self._drain)

        for dev in self._ctx.list_devices(subsystem="usb", DEVTYPE="usb_device"):
            if ident := identity_from_udev(dev):
                self.sink.device_added(ident)

    def _drain(self) -> None:
        while (dev := self._monitor.poll(timeout=0)) is not None:
            if dev.subsystem in NODE_SUBSYSTEMS:
                if dev.action in ("add", "change") and (usb := dev.find_parent("usb", "usb_device")) is not None:
                    self.sink.device_changed(f"usb:{usb.sys_name}")
            elif dev.action == "add":
                if ident := identity_from_udev(dev):
                    self.sink.device_added(ident)
            elif dev.action == "remove":
                self.sink.device_removed(f"usb:{dev.sys_name}")

    async def stop(self) -> None:
        asyncio.get_running_loop().remove_reader(self._monitor.fileno())
