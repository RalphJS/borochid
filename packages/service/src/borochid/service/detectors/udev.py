"""USB detection through udev. Covers HID, CDC serial and vendor (libusb)
devices alike: matching happens on the USB device, and the package's channel
spec later picks the interface/node to talk to.

udev announces a USB device before it has created and set permissions on
its interface nodes (hidraw, tty), so the detector also watches those
subsystems. When a node under a known device is ready it reports
``device_changed``, and a bring-up that found no usable node is retried at
that moment instead of on a timer.

Devices paired to a wireless receiver are not USB devices of their own: the
receiver's kernel driver (``hid-logitech-dj`` and the like) creates a HID
device per paired device under the receiver's, with the paired device's own
product ID and serial. Each of those is reported as a device too, identified
as ``usb:<receiver port>/<slot>``, and its channel opens the hidraw node
under it, so a driver never sees the traffic of other devices on the same
receiver. The receiver itself is still reported like any USB device.
"""

from __future__ import annotations

import asyncio
import logging
import re

import pyudev

from borochid.common.models import Bus, DeviceIdentity
from borochid.service.detectors import Detector

log = logging.getLogger(__name__)

# HID_ID is "<bus>:<vendor>:<product>", each in hex.
_HID_ID_RE = re.compile(r"^[0-9A-Fa-f]{4,8}:([0-9A-Fa-f]{4,8}):([0-9A-Fa-f]{4,8})$")
# HID_PHYS of a paired device ends with ":<slot>" after the receiver's input.
_SLOT_RE = re.compile(r"input\d+:(\d+)$")


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


def paired_identity(hid: pyudev.Device) -> DeviceIdentity | None:
    """Identity of a device paired to a receiver, from its HID device; None
    for anything else (a HID device directly on USB is covered by the USB
    device itself)."""
    parent = hid.parent
    if parent is None or parent.subsystem != "hid":
        return None
    usb = hid.find_parent("usb", "usb_device")
    props = hid.properties
    m = _HID_ID_RE.match(props.get("HID_ID", ""))
    slot = _SLOT_RE.search(props.get("HID_PHYS", ""))
    if usb is None or not m or not slot:
        return None
    return DeviceIdentity(
        bus=Bus.USB,
        uid=f"usb:{usb.sys_name}/{slot[1]}",
        vid=int(m[1], 16),
        pid=int(m[2], 16),
        name=props.get("HID_NAME", ""),
        serial=props.get("HID_UNIQ") or None,
        attrs={"sys_path": hid.sys_path, "receiver": usb.sys_path},
    )


NODE_SUBSYSTEMS = ("hidraw", "tty")


class UdevDetector(Detector):
    async def start(self) -> None:
        self._ctx = pyudev.Context()
        # HID sys_path -> uid of paired devices reported so far. On removal
        # sysfs is already gone, so the uid can't be worked out again.
        self._paired: dict[str, str] = {}
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
        for hid in self._ctx.list_devices(subsystem="hid"):
            self._add_paired(hid)

    def _add_paired(self, hid: pyudev.Device) -> str | None:
        if (uid := self._paired.get(hid.sys_path)) is not None:
            return uid
        if (ident := paired_identity(hid)) is None:
            return None
        self._paired[hid.sys_path] = ident.uid
        self.sink.device_added(ident)
        return ident.uid

    def _drain(self) -> None:
        while (dev := self._monitor.poll(timeout=0)) is not None:
            if dev.subsystem in NODE_SUBSYSTEMS:
                self._node_event(dev)
            elif dev.action == "add":
                if ident := identity_from_udev(dev):
                    self.sink.device_added(ident)
            elif dev.action == "remove":
                self.sink.device_removed(f"usb:{dev.sys_name}")

    def _node_event(self, dev: pyudev.Device) -> None:
        if dev.action == "remove":
            hid_path = dev.sys_path.rsplit("/hidraw/", 1)[0] if dev.subsystem == "hidraw" else None
            if hid_path and (uid := self._paired.pop(hid_path, None)):
                self.sink.device_removed(uid)
            return
        if dev.action not in ("add", "change"):
            return
        hid = dev.find_parent("hid") if dev.subsystem == "hidraw" else None
        if hid is not None and (uid := self._add_paired(hid)) is not None:
            self.sink.device_changed(uid)  # its node is ready
        elif (usb := dev.find_parent("usb", "usb_device")) is not None:
            self.sink.device_changed(f"usb:{usb.sys_name}")

    async def stop(self) -> None:
        asyncio.get_running_loop().remove_reader(self._monitor.fileno())
