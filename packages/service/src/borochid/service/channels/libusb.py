"""Vendor-specific USB interfaces through libusb.

Spec keys: ``interface`` (default 0), ``in_endpoint``, ``out_endpoint``
(e.g. "0x81" / "0x01"), ``packet_size`` (default 64).
"""

from __future__ import annotations

import asyncio
import contextlib

import usb.core
import usb.util

from borochid.common.models import parse_int
from borochid.service.channels import Channel, ChannelError


class LibusbChannel(Channel):
    async def open(self) -> None:
        bus, addr = self.ident.attrs.get("busnum"), self.ident.attrs.get("devnum")
        dev = usb.core.find(idVendor=self.ident.vid, idProduct=self.ident.pid, bus=int(bus), address=int(addr))
        if dev is None:
            raise ChannelError(f"libusb cannot find {self.ident.uid}")
        self._dev = dev
        self._iface = int(self.spec.get("interface", 0))
        with contextlib.suppress(NotImplementedError, usb.core.USBError):
            if dev.is_kernel_driver_active(self._iface):
                dev.detach_kernel_driver(self._iface)
        usb.util.claim_interface(dev, self._iface)
        self._in = parse_int(self.spec["in_endpoint"]) if "in_endpoint" in self.spec else None
        self._out = parse_int(self.spec["out_endpoint"])
        self._size = int(self.spec.get("packet_size", 64))
        self._reader = asyncio.create_task(self._read_loop()) if self._in is not None else None

    async def _read_loop(self) -> None:
        while True:
            try:
                data = await asyncio.to_thread(self._dev.read, self._in, self._size, 500)
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError as e:
                self.on_closed(e)
                return
            self._deliver(bytes(data))

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self._dev.write, self._out, data, 1000)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        with contextlib.suppress(usb.core.USBError):
            usb.util.release_interface(self._dev, self._iface)
        usb.util.dispose_resources(self._dev)
