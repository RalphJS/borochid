"""Battery as the kernel reports it (``/sys/class/power_supply``).

Several kernel drivers already track a device's battery (hid-logitech-hidpp,
hid-playstation, hid-nintendo...). Reading it from sysfs needs no access to
the device at all and never competes with the kernel's own polling, so a
package enables this instead of teaching its driver the battery protocol::

    "power_supply": {}
    "battery": {"level": "power.level", "charging": "power.charging"}

The supply is the one under the device's own sysfs node (for a device on a
receiver, its paired HID device), so a receiver with two devices never
mixes up their batteries. The kernel sends a udev ``change`` event whenever
a reading changes; between events nothing is polled, apart from a slow
safety re-read.

State: ``power.level`` (0-100 or None), ``power.charging``, ``power.online``
(the kernel can reach the device). It offers no actions.

The kernel's own ``online`` is narrower than that for hid-logitech-hidpp:
it means "running on its battery", so a mouse charging from a wall charger
while still in use through its receiver reads offline. A device that has
lost its link reads ``Unknown``, never ``Charging``, so charging counts as
reachable too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

Watch = Callable[[Path], AsyncIterator[None]]


async def udev_watch(device: Path) -> AsyncIterator[None]:
    """Yields whenever a power supply under ``device`` is added or changes."""
    import pyudev

    monitor = pyudev.Monitor.from_netlink(pyudev.Context())
    monitor.filter_by("power_supply")
    monitor.start()
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_reader(monitor.fileno(), ready.set)
    prefix = str(device) + "/"
    try:
        while True:
            await ready.wait()
            ready.clear()
            hit = False
            while (dev := monitor.poll(timeout=0)) is not None:
                hit = hit or dev.sys_path.startswith(prefix)
            if hit:
                yield
    finally:
        loop.remove_reader(monitor.fileno())


class HostPower:
    def __init__(
        self,
        device: Path,
        publish: Callable[[dict[str, Any]], None],
        watch: Watch = udev_watch,
        reread_s: float = 300.0,
    ):
        self.device = device
        self._publish = publish
        self._watch = watch
        self.reread_s = reread_s
        self.state: dict[str, Any] = {"power.level": None, "power.charging": False, "power.online": False}
        self._task: asyncio.Task | None = None

    def supply(self) -> Path | None:
        """The battery under the device. Found anew on every read: the kernel
        re-creates it (with a new number) when the device reconnects."""
        with contextlib.suppress(OSError):
            for p in sorted((self.device / "power_supply").iterdir()):
                if _read(p / "type") == "Battery":
                    return p
        return None

    def read(self) -> None:
        changes: dict[str, Any] = {"power.level": None, "power.charging": False, "power.online": False}
        if (p := self.supply()) is not None:
            level = _read(p / "capacity")
            status = _read(p / "status")
            online = _read(p / "online")
            changes["power.level"] = int(level) if level and level.isdigit() and int(level) <= 100 else None
            changes["power.charging"] = status in ("Charging", "Full")
            # Batteries without an "online" attribute are reachable when they report a level.
            if online is None:
                changes["power.online"] = changes["power.level"] is not None
            else:
                changes["power.online"] = online == "1" or changes["power.charging"]
        changes = {k: v for k, v in changes.items() if self.state.get(k) != v}
        if changes:
            self.state.update(changes)
            self._publish(changes)

    async def start(self) -> None:
        self.read()
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        events = self._watch(self.device)
        next_event = asyncio.ensure_future(anext(events))
        try:
            while True:
                done, _ = await asyncio.wait({next_event}, timeout=self.reread_s)
                if next_event in done:
                    try:
                        next_event.result()
                    except StopAsyncIteration:
                        log.warning("power supply watch for %s ended; re-reading on a timer only", self.device)
                        next_event = asyncio.get_running_loop().create_future()  # never completes
                    else:
                        next_event = asyncio.ensure_future(anext(events))
                self.read()
        finally:
            next_event.cancel()
            with contextlib.suppress(Exception):
                await events.aclose()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def invoke(self, action: str, params: dict[str, Any]) -> Any:
        raise KeyError(f"power has no action {action!r}")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None
