"""Finds the driver plugin a device package asks for.

Drivers are never downloaded. They are Python plugins (entry point group
``borochid.drivers``) installed through the system package manager, or built
into the service like ``declarative``.
"""

from __future__ import annotations

from dataclasses import dataclass

from borochid.common.manifest import DriverRef, ManifestError
from borochid.service import plugins
from borochid.service.drivers import Driver, DriverError


@dataclass
class DriverUnavailable(Exception):
    driver: str
    spec: str
    provided_by: str | None
    installed: str | None = None

    def __str__(self) -> str:
        need = f"{self.driver} {self.spec}".strip()
        have = f" (installed: {self.installed})" if self.installed else ""
        where = f"; install {self.provided_by}" if self.provided_by else ""
        return f"driver {need} is not available{have}{where}"

    def to_json(self) -> dict:
        return {"driver": self.driver, "version": self.spec, "provided_by": self.provided_by, "installed": self.installed}


def driver_class(ref: DriverRef) -> type[Driver]:
    unavailable = DriverUnavailable(ref.type, ref.version.spec, ref.provided_by)
    if ref.type not in plugins.available(plugins.DRIVERS):
        raise unavailable
    installed = plugins.version(plugins.DRIVERS, ref.type)
    # Built-in drivers ship with the service and have no separate version.
    if installed and ref.version.spec:
        try:
            ok = ref.version.allows(installed)
        except ManifestError:
            ok = False
        if not ok:
            unavailable.installed = installed
            raise unavailable
    try:
        cls = plugins.load(plugins.DRIVERS, ref.type)
    except ImportError as e:
        raise DriverError(f"driver {ref.type!r} failed to import: {e}") from e
    if not (isinstance(cls, type) and issubclass(cls, Driver)):
        raise DriverError(f"{cls!r} is not a Driver subclass")
    return cls
