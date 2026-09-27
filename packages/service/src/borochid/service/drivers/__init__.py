from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from borochid.common.manifest import Manifest
from borochid.service.channels import Channel
from borochid.service.settings import MemoryStore


class DriverError(Exception):
    pass


class Driver(ABC):
    """Translates between a device's bytes and named state/actions.

    State is a flat JSON-able dict; the GUI binds widgets to its keys. Call
    ``publish(changes)`` whenever state changes so clients are notified.
    ``settings`` is a per-device dict persisted by the service; call
    ``save_settings()`` after changing it. ``host`` exposes the service's host services
    (``host.audio`` ...) enabled by the manifest, so drivers never need to
    touch the host themselves. Driver plugins subclass this.
    """

    def __init__(
        self,
        manifest: Manifest,
        package_dir: Path,
        channel: Channel,
        publish: Callable[[dict[str, Any]], None],
        store: Any = None,
        host: Any = None,
    ):
        self.manifest = manifest
        self.package_dir = package_dir
        self.channel = channel
        self._publish = publish
        self.state: dict[str, Any] = {}
        self.host = host
        self._store = store or MemoryStore()
        self.settings: dict[str, Any] = self._store.load()

    def save_settings(self) -> None:
        self._store.save(self.settings)

    def publish(self, changes: dict[str, Any]) -> None:
        changes = {k: v for k, v in changes.items() if self.state.get(k, object()) != v}
        if changes:
            self.state.update(changes)
            self._publish(changes)

    async def start(self) -> None:
        """Called once the channel is open."""

    async def stop(self) -> None:
        """Called before the channel is closed."""

    @abstractmethod
    def on_data(self, data: bytes) -> None: ...

    @abstractmethod
    async def invoke(self, action: str, params: dict[str, Any]) -> Any: ...
