from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from borochid.common.manifest import Manifest
from borochid.service.channels import Channel
from borochid.service.profiles import Profile
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

    Drivers that keep settings per profile set ``supports_profiles``: the
    service then calls ``use_profile()`` before ``start()`` and whenever the
    user switches, renames or deletes profiles (see service/profiles.py).

    A driver that can read the device's own ID (a unit ID, a serial the
    device reports whatever the connection) calls ``identify()``: its
    settings then follow the device across connections and ports, and the
    service shows a device connected two ways (receiver and cable) once.
    While the other connection is the one in use this one is ``passive``:
    it doesn't save settings, and it reloads them when it becomes active.
    """

    supports_profiles = False

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
        self.device_id: str | None = None
        self.passive = False
        self.on_identify: Callable[[str], None] | None = None  # set by the service

    def save_settings(self) -> None:
        if not self.passive:  # the connection in use owns the settings
            self._store.save(self.settings)

    async def identify(self, device_id: str) -> None:
        """The device's own ID, read from it. Settings kept so far move to
        it the first time; if some were already kept under it, they are
        loaded (``settings_reloaded()``)."""
        if not device_id or device_id == self.device_id:
            return
        self.device_id = device_id
        if self._store.rekey(device_id):
            await self.reload_settings()
        if (host_identify := getattr(self.host, "identify", None)) is not None:
            await host_identify(device_id)
        if self.on_identify is not None:
            self.on_identify(device_id)

    async def reload_settings(self) -> None:
        self.settings = self._store.load()
        await self.settings_reloaded()

    async def settings_reloaded(self) -> None:
        """``settings`` were replaced (the device's own settings found, or
        another connection changed them): drivers that keep state derived
        from them rebuild it here."""

    def publish(self, changes: dict[str, Any]) -> None:
        changes = {k: v for k, v in changes.items() if self.state.get(k, object()) != v}
        if changes:
            self.state.update(changes)
            self._publish(changes)

    async def start(self) -> None:
        """Called once the channel is open."""

    async def stop(self) -> None:
        """Called before the channel is closed."""

    async def use_profile(self, profile: Profile, known: set[str]) -> None:
        """Make ``profile`` active. A profile the driver has no settings for
        starts from its settings for ``profile.copy_of`` if it has those, else
        from its defaults. Settings for ids not in ``known`` (deleted
        profiles) should be dropped. Before ``start()`` nothing is applied
        yet; afterwards the device should switch at once."""

    @abstractmethod
    def on_data(self, data: bytes) -> None: ...

    @abstractmethod
    async def invoke(self, action: str, params: dict[str, Any]) -> Any: ...
