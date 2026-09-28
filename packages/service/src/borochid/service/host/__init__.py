"""Services the service provides to drivers, so packages never need to touch
the host themselves (spawn processes, write files, talk to the sound server).

A device package opts in through its manifest, e.g. ``"audio": {"match":
["corsair", "virtuoso"]}``. Each service owns a slice of the device's state
and actions under its own prefix (``audio.volume``, ``audio.set_volume``),
so UIs can bind to it without any driver code.

* ``audio``: ALSA volume/sidetone, PipeWire mic mute (``host/audio.py``).
* ``power``: the kernel's battery reading (``host/power.py``).
* ``input``: a virtual input device for button remapping
  (``host/input.py``). It is for driver code only and refuses every RPC
  action.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

from borochid.service.host.audio import HostAudio
from borochid.service.host.input import HostInput
from borochid.service.host.power import HostPower

log = logging.getLogger(__name__)

Publish = Callable[[dict[str, Any]], None]


class Host:
    def __init__(self, audio: HostAudio | None = None, power: HostPower | None = None, input: HostInput | None = None):
        self.audio = audio
        self.power = power
        self.input = input

    @property
    def services(self) -> dict[str, Any]:
        named = (("audio", self.audio), ("power", self.power), ("input", self.input))
        return {name: svc for name, svc in named if svc is not None}

    @property
    def state(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for svc in self.services.values():
            merged.update(svc.state)
        return merged

    def owns(self, action: str) -> bool:
        return action.partition(".")[0] in self.services

    async def invoke(self, action: str, params: dict[str, Any]) -> Any:
        prefix, _, name = action.partition(".")
        return await self.services[prefix].invoke(name, params)

    async def start(self) -> None:
        for svc in self.services.values():
            await svc.start()

    async def identify(self, device_id: str) -> None:
        """Host services with per-device settings follow the device's own ID too."""
        for svc in self.services.values():
            if hasattr(svc, "identify"):
                await svc.identify(device_id)

    async def stop(self) -> None:
        for name, svc in self.services.items():
            with contextlib.suppress(Exception):
                await svc.stop()
