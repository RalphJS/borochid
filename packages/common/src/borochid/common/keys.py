"""The keys a remapped button may send, shared by the service (which
validates and emits them) and the GUI (which captures and shows them).

Names and codes are Linux input event codes (``linux/input-event-codes.h``),
which are stable ABI. Deliberately missing: power, sleep, wake, suspend,
screen lock and other system keys, so a button binding can never shut the
machine down or lock the session.
"""

from __future__ import annotations

import string

MAX_CHORD = 6

MODIFIERS = {
    "KEY_LEFTCTRL": 29, "KEY_LEFTSHIFT": 42, "KEY_LEFTALT": 56, "KEY_LEFTMETA": 125,
    "KEY_RIGHTCTRL": 97, "KEY_RIGHTSHIFT": 54, "KEY_RIGHTALT": 100, "KEY_RIGHTMETA": 126,
}  # fmt: skip

_LETTERS = dict(zip(string.ascii_uppercase, (30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44)))
_DIGITS = {str(d): (11 if d == 0 else d + 1) for d in range(10)}
_F = {n: (58 + n if n <= 10 else 76 + n if n <= 12 else 170 + n) for n in range(1, 25)}

CODES: dict[str, int] = {
    **{f"KEY_{c}": code for c, code in _LETTERS.items()},
    **{f"KEY_{d}": code for d, code in _DIGITS.items()},
    **{f"KEY_F{n}": code for n, code in _F.items()},
    **MODIFIERS,
    "KEY_ESC": 1, "KEY_MINUS": 12, "KEY_EQUAL": 13, "KEY_BACKSPACE": 14, "KEY_TAB": 15,
    "KEY_LEFTBRACE": 26, "KEY_RIGHTBRACE": 27, "KEY_ENTER": 28, "KEY_SEMICOLON": 39,
    "KEY_APOSTROPHE": 40, "KEY_GRAVE": 41, "KEY_BACKSLASH": 43, "KEY_COMMA": 51, "KEY_DOT": 52,
    "KEY_SLASH": 53, "KEY_KPASTERISK": 55, "KEY_SPACE": 57, "KEY_CAPSLOCK": 58, "KEY_NUMLOCK": 69,
    "KEY_SCROLLLOCK": 70, "KEY_KP7": 71, "KEY_KP8": 72, "KEY_KP9": 73, "KEY_KPMINUS": 74,
    "KEY_KP4": 75, "KEY_KP5": 76, "KEY_KP6": 77, "KEY_KPPLUS": 78, "KEY_KP1": 79, "KEY_KP2": 80,
    "KEY_KP3": 81, "KEY_KP0": 82, "KEY_KPDOT": 83, "KEY_KPENTER": 96, "KEY_KPSLASH": 98,
    "KEY_SYSRQ": 99, "KEY_HOME": 102, "KEY_UP": 103, "KEY_PAGEUP": 104, "KEY_LEFT": 105,
    "KEY_RIGHT": 106, "KEY_END": 107, "KEY_DOWN": 108, "KEY_PAGEDOWN": 109, "KEY_INSERT": 110,
    "KEY_DELETE": 111, "KEY_MUTE": 113, "KEY_VOLUMEDOWN": 114, "KEY_VOLUMEUP": 115, "KEY_PAUSE": 119,
    "KEY_COMPOSE": 127, "KEY_UNDO": 131, "KEY_COPY": 133, "KEY_PASTE": 135, "KEY_FIND": 136,
    "KEY_CUT": 137, "KEY_CALC": 140, "KEY_WWW": 150, "KEY_MAIL": 155, "KEY_BACK": 158,
    "KEY_FORWARD": 159, "KEY_NEXTSONG": 163, "KEY_PLAYPAUSE": 164, "KEY_PREVIOUSSONG": 165,
    "KEY_STOPCD": 166, "KEY_REFRESH": 173, "KEY_REDO": 182, "KEY_BRIGHTNESSDOWN": 224,
    "KEY_BRIGHTNESSUP": 225, "KEY_ZOOMIN": 0x1A2, "KEY_ZOOMOUT": 0x1A3,
    "BTN_LEFT": 0x110, "BTN_RIGHT": 0x111, "BTN_MIDDLE": 0x112, "BTN_SIDE": 0x113,
    "BTN_EXTRA": 0x114, "BTN_FORWARD": 0x115, "BTN_BACK": 0x116, "BTN_TASK": 0x117,
}  # fmt: skip

ALLOWED = frozenset(CODES)
BY_CODE = {code: name for name, code in CODES.items()}

