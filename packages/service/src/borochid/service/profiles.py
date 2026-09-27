"""Profiles shared by every device that supports them.

A profile ("Work", "Gaming") is a name the service owns; each device keeps
its own settings for it (a mouse its DPI and buttons, a headset its EQ).
Selecting a profile switches every such device at once, and a device that
connects later starts in the active profile.

Stored in ``<data_dir>/profiles.json``::

    {"profiles": [{"id": "default", "name": "Default"},
                  {"id": "p3f9a1c2e", "name": "Gaming", "copy_of": "default"}],
     "active": "p3f9a1c2e"}

Ids are stable, so renaming touches only this file. ``copy_of`` records
what a duplicated profile was copied from: a device that was unplugged
when it was duplicated still starts from the right settings.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_PROFILES = 32
MAX_NAME = 32
DEFAULT_ID = "default"


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    copy_of: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.copy_of:
            out["copy_of"] = self.copy_of
        return out


def clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ProfileError("a profile name is text")
    cleaned = " ".join("".join(c for c in value if c.isprintable()).split())
    if not 1 <= len(cleaned) <= MAX_NAME:
        raise ProfileError(f"a profile name has 1-{MAX_NAME} characters")
    return cleaned


class ProfileStore:
    def __init__(self, path: Path, on_change: Callable[[], None] = lambda: None):
        self.path = path
        self.on_change = on_change
        self.items: list[Profile] = [Profile(DEFAULT_ID, "Default")]
        self.active = DEFAULT_ID
        self._load()

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as e:
            log.warning("ignoring unreadable profiles %s: %s", self.path, e)
            return
        items, seen = [], set()
        for p in raw.get("profiles", [])[:MAX_PROFILES] if isinstance(raw, dict) else []:
            try:
                pid = p["id"]
                if not isinstance(pid, str) or not pid or pid in seen or len(pid) > 32:
                    raise ProfileError(f"bad id {pid!r}")
                copy_of = p.get("copy_of") if isinstance(p.get("copy_of"), str) else None
                items.append(Profile(pid, clean_name(p["name"]), copy_of))
                seen.add(pid)
            except (KeyError, TypeError, ProfileError) as e:
                log.warning("ignoring stored profile %r: %s", p, e)
        if items:
            self.items = items
            self.active = raw.get("active") if raw.get("active") in seen else items[0].id

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.snapshot(), indent=1))
        os.replace(tmp, self.path)

    def _changed(self) -> None:
        self._save()
        self.on_change()

    # -- queries -----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {"profiles": [p.to_json() for p in self.items], "active": self.active}

    def get(self, pid: str) -> Profile:
        for p in self.items:
            if p.id == pid:
                return p
        raise ProfileError("no such profile")

    @property
    def current(self) -> Profile:
        return self.get(self.active)

    @property
    def ids(self) -> set[str]:
        return {p.id for p in self.items}

    # -- edits -------------------------------------------------------------------

    def select(self, pid: str) -> Profile:
        self.get(pid)
        if pid != self.active:
            self.active = pid
            self._changed()
        return self.current

    def add(self, name: Any, duplicate: bool = False) -> Profile:
        """A new profile, made active. Devices start it from their defaults,
        or from their settings for the current profile with ``duplicate``."""
        if len(self.items) >= MAX_PROFILES:
            raise ProfileError(f"at most {MAX_PROFILES} profiles")
        clean = clean_name(name)
        if any(p.name == clean for p in self.items):
            raise ProfileError(f"there is already a profile called {clean!r}")
        pid = "p" + secrets.token_hex(4)
        profile = Profile(pid, clean, self.active if duplicate else None)
        self.items.append(profile)
        self.active = pid
        self._changed()
        return profile

    def rename(self, pid: str, name: Any) -> None:
        old = self.get(pid)
        clean = clean_name(name)
        if any(p.name == clean and p.id != pid for p in self.items):
            raise ProfileError(f"there is already a profile called {clean!r}")
        self.items[self.items.index(old)] = Profile(pid, clean, old.copy_of)
        self._changed()

    def remove(self, pid: str) -> None:
        old = self.get(pid)
        if len(self.items) == 1:
            raise ProfileError("the last profile can't be deleted")
        i = self.items.index(old)
        del self.items[i]
        # Profiles copied from this one keep working: devices fall back to defaults.
        self.items = [Profile(p.id, p.name, None if p.copy_of == pid else p.copy_of) for p in self.items]
        if self.active == pid:
            self.active = self.items[max(i - 1, 0)].id
        self._changed()
