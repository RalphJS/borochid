"""HID via Linux hidraw. Reports are read/written whole; the first byte is the
report ID when the device uses numbered reports."""

from __future__ import annotations

import asyncio
import os

from borochid.service.channels import Channel, ChannelNotReady, find_usb_child

MAX_REPORT = 4096


class HidChannel(Channel):
    async def open(self) -> None:
        node = find_usb_child(self.ident, "hidraw", self.spec.get("interface"))
        try:
            self._fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise ChannelNotReady(
                f"no access to {node} yet; if this persists, the driver package's udev rule is not installed"
            ) from None
        except FileNotFoundError:
            raise ChannelNotReady(f"{node} disappeared") from None
        asyncio.get_running_loop().add_reader(self._fd, self._readable)

    def _readable(self) -> None:
        try:
            data = os.read(self._fd, MAX_REPORT)
        except BlockingIOError:
            return
        except OSError as e:
            self._teardown()
            self.on_closed(e)
            return
        self._deliver(data)

    async def write(self, data: bytes) -> None:
        if (size := self.spec.get("report_size")) and len(data) < size:
            data = data.ljust(size, b"\0")
        os.write(self._fd, data)

    def _teardown(self) -> None:
        if getattr(self, "_fd", None) is None:
            return
        asyncio.get_running_loop().remove_reader(self._fd)
        os.close(self._fd)
        self._fd = None

    async def close(self) -> None:
        self._teardown()
