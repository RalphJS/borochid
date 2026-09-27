"""Loopback channel for simulated devices.

Behaviour comes from the manifest's optional ``simulation`` section so each
package can describe how its fake device should respond::

    "simulation": {
      "periodic": [{"every": 2.0, "report": "02 5a"}],
      "echo": true
    }

``periodic`` reports are hex strings; a byte written as ``~~`` is replaced by
a value that walks down from 100, handy for batteries and sensors.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from borochid.service.channels import Channel

log = logging.getLogger(__name__)


class SimChannel(Channel):
    def __init__(self, ident, spec, simulation=None):
        super().__init__(ident, spec)
        self.sim = simulation or {}
        self.written: list[bytes] = []
        self._tasks: list[asyncio.Task] = []

    async def open(self) -> None:
        for p in self.sim.get("periodic", []):
            self._tasks.append(asyncio.create_task(self._periodic(float(p["every"]), p["report"])))

    async def _periodic(self, every: float, template: str) -> None:
        level = 100
        while True:
            await asyncio.sleep(every)
            hex_ = " ".join(f"{level:02x}" if b == "~~" else b for b in template.split())
            self._deliver(bytes.fromhex(hex_))
            level = level - 1 if level > 0 else 100

    async def write(self, data: bytes) -> None:
        log.info("sim %s <- %s", self.ident.uid, data.hex(" "))
        self.written.append(data)
        if self.sim.get("echo"):
            asyncio.get_running_loop().call_soon(self._deliver, data)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