_LABELS = {
    "KEY_LEFTCTRL": "Ctrl", "KEY_RIGHTCTRL": "Right Ctrl", "KEY_LEFTSHIFT": "Shift",
    "KEY_RIGHTSHIFT": "Right Shift", "KEY_LEFTALT": "Alt", "KEY_RIGHTALT": "AltGr",
    "KEY_LEFTMETA": "Super", "KEY_RIGHTMETA": "Right Super", "KEY_ESC": "Esc",
    "KEY_SYSRQ": "Print", "KEY_PAGEUP": "Page Up", "KEY_PAGEDOWN": "Page Down",
    "KEY_MINUS": "-", "KEY_EQUAL": "=", "KEY_LEFTBRACE": "[", "KEY_RIGHTBRACE": "]",
    "KEY_SEMICOLON": ";", "KEY_APOSTROPHE": "'", "KEY_GRAVE": "`", "KEY_BACKSLASH": "\\",
    "KEY_COMMA": ",", "KEY_DOT": ".", "KEY_SLASH": "/", "KEY_PLAYPAUSE": "Play/Pause",
    "KEY_NEXTSONG": "Next track", "KEY_PREVIOUSSONG": "Previous track", "KEY_STOPCD": "Stop",
    "KEY_VOLUMEUP": "Volume up", "KEY_VOLUMEDOWN": "Volume down",
    "BTN_LEFT": "Left click", "BTN_RIGHT": "Right click", "BTN_MIDDLE": "Middle click",
    "BTN_SIDE": "Back button", "BTN_EXTRA": "Forward button",
    "KEY_SCROLLLOCK": "Scroll Lock", "KEY_CAPSLOCK": "Caps Lock", "KEY_NUMLOCK": "Num Lock",
    "KEY_BACKSPACE": "Backspace", "KEY_WWW": "Browser", "KEY_CALC": "Calculator",
    "KEY_ZOOMIN": "Zoom in", "KEY_ZOOMOUT": "Zoom out", "KEY_BRIGHTNESSUP": "Brightness up",
    "KEY_BRIGHTNESSDOWN": "Brightness down", "KEY_BACK": "Browser back", "KEY_FORWARD": "Browser forward",
    "KEY_KPPLUS": "Keypad +", "KEY_KPMINUS": "Keypad -", "KEY_KPASTERISK": "Keypad *",
    "KEY_KPSLASH": "Keypad /", "KEY_KPDOT": "Keypad .", "KEY_KPENTER": "Keypad Enter",
    **{f"KEY_KP{d}": f"Keypad {d}" for d in range(10)},
}  # fmt: skip

# Every key a button may send, grouped for pickers (modifiers and mouse
# buttons are offered separately). Desktops keep some keys for themselves
# (Print on KDE and GNOME), so a picker is the only way to choose them.
GROUPS: list[tuple[str, list[str]]] = [
    ("System", ["KEY_SYSRQ", "KEY_ESC", "KEY_PAUSE", "KEY_SCROLLLOCK", "KEY_CAPSLOCK", "KEY_NUMLOCK", "KEY_COMPOSE"]),
    ("Function keys", [f"KEY_F{n}" for n in range(1, 25)]),
    ("Media", ["KEY_PLAYPAUSE", "KEY_STOPCD", "KEY_NEXTSONG", "KEY_PREVIOUSSONG", "KEY_VOLUMEUP", "KEY_VOLUMEDOWN", "KEY_MUTE", "KEY_BRIGHTNESSUP", "KEY_BRIGHTNESSDOWN"]),
    ("Navigation", ["KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_HOME", "KEY_END", "KEY_PAGEUP", "KEY_PAGEDOWN"]),
    ("Editing", ["KEY_ENTER", "KEY_TAB", "KEY_SPACE", "KEY_BACKSPACE", "KEY_INSERT", "KEY_DELETE", "KEY_UNDO", "KEY_REDO", "KEY_CUT", "KEY_COPY", "KEY_PASTE", "KEY_FIND"]),
    ("Browser and apps", ["KEY_BACK", "KEY_FORWARD", "KEY_REFRESH", "KEY_ZOOMIN", "KEY_ZOOMOUT", "KEY_WWW", "KEY_MAIL", "KEY_CALC"]),
    ("Letters", [f"KEY_{c}" for c in string.ascii_uppercase]),
    ("Digits", [f"KEY_{d}" for d in "1234567890"]),
    ("Symbols", ["KEY_MINUS", "KEY_EQUAL", "KEY_LEFTBRACE", "KEY_RIGHTBRACE", "KEY_SEMICOLON", "KEY_APOSTROPHE", "KEY_GRAVE", "KEY_BACKSLASH", "KEY_COMMA", "KEY_DOT", "KEY_SLASH"]),
    ("Keypad", [f"KEY_KP{d}" for d in "1234567890"] + ["KEY_KPPLUS", "KEY_KPMINUS", "KEY_KPASTERISK", "KEY_KPSLASH", "KEY_KPDOT", "KEY_KPENTER"]),
]


def label(name: str) -> str:
    if name in _LABELS:
        return _LABELS[name]
    base = name.removeprefix("KEY_").removeprefix("BTN_")
    return base if len(base) <= 3 else base.title()


def chord_label(keys: list[str] | tuple[str, ...]) -> str:
    """``["KEY_LEFTMETA", "KEY_V"]`` -> ``"Super+V"``."""
    return "+".join(label(k) for k in keys)
