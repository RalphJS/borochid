"""Device lifecycle: detected -> resolving -> connecting -> ready.

Every device gets its own bring-up task, so a slow registry or a stuck device
never delays the others. Status and state changes are emitted as events that
the RPC server fans out to clients.

Some devices re-enumerate to change mode (a wireless dongle becomes another
product while its headset is off). A removal is therefore held for
``grace_s``: if the same device (same port, same serial) comes back in time,
it continues as the same entry instead of being removed and re-added.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from borochid.common import images
from borochid.common.manifest import Manifest, display_name
from borochid.common.models import DeviceIdentity, DeviceStatus
from borochid.service import plugins
from borochid.service.channels import Channel, ChannelNotReady, NullChannel
from borochid.service.drivers import Driver
from borochid.service.drivers.loader import DriverUnavailable, driver_class
from borochid.service.host import Host
from borochid.service.host.audio import HostAudio
from borochid.service.registry.client import Candidate, Registry
from borochid.service.registry.trust import TrustError
from borochid.service.settings import SettingsStore

log = logging.getLogger(__name__)

Emit = Callable[[str, dict[str, Any]], None]

# Hint shown when a channel plugin's optional dependency is missing.
_EXTRAS = {"serial": "serial", "libusb": "libusb", "ble": "ble"}


@dataclass
class Device:
    ident: DeviceIdentity
    status: DeviceStatus = DeviceStatus.DETECTED
    error: str | None = None
    needs: dict[str, Any] | None = None
    candidate: Candidate | None = None
    manifest: Manifest | None = None
    package_dir: Path | None = None
    image: str | None = None  # digest in the image store, see export_image()
    channel: Channel | None = None
    driver: Driver | None = None
    host: Host | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    # A readiness event arrived while bring-up was running; retry after it.
    changed_during_bring_up: bool = False
    # Pending removal while waiting to see whether the device comes back.
    removal: asyncio.TimerHandle | None = field(default=None, repr=False)
    teardown: asyncio.Task | None = field(default=None, repr=False)

    def summary(self) -> dict[str, Any]:
        pkg = None
        if self.manifest and self.candidate:
            pkg = {
                "id": self.manifest.id,
                "version": self.manifest.version,
                "name": self.manifest.name,
                "driver": self.manifest.driver.type,
                "source": self.candidate.source,
            }
        return {
            **self.ident.to_json(),
            # What the device calls itself (e.g. its USB product string) is
            # more specific than the package, which may cover a whole family.
            "display_name": self.display_name(),
            "category": self.manifest.category if self.manifest else None,
            # Digest of the device's picture in the image store (service.info).
            "image": self.image,
            "status": str(self.status),
            "status_text": self.status_text(),
            "battery": self.battery(),
            "available": self.available(),
            "error": self.error,
            "needs": self.needs,
            "package": pkg,
        }

    def display_name(self) -> str:
        """The package's fix-up of the reported name if one matches, else the
        reported name, else the package name."""
        reported = self.ident.name.strip()
        if reported and self.manifest and (fixed := display_name(self.manifest.display_names, reported)):
            return fixed
        return reported or (self.manifest.name if self.manifest else "") or self.ident.uid

    def status_text(self) -> str | None:
        """The package's own one-line status (manifest ``summary``), shown
        instead of the generic "Ready", e.g. "Headset off"."""
        spec = self.manifest.raw.get("summary") if self.manifest else None
        if self.status is not DeviceStatus.READY or not spec or not self.driver:
            return None
        value = self.driver.state.get(spec["state"])
        key = str(value).lower() if isinstance(value, bool) or value is None else str(value)
        return spec.get("map", {}).get(key, None if value is None else str(value))

    def state(self) -> dict[str, Any]:
        return {**(self.driver.state if self.driver else {}), **(self.host.state if self.host else {})}

    def available(self) -> bool:
        """False while a ready device can't be used (manifest ``available``),
        e.g. a dongle whose headset is switched off."""
        spec = self.manifest.available if self.manifest else None
        return spec is None or self.status is not DeviceStatus.READY or spec(self.state())

    def battery(self) -> dict[str, Any] | None:
        """``{"level": 0-100 or None, "charging": bool, "refresh": action or
        None}`` for packages with a ``battery`` section, else None."""
        spec = self.manifest.battery if self.manifest else None
        if spec is None or self.status is not DeviceStatus.READY:
            return None
        state = self.state()
        level = state.get(spec.level)
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            level = None
        else:
            level = max(0, min(100, round(level)))
        can_refresh = spec.refresh and (not spec.refresh_if or bool(state.get(spec.refresh_if)))
        return {
            "level": level,
            "charging": bool(spec.charging and state.get(spec.charging)),
            "refresh": spec.refresh if can_refresh else None,
        }

    def detail(self) -> dict[str, Any]:
        d = self.summary()
        d["ui"] = self.manifest.ui if self.manifest else []
        d["actions"] = {
            name: {"params": spec.get("params", {})}
            for name, spec in (self.manifest.raw.get("actions", {}) if self.manifest else {}).items()
        }
        d["state"] = self.state()
        return d


def export_image(manifest: Manifest, package_dir: Path, ident: DeviceIdentity, store_dir: Path) -> str | None:
    """Put the device's picture in the image store and return its digest, or
    None so clients fall back to the theme icon.

    A bad image never stops the device from working; it is logged and skipped.
    """
    if not (rel := manifest.image_for(ident)):
        return None
    try:
        path = (package_dir / rel).resolve()
        if not path.is_relative_to(package_dir.resolve()):  # symlink out of a local package
            raise images.ImageError("image is outside the package")
        with path.open("rb") as f:
            data = f.read(images.MAX_BYTES + 1)
        return images.store(store_dir, data)
    except (OSError, images.ImageError) as e:
        log.warning("%s: ignoring image %s: %s", manifest.id, rel, e)
        return None


class DeviceManager:
    def __init__(self, registry: Registry, emit: Emit, grace_s: float = 3.0):
        self.registry = registry
        self.grace_s = grace_s
        self.emit = emit
        self.data_dir = registry.cfg.data_dir
        self.cache_dir = registry.cfg.cache_dir
        self.image_store = registry.cfg.cache_dir / "images"
        self.devices: dict[str, Device] = {}

    # -- DeviceSink ------------------------------------------------------------

    def device_added(self, ident: DeviceIdentity) -> None:
        ids = f" [{ident.vid:04x}:{ident.pid:04x}]" if ident.vid is not None and ident.pid is not None else ""
        dev = self.devices.get(ident.uid)
        if dev is not None:
            if dev.removal is None:
                return  # already known and present
            if ident.serial and dev.ident.serial and ident.serial != dev.ident.serial:
                self._forget(dev)  # a different device on the same port
            else:
                dev.removal.cancel()
                dev.removal = None
                log.info("%s came back%s %s", ident.uid, ids, ident.name)
                dev.ident = ident
                dev.task = asyncio.create_task(self._bring_up(dev), name=f"bring-up {ident.uid}")
                return
        dev = Device(ident)
        self.devices[ident.uid] = dev
        log.info("detected %s%s %s", ident.uid, ids, ident.name)
        self.emit("device.added", dev.summary())
        dev.task = asyncio.create_task(self._bring_up(dev), name=f"bring-up {ident.uid}")

    def device_removed(self, uid: str) -> None:
        dev = self.devices.get(uid)
        if dev is None or dev.removal is not None:
            return
        if dev.channel is not None and dev.channel.outlives_detection:
            return  # e.g. connected BLE peripheral that stopped advertising
        dev.teardown = asyncio.create_task(self._tear_down(dev, dev.task))
        self._set(dev, DeviceStatus.DISCONNECTED, "device disconnected")
        dev.removal = asyncio.get_running_loop().call_later(self.grace_s, self._forget, dev)

    def _forget(self, dev: Device) -> None:
        if dev.removal is not None:
            dev.removal.cancel()
            dev.removal = None
        if self.devices.get(dev.ident.uid) is dev:
            del self.devices[dev.ident.uid]
            log.info("removed %s", dev.ident.uid)
            self.emit("device.removed", {"uid": dev.ident.uid})

    def device_changed(self, uid: str) -> None:
        dev = self.devices.get(uid)
        if dev is None or dev.status is DeviceStatus.READY or dev.removal is not None:
            return
        if dev.task and not dev.task.done():
            dev.changed_during_bring_up = True
        else:
            self.retry(uid, rescan=False)

    # -- lifecycle -------------------------------------------------------------

    def _set(self, dev: Device, status: DeviceStatus, error: str | None = None) -> None:
        dev.status, dev.error = status, error
        if status is not DeviceStatus.NEEDS_DRIVER:
            dev.needs = None
        self.emit("device.changed", dev.summary())

    async def _bring_up(self, dev: Device) -> None:
        dev.changed_during_bring_up = False
        if dev.teardown is not None:
            # Came back before the old connection finished closing.
            await asyncio.shield(dev.teardown)
            dev.teardown = None
        dev.candidate = dev.manifest = dev.package_dir = dev.image = None
        try:
            self._set(dev, DeviceStatus.RESOLVING)
            cand = await self.registry.resolve(dev.ident)
            if cand is None:
                log.debug("%s: no package matches", dev.ident.uid)
                self._set(dev, DeviceStatus.UNSUPPORTED)
                return
            dev.candidate = cand
            dev.manifest, dev.package_dir = await self.registry.fetch(cand)
            dev.image = export_image(dev.manifest, dev.package_dir, dev.ident, self.image_store)
            self._set(dev, DeviceStatus.CONNECTING)
            await self._connect(dev)
            self._set(dev, DeviceStatus.READY)
            log.info("%s ready with %s %s", dev.ident.uid, dev.manifest.id, dev.manifest.version)
        except asyncio.CancelledError:
            raise
        except DriverUnavailable as e:
            log.info("%s: %s", dev.ident.uid, e)
            dev.needs = e.to_json()
            self._set(dev, DeviceStatus.NEEDS_DRIVER, str(e))
        except ChannelNotReady as e:
            # Retried when the detector reports the node ready (device_changed).
            log.info("%s: waiting for the device node: %s", dev.ident.uid, e)
            await self._close_io(dev)
            self._set(dev, DeviceStatus.CONNECTING, str(e))
            if dev.changed_during_bring_up:
                asyncio.get_running_loop().call_soon(self.retry, dev.ident.uid, False)
        except TrustError as e:
            log.warning("%s blocked: %s", dev.ident.uid, e)
            self._set(dev, DeviceStatus.BLOCKED, str(e))
        except Exception as e:
            log.exception("bring-up failed for %s", dev.ident.uid)
            await self._close_io(dev)
            self._set(dev, DeviceStatus.ERROR, str(e) or type(e).__name__)

    async def _connect(self, dev: Device) -> None:
        manifest, ident = dev.manifest, dev.ident
        assert manifest and dev.package_dir
        # Resolve the driver before touching the device: a missing driver
        # should not open (or disturb) the hardware.
        cls = driver_class(manifest.driver)

        if not self._matching_rule_has_channel(manifest, ident):
            channel: Channel = NullChannel(ident, {})
        elif ident.attrs.get("simulated"):
            from borochid.service.channels.sim import SimChannel

            channel = SimChannel(ident, manifest.channel, manifest.raw.get("simulation"))
        else:
            kind = manifest.channel["type"]
            try:
                channel = plugins.load(plugins.CHANNELS, kind)(ident, manifest.channel)
            except ImportError as e:
                extra = _EXTRAS.get(kind)
                hint = f"; install borochid-service[{extra}]" if extra else ""
                raise RuntimeError(f"channel {kind!r} unavailable: {e}{hint}") from e

        uid = ident.uid

        # State that the summary shows (status line, battery): clients
        # listing devices get a new summary when it changes.
        summary_keys = {(manifest.raw.get("summary") or {}).get("state")} - {None}
        if manifest.battery:
            summary_keys |= manifest.battery.keys
        if manifest.available:
            summary_keys.add(manifest.available.state)

        def publish(changes: dict[str, Any]) -> None:
            self.emit("device.state", {"uid": uid, "changes": changes})
            if summary_keys & changes.keys() and dev.status is DeviceStatus.READY:
                self.emit("device.changed", dev.summary())

        host = Host(audio=self._audio_service(manifest, ident, publish))
        driver = cls(manifest, dev.package_dir, channel, publish, SettingsStore(self.data_dir, manifest.id, ident), host)
        channel.on_data = driver.on_data
        channel.on_closed = lambda exc: self._on_channel_closed(dev, exc)
        dev.channel, dev.driver, dev.host = channel, driver, host
        await channel.open()
        await host.start()
        await driver.start()

    @staticmethod
    def _matching_rule_has_channel(manifest: Manifest, ident: DeviceIdentity) -> bool:
        best = manifest.best_rule(ident)
        return best is None or best.channel

    def _audio_service(self, manifest: Manifest, ident: DeviceIdentity, publish) -> HostAudio | None:
        spec = manifest.raw.get("audio")
        if not spec:
            return None
        return HostAudio(
            spec["match"],
            SettingsStore(self.data_dir, manifest.id, ident, namespace="audio"),
            publish,
            self.cache_dir / "tones",
        )

    def _on_channel_closed(self, dev: Device, exc: Exception | None) -> None:
        if self.devices.get(dev.ident.uid) is not dev:
            return
        log.info("channel to %s closed: %s", dev.ident.uid, exc)
        # Stop the driver too, or its background tasks outlive the channel.
        # Usually the device is leaving the bus and udev reports its removal
        # (or its return) next, so this isn't shown as an error.
        dev.teardown = asyncio.get_running_loop().create_task(self._close_io(dev))
        self._set(dev, DeviceStatus.DISCONNECTED, "device disconnected")

    async def _close_io(self, dev: Device) -> None:
        driver, host, channel = dev.driver, dev.host, dev.channel
        dev.driver = dev.host = dev.channel = None
        # Driver first: it may still need the channel to hand the device back
        # to its firmware.
        if driver:
            with contextlib.suppress(Exception):
                await driver.stop()
        if host:
            await host.stop()
        if channel:
            with contextlib.suppress(Exception):
                await channel.close()

    async def _tear_down(self, dev: Device, task: asyncio.Task | None = None) -> None:
        # ``task`` is captured when the teardown is scheduled: by the time it
        # runs, a quick re-plug may already have started a new bring-up.
        task = task if task is not None else dev.task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._close_io(dev)

    # -- operations exposed over RPC -------------------------------------------

    async def invoke(self, uid: str, action: str, params: dict[str, Any]) -> Any:
        dev = self.devices.get(uid)
        if dev is None:
            raise KeyError(uid)
        if dev.status is not DeviceStatus.READY or dev.driver is None:
            raise RuntimeError(f"device {uid} is {dev.status}")
        if dev.host and dev.host.owns(action):
            return await dev.host.invoke(action, params)
        return await dev.driver.invoke(action, params)

    def retry(self, uid: str | None = None, rescan: bool = True) -> int:
        """Re-run bring-up for devices that are not ready, e.g. after a driver
        package was installed or the registry changed."""
        if rescan:
            plugins.rescan()
        count = 0
        for dev in list(self.devices.values()):
            if uid not in (None, dev.ident.uid) or dev.status is DeviceStatus.READY or dev.removal is not None:
                continue
            if dev.task and not dev.task.done():
                continue
            dev.task = asyncio.create_task(self._bring_up(dev))
            count += 1
        return count

    async def shutdown(self) -> None:
        for d in self.devices.values():
            if d.removal is not None:
                d.removal.cancel()
        await asyncio.gather(*(self._tear_down(d) for d in self.devices.values()))
        self.devices.clear()
