"""Headset audio on the host: ALSA mixer (volume, sidetone), PipeWire mic
mute and the mute feedback tone.

The device's sound card and PipeWire nodes are found by the names listed in
the manifest. Names must be specific: a generic term like "gaming" once
matched a "G560 Gaming Speaker" on a lower card index and sent every mixer
command to it. When nothing matches, operations fail loudly rather than
guessing a card.

Mute is applied at the PipeWire layer, which is what the desktop itself
mutes; the ALSA capture switch sits underneath and makes the mic look dead.
Toggling reads the current PipeWire state first, so mutes made from the
desktop are never fought.

The mute state is followed through ``pactl subscribe``: PipeWire reports
when sources appear, disappear or change, so a mic that shows up after the
device (re)enumerates, or a mute from the desktop, is picked up when it
happens. Between events the watcher sleeps; nothing is polled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import struct
import wave
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MIN_NAME_LEN = 4
_EVENT_RE = re.compile(r"^Event '(new|change|remove)' on source #(\d+)")


async def pactl_subscribe() -> AsyncIterator[str]:
    """Lines from ``pactl subscribe`` until it exits or is cancelled."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "subscribe", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except FileNotFoundError:
        raise AudioError("pactl is not installed") from None
    try:
        assert proc.stdout
        while line := await proc.stdout.readline():
            yield line.decode(errors="replace").strip()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


class AudioError(Exception):
    pass


