"""USB CDC / serial ports. Spec keys: ``interface``, ``baudrate`` (default 115200)."""

from __future__ import annotations

import asyncio

import serial

from borochid.service.channels import Channel, ChannelError, ChannelNotReady, find_usb_child


class SerialChannel(Channel):
    async def open(self) -> None:
        node = find_usb_child(self.ident, "tty", self.spec.get("interface"))
        try:
            self._port = serial.Serial(node, baudrate=int(self.spec.get("baudrate", 115200)), timeout=0)
        except serial.SerialException as e:
            if isinstance(e.__context__, PermissionError) or "Permission denied" in str(e):
                raise ChannelNotReady(f"no access to {node} yet") from e
            raise ChannelError(str(e)) from e
        asyncio.get_running_loop().add_reader(self._port.fileno(), self._readable)

    def _readable(self) -> None:
        try:
            data = self._port.read(self._port.in_waiting or 1)
        except serial.SerialException as e:
            self._teardown()
            self.on_closed(e)
            return
        if data:
            self._deliver(data)

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self._port.write, data)

    def _teardown(self) -> None:
        if getattr(self, "_port", None) is None:
            return
        asyncio.get_running_loop().remove_reader(self._port.fileno())
        self._port.close()
        self._port = None

    async def close(self) -> None:
        self._teardown()
