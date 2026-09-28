"""Device lifecycle: detected -> resolving -> connecting -> ready.

Every device gets its own bring-up task, so a slow registry or a stuck device
never delays the others. Status and state changes are emitted as events that
the RPC server fans out to clients.

Some devices re-enumerate to change mode (a wireless dongle becomes another
product while its headset is off). A removal is therefore held for
``grace_s``: if the same device (same port, same serial) comes back in time,
it continues as the same entry instead of being removed and re-added.

A receiver's driver may announce the device behind it (``Driver.pair``):
that device has no node of its own, talks through the receiver's channel,
and goes when the receiver goes.

The user can hide a device (a receiver they never need to see, say). Hidden
devices stay in the list, flagged, so clients can offer to show them again;
the choice is kept by the device's ID, else its USB serial, else its port.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from borochid.common import images
from borochid.common.manifest import Manifest, display_name, state_key
from borochid.common.models import Bus, DeviceIdentity, DeviceStatus
from borochid.service import plugins
from borochid.service.channels import Channel, ChannelNotReady, NullChannel, PairedChannel
from borochid.service.drivers import Driver
from borochid.service.drivers.loader import DriverUnavailable, driver_class
from borochid.service.host import Host
from borochid.service.host.audio import HostAudio
from borochid.service.host.input import HostInput
from borochid.service.host.power import HostPower
from borochid.service.profiles import ProfileStore
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
    # The device's own ID: from its driver (Driver.identify), or known ahead
    # from an earlier connection with the same USB serial or Bluetooth address.
    device_id: str | None = None
    # Another connection to the same device (same device ID) is the one in
    # use: clients hide this one (see DeviceManager._resolve_twins).
    shadowed: bool = False
    # Behind a receiver, announced by the receiver's driver: its uid and the
    # channel through it.
    receiver: str | None = None
    paired_channel: PairedChannel | None = field(default=None, repr=False)
    # Hidden by the user (DeviceManager.set_hidden).
    hidden: bool = False

    def connection(self) -> str | None:
        """"wireless", "cable", "usb" or "bluetooth": from the matching package rule,
        or the bus."""
        if self.ident.bus is Bus.BLE:
            return "bluetooth"
        rule = self.manifest.best_rule(self.ident) if self.manifest else None
        return rule.connection if rule else None

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
            # Follows the service-wide profiles (profiles.list).
            "profiles": bool(self.driver and self.driver.supports_profiles and self.status is DeviceStatus.READY),
            "available": self.available(),
            "connection": self.connection(),
            # The device's own ID, once its driver read it; connections
            # sharing one are the same device.
            "device_id": self.device_id,
            "shadowed": self.shadowed,
            "hidden": self.hidden,
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
        key = state_key(value)
        return spec.get("map", {}).get(key, None if value is None else str(value))

    def state(self) -> dict[str, Any]:
        return {**(self.driver.state if self.driver else {}), **(self.host.state if self.host else {})}

    def available(self) -> bool:
        """False while a ready device can't be used (manifest ``available``),
        e.g. a dongle whose headset is switched off."""
        spec = self.manifest.available if self.manifest else None
        return spec is None or self.status is not DeviceStatus.READY or spec(self.state())

    def battery(self) -> dict[str, Any] | None:
        """``{"level": 0-100 or None, "charging": bool}`` for packages with a
        ``battery`` section, else None."""
        spec = self.manifest.battery if self.manifest else None
        if spec is None or self.status is not DeviceStatus.READY:
            return None
        state = self.state()
        level = state.get(spec.level)
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            level = None
        else:
            level = max(0, min(100, round(level)))
        return {
            "level": level,
            "charging": bool(spec.charging and state.get(spec.charging)),
        }

    def detail(self) -> dict[str, Any]:
        d = self.summary()
        d["ui"] = self.manifest.ui if self.manifest else []
        d["layouts"] = self.manifest.ui_layouts() if self.manifest else {}
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
        self.profiles = ProfileStore(self.data_dir / "profiles.json", self._profiles_changed)
        self._profile_task: asyncio.Task | None = None
        # USB serial (or Bluetooth address) -> device ID, learned when a
        # connection with that serial identifies, so it is known the moment
        # that connection appears again (no second card while its driver sets
        # up). Serials only: a port may hold another device next time.
        self._known_ids_path = self.data_dir / "device-ids.json"
        self._known_ids: dict[str, str] = self._load_known_ids()
        self._hidden_path = self.data_dir / "hidden-devices.json"
        self._hidden: set[str] = self._load_hidden()

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
        log.info("detected %s%s %s", ident.uid, ids, ident.name)
        self._add(Device(ident))

    def _add(self, dev: Device) -> None:
        ident = dev.ident
        self.devices[ident.uid] = dev
        if known := self._known_ids.get(self._serial_key(ident) or ""):
            dev.device_id = known
            self._resolve_twins(known)  # hidden from the start if another connection is in use
        dev.hidden = self._is_hidden(dev)
        self.emit("device.added", dev.summary())
        dev.task = asyncio.create_task(self._bring_up(dev), name=f"bring-up {ident.uid}")

    def device_removed(self, uid: str) -> None:
        dev = self.devices.get(uid)
        if dev is None or dev.removal is not None or dev.receiver is not None:
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
            if dev.device_id:
                self._resolve_twins(dev.device_id)

    def device_changed(self, uid: str) -> None:
        dev = self.devices.get(uid)
        if dev is None or dev.status is DeviceStatus.READY or dev.removal is not None or dev.receiver is not None:
            return
        if dev.task and not dev.task.done():
            dev.changed_during_bring_up = True
        else:
            self.retry(uid, rescan=False)

    # -- devices behind a receiver ----------------------------------------------

    def _paired_added(self, receiver: Device, slot: str, name: str, pid: int | None, shared: Any) -> PairedChannel | None:
        if receiver.channel is None or self.devices.get(receiver.ident.uid) is not receiver:
            return None
        uid = f"{receiver.ident.uid}/{slot}"
        if uid in self.devices:
            self._paired_removed(receiver, slot)
        r = receiver.ident
        # "receiver" as for the kernel's paired devices (udev detector), so
        # "paired" match rules treat both alike.
        ident = DeviceIdentity(r.bus, uid, vid=r.vid, pid=r.pid if pid is None else pid, name=name or r.name,
                               attrs={"receiver": r.attrs.get("sys_path", r.uid)})
        channel = PairedChannel(ident, receiver.channel, shared)
        log.info("%s: %s paired", r.uid, uid)
        self._add(Device(ident, receiver=r.uid, paired_channel=channel))
        return channel

    def _paired_removed(self, receiver: Device, slot: str) -> None:
        dev = self.devices.get(f"{receiver.ident.uid}/{slot}")
        if dev is None or dev.receiver != receiver.ident.uid:
            return
        dev.teardown = asyncio.get_running_loop().create_task(self._tear_down(dev))
        self._forget(dev)

    async def _remove_paired(self, receiver: Device) -> None:
        """Before the receiver's own driver stops: a paired device's driver
        may still need the receiver's channel to hand the device back."""
        for dev in [d for d in self.devices.values() if d.receiver == receiver.ident.uid]:
            self._paired_removed(receiver, dev.ident.uid.rsplit("/", 1)[1])
            if dev.teardown is not None:
                await dev.teardown

    # -- hidden devices ----------------------------------------------------------

    def _load_hidden(self) -> set[str]:
        try:
            raw = json.loads(self._hidden_path.read_text())
        except (OSError, ValueError):
            return set()
        return {k for k in raw if isinstance(k, str)} if isinstance(raw, list) else set()

    @staticmethod
    def _hide_keys(dev: Device) -> list[str]:
        """Most lasting first: the device's own ID follows it everywhere; a
        USB serial survives a re-enumeration as another product; a port is
        all there is otherwise."""
        ident, keys = dev.ident, []
        if dev.device_id:
            keys.append(f"id:{dev.device_id}")
        if ident.serial and ident.vid is not None:
            keys.append(f"{ident.bus}:{ident.vid:04x}:{ident.serial}")
        keys.append(f"uid:{ident.uid}")
        return keys

    def _is_hidden(self, dev: Device) -> bool:
        return any(k in self._hidden for k in self._hide_keys(dev))

    def set_hidden(self, uid: str, hidden: bool) -> Device:
        dev = self.devices[uid]
        keys = self._hide_keys(dev)
        if hidden:
            self._hidden.add(keys[0])
        else:
            self._hidden.difference_update(keys)
        try:
            self._hidden_path.parent.mkdir(parents=True, exist_ok=True)
            self._hidden_path.write_text(json.dumps(sorted(self._hidden), indent=1))
        except OSError as e:
            log.warning("can't remember hidden devices: %s", e)
        self._refresh_hidden()
        return dev

    def _refresh_hidden(self) -> None:
        # Every connection of a device hidden by its ID follows.
        for d in self.devices.values():
            if d.hidden != (hidden := self._is_hidden(d)):
                d.hidden = hidden
                self.emit("device.changed", d.summary())

    # -- lifecycle -------------------------------------------------------------

    def _set(self, dev: Device, status: DeviceStatus, error: str | None = None) -> None:
        dev.status, dev.error = status, error
        if status is not DeviceStatus.NEEDS_DRIVER:
            dev.needs = None
        self.emit("device.changed", dev.summary())
        if dev.device_id:
            self._resolve_twins(dev.device_id)

    # -- one device, several connections ---------------------------------------

    @staticmethod
    def _serial_key(ident: DeviceIdentity) -> str | None:
        if ident.bus is Bus.BLE:
            # No serial, but the address BlueZ keeps the pairing under: a
            # paired keyboard in use on its receiver is reported at start,
            # disconnected, and must not show as a second device.
            address = ident.attrs.get("address")
            return f"ble:{str(address).upper()}" if address else None
        if not ident.serial or ident.vid is None or ident.pid is None:
            return None
        return f"{ident.bus}:{ident.vid:04x}:{ident.pid:04x}:{ident.serial}"

    def _load_known_ids(self) -> dict[str, str]:
        try:
            raw = json.loads(self._known_ids_path.read_text())
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(raw, dict) else {}

    def _identified(self, dev: Device, device_id: str) -> None:
        dev.device_id = device_id
        if (key := self._serial_key(dev.ident)) and self._known_ids.get(key) != device_id:
            self._known_ids[key] = device_id
            try:
                self._known_ids_path.parent.mkdir(parents=True, exist_ok=True)
                self._known_ids_path.write_text(json.dumps(self._known_ids, indent=1, sort_keys=True))
            except OSError as e:
                log.warning("can't remember device IDs: %s", e)
        dev.hidden = self._is_hidden(dev)
        self.emit("device.changed", dev.summary())
        self._resolve_twins(device_id)
        self._refresh_hidden()

    _PREFERRED = {"cable": 0, "usb": 0, "wireless": 1, "bluetooth": 2}

    def _resolve_twins(self, device_id: str) -> None:
        """Connections to one device (receiver and cable, say): the one in
        use shows and owns the settings, the others are hidden and passive.
        In use: ready and available first, then cable over wireless over
        Bluetooth. One that becomes active reloads the settings, which its
        twin may have changed meanwhile."""
        twins = [d for d in self.devices.values() if d.device_id == device_id]
        if not twins:
            return
        # A connection that just dropped (kept a moment in case it comes back)
        # is never the one in use while another is still there.
        active = min(twins, key=lambda d: (d.removal is not None, d.status is not DeviceStatus.READY, not d.available(),
                                           self._PREFERRED.get(d.connection() or "", 3), d.ident.uid))
        for d in twins:
            shadowed = d is not active
            if d.driver is not None:  # (none yet while it is being brought up: see _bring_up)
                if d.driver.passive and not shadowed:
                    task = asyncio.get_running_loop().create_task(d.driver.reload_settings())
                    task.add_done_callback(lambda t, uid=d.ident.uid: t.cancelled() or t.exception() is None
                                           or log.warning("%s: reloading settings failed: %s", uid, t.exception()))
                d.driver.passive = shadowed
            if d.shadowed != shadowed:
                d.shadowed = shadowed
                log.info("%s: %s", d.ident.uid, "another connection is in use" if shadowed else "in use")
                self.emit("device.changed", d.summary())

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

        if dev.paired_channel is not None:
            channel: Channel = dev.paired_channel
        elif not self._matching_rule_has_channel(manifest, ident):
            channel = NullChannel(ident, {})
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
                if dev.device_id:  # e.g. asleep: its twin may take over
                    self._resolve_twins(dev.device_id)

        host = Host(
            audio=self._audio_service(manifest, ident, publish),
            power=self._power_service(manifest, ident, publish),
            input=HostInput() if "input" in manifest.raw else None,
        )
        driver = cls(manifest, dev.package_dir, channel, publish, SettingsStore(self.data_dir, manifest.id, ident), host)
        driver.on_identify = lambda device_id: self._identified(dev, device_id)
        driver.on_pair = lambda slot, name, pid, shared: self._paired_added(dev, slot, name, pid, shared)
        driver.on_unpair = lambda slot: self._paired_removed(dev, slot)
        driver.passive = dev.shadowed  # known ahead to be a second connection: not the settings' owner yet
        channel.on_data = driver.on_data
        channel.on_closed = lambda exc: self._on_channel_closed(dev, exc)
        dev.channel, dev.driver, dev.host = channel, driver, host
        await channel.open()
        await host.start()
        if driver.supports_profiles:
            await driver.use_profile(self.profiles.current, self.profiles.ids)
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

    @staticmethod
    def _power_service(manifest: Manifest, ident: DeviceIdentity, publish) -> HostPower | None:
        if "power_supply" not in manifest.raw or not (sys_path := ident.attrs.get("sys_path")):
            return None
        return HostPower(Path(sys_path), publish)

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
        await self._remove_paired(dev)
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

    # -- profiles ----------------------------------------------------------------

    def _profiles_changed(self) -> None:
        self.emit("profiles.changed", self.profiles.snapshot())
        # One switch at a time, in order: a quick A -> B -> A must end on A.
        previous = self._profile_task
        self._profile_task = asyncio.get_running_loop().create_task(self._apply_profiles(previous))

    async def _apply_profiles(self, previous: asyncio.Task | None) -> None:
        if previous is not None:
            with contextlib.suppress(Exception):
                await previous
        profile, known = self.profiles.current, self.profiles.ids
        for dev in list(self.devices.values()):
            driver = dev.driver
            if driver is None or not driver.supports_profiles or dev.status is not DeviceStatus.READY:
                continue
            try:
                await driver.use_profile(profile, known)
            except Exception:
                log.exception("%s: switching to profile %r failed", dev.ident.uid, profile.name)

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
        # Paired devices go with their receivers, first.
        await asyncio.gather(*(self._tear_down(d) for d in list(self.devices.values()) if d.receiver is None))
        self.devices.clear()