async def run(*argv: str, timeout: float = 5) -> tuple[int, str]:
    """Run a fixed tool with argv (never a shell). Raises AudioError on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        raise AudioError(f"{argv[0]} is not installed") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise AudioError(f"{argv[0]} timed out") from None
    if proc.returncode != 0:
        raise AudioError(f"{argv[0]} failed: {err.decode(errors='replace').strip()}")
    return proc.returncode, out.decode(errors="replace")


def _percent(value: Any) -> int:
    v = int(value)
    if not 0 <= v <= 100:
        raise AudioError("value must be 0-100")
    return v


class HostAudio:
    # name -> (validator, default). None defaults mean "never touch the host
    # setting until the user sets it", so plugging a device in does not
    # silently change someone's mixer.
    SETTINGS: dict[str, tuple[Callable[[Any], Any], Any]] = {
        "volume": (_percent, None),
        "sidetone": (_percent, None),
        "mute_tone": (bool, True),
        "sidetone_off_on_exit": (bool, True),
    }

    def __init__(
        self,
        names: list[str],
        store: Any,
        publish: Callable[[dict[str, Any]], None],
        cache_dir: Path,
        cards_file: Path = Path("/proc/asound/cards"),
        runner: Callable[..., Any] = run,
        subscribe: Callable[[], AsyncIterator[str]] = pactl_subscribe,
    ):
        names = [n.lower() for n in names]
        if not names or any(len(n) < MIN_NAME_LEN for n in names):
            raise ValueError(f"audio.match needs specific names of at least {MIN_NAME_LEN} characters")
        self.names = names
        self._store = store
        self._publish = publish
        self.cache_dir = cache_dir
        self.cards_file = cards_file
        self._run = runner
        self._subscribe = subscribe
        self._watcher: asyncio.Task | None = None
        self._source_index: str | None = None
        self._resync_pending = False
        self._resyncing = False
        self.settings: dict[str, Any] = store.load()
        self.state: dict[str, Any] = {}
        self._listeners: list[Callable[[bool], Any]] = []
        self._mute_lock = asyncio.Lock()

    def _set(self, **changes: Any) -> None:
        prefixed = {f"audio.{k}": v for k, v in changes.items()}
        self.state.update(prefixed)
        self._publish(prefixed)

    def on_mic_muted(self, listener: Callable[[bool], Any]) -> None:
        """Register a callback (sync or async) for mute changes, e.g. to recolour an LED."""
        self._listeners.append(listener)

    @property
    def mic_muted(self) -> bool:
        return bool(self.state.get("audio.mic_muted"))

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        self._set(**{k: self.settings.get(k, default) for k, (_, default) in self.SETTINGS.items()})
        await self._resync()
        self._watcher = asyncio.create_task(self._watch())
        await self._restore()

    async def identify(self, device_id: str) -> None:
        """Keep settings under the device's own ID; apply them if they differ."""
        if self._store.rekey(device_id):
            self.settings = self._store.load()
            self._set(**{k: self.settings.get(k, default) for k, (_, default) in self.SETTINGS.items()})
            await self._restore()

    async def _restore(self) -> None:
        for key, apply in (("volume", self._apply_volume), ("sidetone", self._apply_sidetone)):
            if self.settings.get(key) is not None:
                try:
                    await apply(self.settings[key])
                except AudioError as e:
                    log.warning("could not restore %s: %s", key, e)

    async def _watch(self) -> None:
        try:
            async for line in self._subscribe():
                m = _EVENT_RE.match(line)
                if not m:
                    continue
                kind, index = m.groups()
                # A new or removed source may be ours arriving or leaving;
                # a change on ours may be a mute from the desktop.
                if kind != "change" or index == self._source_index:
                    await self._resync()
        except AudioError as e:
            log.info("not following mic mute changes: %s", e)

    async def _resync(self) -> None:
        """Re-read which source is ours and its mute state. Coalesces bursts
        of events (a volume drag emits many) into at most one extra read."""
        if self._resyncing:
            self._resync_pending = True
            return
        self._resyncing = True
        try:
            while True:
                self._resync_pending = False
                try:
                    self._source_index, name = await self._find("sources")
                    muted = await self._read_mic_muted(name)
                except AudioError as e:
                    self._source_index, muted = None, None
                    log.debug("mic mute state unavailable: %s", e)
                known = "audio.mic_muted" in self.state
                if not known or muted != self.state["audio.mic_muted"]:
                    self._set(mic_muted=muted)
                    # Listeners hear about changes, not the first reading.
                    if known and muted is not None:
                        await self._notify(muted)
                if not self._resync_pending:
                    return
        finally:
            self._resyncing = False

    async def _notify(self, muted: bool) -> None:
        for listener in self._listeners:
            result = listener(muted)
            if asyncio.iscoroutine(result):
                await result

    async def stop(self) -> None:
        if self._watcher:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
        if self.settings.get("sidetone_off_on_exit", True) and self.settings.get("sidetone"):
            await self._apply_sidetone(0)

    # -- actions ---------------------------------------------------------------

    async def invoke(self, name: str, params: dict[str, Any]) -> Any:
        if name == "toggle_mic_mute":
            return await self.toggle_mic_mute()
        if name == "set_mic_muted":
            return await self.set_mic_muted(bool(params.get("value")))
        key = name.removeprefix("set_")
        if not name.startswith("set_") or key not in self.SETTINGS:
            raise AudioError(f"unknown audio action {name!r}")
        value = self.SETTINGS[key][0](params.get("value"))
        if key == "volume":
            await self._apply_volume(value)
        elif key == "sidetone":
            await self._apply_sidetone(value)
        self.settings[key] = value
        self._store.save(self.settings)
        self._set(**{key: value})
        return value

    async def toggle_mic_mute(self) -> bool:
        async with self._mute_lock:
            try:
                current = await self._read_mic_muted((await self._find("sources"))[1])
            except AudioError:
                current = self.mic_muted
            return await self._set_mic_muted_locked(not current)

    async def set_mic_muted(self, muted: bool) -> bool:
        async with self._mute_lock:
            return await self._set_mic_muted_locked(muted)

    async def _set_mic_muted_locked(self, muted: bool) -> bool:
        _, name = await self._find("sources")
        await self._run("pactl", "set-source-mute", name, "1" if muted else "0")
        self._set(mic_muted=muted)
        await self._notify(muted)
        if self.settings.get("mute_tone", True):
            await self._play_tone(muted)
        return muted

    # -- ALSA ------------------------------------------------------------------

    def _card(self) -> str:
        try:
            # " 4 [Ga  ]: USB-Audio - CORSAIR VIRTUOSO SE Wireless Ga"
            for line in self.cards_file.read_text().splitlines():
                fields = line.split()
                if fields and fields[0].isdigit() and self._ours(line):
                    return fields[0]
        except OSError:
            pass
        raise AudioError(f"no sound card matching {self.names}")

    def _ours(self, text: str) -> bool:
        low = text.lower()
        return any(n in low for n in self.names)

    async def _apply_volume(self, level: int) -> None:
        await self._run("amixer", "-c", self._card(), "sset", "Headset", f"{level}%")

    async def _apply_sidetone(self, level: int) -> None:
        if level == 0:
            await self._run("amixer", "-c", self._card(), "sset", "Sidetone", "mute")
        else:
            # "unmute" too: setting a level on a muted control leaves it muted.
            await self._run("amixer", "-c", self._card(), "sset", "Sidetone", f"{level}%", "unmute")

    # -- PipeWire --------------------------------------------------------------

    async def _find(self, kind: str) -> tuple[str, str]:
        """(index, name) of our sink or source."""
        _, out = await self._run("pactl", "list", kind, "short")
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) > 1 and self._ours(parts[1]) and not parts[1].endswith(".monitor"):
                return parts[0], parts[1]
        raise AudioError(f"no PipeWire {kind[:-1]} matching {self.names}")

    async def _read_mic_muted(self, source: str) -> bool:
        _, out = await self._run("pactl", "get-source-mute", source)
        return "yes" in out.lower()

    async def _play_tone(self, muted: bool) -> None:
        """In software-controlled mute the headset no longer beeps, so the host
        supplies the cue. Descending for mute, ascending for unmute."""
        path = self.cache_dir / ("mic-muted.wav" if muted else "mic-active.wav")
        if not path.exists():
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            _write_tone(path, [880, 494] if muted else [494, 880])
        try:
            _, sink = await self._find("sinks")
            await self._run("paplay", f"--device={sink}", str(path))
        except AudioError as e:
            log.info("feedback tone skipped: %s", e)


def _write_tone(path: Path, freqs: list[int], ms: int = 110, rate: int = 44100, volume: float = 0.30) -> None:
    n = int(rate * ms / 1000)
    fade = max(1, int(rate * 0.006))  # ramp in and out, otherwise it clicks
    frames = bytearray()
    for freq in freqs:
        for i in range(n):
            env = min(1.0, i / fade, (n - i) / fade)
            s = int(volume * env * 32767 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<hh", s, s)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
