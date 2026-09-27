from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from borochid.common.models import DeviceIdentity


class DeviceSink(Protocol):
    def device_added(self, ident: DeviceIdentity) -> None: ...
    def device_removed(self, uid: str) -> None: ...
    def device_changed(self, uid: str) -> None:
        """Something about a present device changed (e.g. it came back in range)."""


class Detector(ABC):
    """Discovers devices on one bus and reports arrivals/departures.

    Detectors only identify devices; they never open them. Implementations
    must report already-present devices on ``start()`` and then wait for
    events from the OS rather than polling.
    """

    def __init__(self, sink: DeviceSink, options: dict[str, Any] | None = None):
        self.sink = sink
        self.options = options or {}

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...
