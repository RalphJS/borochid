"""A virtual input device (uinput) for drivers that remap buttons on the host.

A mouse in host mode reports a remapped button to the driver instead of to
the kernel, and the driver replays what the user bound to it through this
service: a key chord (``Super+V``), another mouse button or a wheel step.

Security rules, because this can type into the user's session:

* **No RPC actions.** Clients (the GUI, anything on the socket) can change
  *which* chord a button is bound to, as a validated driver setting, but
  nothing on the socket can make the service emit input. Only driver code
  calls ``down``/``up``, in response to a physical button event.
* **Allow-listed codes only.** Letters, digits, punctuation, function,
  navigation, modifier and media keys, mouse buttons and wheel steps; never
  power, sleep or similar system keys. At most ``MAX_CHORD`` keys.
* **Nothing typed on its own.** No text strings, no timed sequences, no
  repeats: a chord is held exactly as long as the physical button.
* **The device only exists while needed.** A driver opens it when it takes
  over buttons and closes it when it hands them back; closing releases
  anything still held.

A package opts in with ``"input": {}``. Needs python-evdev (the ``input``
extra) and write access to ``/dev/uinput``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from borochid.common.keys import ALLOWED as ALLOWED_KEYS
from borochid.common.keys import CODES, MAX_CHORD

log = logging.getLogger(__name__)

DEVICE_NAME = "Borochid virtual input"
WHEELS = {"wheel": "REL_WHEEL", "hwheel": "REL_HWHEEL"}


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class Chord:
    """What one physical button does: keys held together (pressed in order,
    released in reverse), or one wheel step."""

    keys: tuple[str, ...] = ()
    wheel: tuple[str, int] | None = None

    @classmethod
    def parse(cls, value: Any) -> Chord:
        """From a setting value: ``{"keys": ["KEY_LEFTMETA", "KEY_V"]}``,
        ``{"keys": ["BTN_SIDE"]}`` or ``{"wheel": -1}`` / ``{"hwheel": 1}``."""
        if not isinstance(value, dict) or len(value) != 1:
            raise InputError("a chord is {'keys': [...]}, {'wheel': ±1} or {'hwheel': ±1}")
        [(kind, arg)] = value.items()
        if kind == "keys":
            if not isinstance(arg, list) or not 1 <= len(arg) <= MAX_CHORD:
                raise InputError(f"keys must list 1-{MAX_CHORD} key names")
            if len(set(arg)) != len(arg):
                raise InputError("a key is listed twice")
            if bad := [k for k in arg if k not in ALLOWED_KEYS]:
                raise InputError(f"not an allowed key: {', '.join(map(str, bad))}")
            return cls(keys=tuple(arg))
        if kind in WHEELS:
            if arg not in (-1, 1) or isinstance(arg, bool):
                raise InputError(f"{kind} must be -1 or 1")
            return cls(wheel=(kind, arg))
        raise InputError(f"unknown chord kind {kind!r}")

    def to_json(self) -> dict[str, Any]:
        return {"keys": list(self.keys)} if self.keys else {self.wheel[0]: self.wheel[1]}  # type: ignore[index]


def _uinput_factory():
    import evdev
    from evdev import ecodes

    class WriteOnlyUInput(evdev.UInput):
        """After creating the device, evdev opens its /dev/input node to read
        it back, retrying with ``time.sleep`` for up to 2 s while udev sets
        permissions. The service only writes, so skip that: no wait, and no
        need to be able to read the node. evdev documents ``device`` as None
        when the node can't be opened, and handles that everywhere."""

        def _find_device(self, fd):
            return None

    def make():
        caps = {
            ecodes.EV_KEY: sorted(CODES.values()),
            ecodes.EV_REL: [ecodes.REL_WHEEL, ecodes.REL_HWHEEL],
        }
        ui = WriteOnlyUInput(caps, name=DEVICE_NAME)

        def emit(events: list[tuple[str, str, int]]) -> None:
            for kind, code, value in events:
                if kind == "EV_KEY":
                    ui.write(ecodes.EV_KEY, CODES[code], value)
                else:
                    ui.write(ecodes.EV_REL, ecodes.ecodes[code], value)
            ui.syn()

        return emit, ui.close

    return make


class HostInput:
    def __init__(self, factory: Callable[[], Callable[[], tuple[Callable, Callable]]] = _uinput_factory):
        self._factory = factory
        self._emit: Callable[[list[tuple[str, str, int]]], None] | None = None
        self._close: Callable[[], None] | None = None
        self._held: dict[Any, Chord] = {}
        self.state: dict[str, Any] = {}

    @property
    def is_open(self) -> bool:
        return self._emit is not None

    async def open(self) -> None:
        """Create the device, in a worker thread: creating a uinput device
        makes system calls that can take a while, and the event loop serves
        every other device meanwhile (a 2 s stall once made a headset miss
        its first-contact window)."""
        if self._emit is not None:
            return
        try:
            self._emit, self._close = await asyncio.to_thread(self._factory())
        except ImportError as e:
            raise InputError("python-evdev is not installed (borochid-service[input])") from e
        except OSError as e:
            raise InputError(f"cannot create a virtual input device: {e.strerror or e}") from e
        log.info("virtual input device created")

    def close(self) -> None:
        if self._emit is None:
            return
        for token in list(self._held):
            self.up(token)
        close, self._emit, self._close = self._close, None, None
        if close:
            close()
        log.info("virtual input device removed")

    def down(self, token: Any, chord: Chord) -> None:
        """Physical button ``token`` went down: press its chord (or scroll)."""
        if self._emit is None or token in self._held:
            return
        if chord.wheel:
            kind, step = chord.wheel
            self._emit([("EV_REL", WHEELS[kind], step)])
            return
        self._held[token] = chord
        self._emit([("EV_KEY", k, 1) for k in chord.keys])

    def up(self, token: Any) -> None:
        chord = self._held.pop(token, None)
        if chord is not None and self._emit is not None:
            self._emit([("EV_KEY", k, 0) for k in reversed(chord.keys)])

    # The host service protocol. Deliberately no actions: see the module docstring.
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.close()

    async def invoke(self, action: str, params: dict[str, Any]) -> Any:
        raise PermissionError("the virtual input device cannot be driven over RPC")
