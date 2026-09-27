"""BLE GATT. Spec keys: ``notify`` (characteristic UUID to subscribe),
``write`` (characteristic UUID), ``write_response`` (bool, default false)."""

from __future__ import annotations

from bleak import BleakClient
from bleak.exc import BleakError

from borochid.service.channels import Channel, ChannelError


class BleChannel(Channel):
    outlives_detection = True

    async def open(self) -> None:
        self._client = BleakClient(self.ident.attrs["address"], disconnected_callback=lambda _c: self.on_closed(None))
        try:
            await self._client.connect()
            if notify := self.spec.get("notify"):
                await self._client.start_notify(notify, lambda _char, data: self._deliver(bytes(data)))
        except BleakError as e:
            raise ChannelError(str(e)) from e

    async def write(self, data: bytes) -> None:
        await self._client.write_gatt_char(self.spec["write"], data, response=bool(self.spec.get("write_response")))

    async def close(self) -> None:
        await self._client.disconnect()
