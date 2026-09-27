"""Simulated devices for developing packages and the GUI without hardware.

Enable with ``detectors = ["sim"]`` (or ``borochid-service --sim``). Devices are
taken from the ``devices`` option, defaulting to one that matches the bundled
example package. Simulated identities look like real USB devices but carry
``attrs["simulated"]`` so the device manager wires them to the sim channel.
"""

from __future__ import annotations

from borochid.common.models import Bus, DeviceIdentity, parse_int
from borochid.service.detectors import Detector

DEFAULT_DEVICES = [{"vid": "0x1209", "pid": "0xb0c1", "name": "Simulated Macropad"}]


class SimDetector(Detector):
    async def start(self) -> None:
        for i, d in enumerate(self.options.get("devices", DEFAULT_DEVICES)):
            self.sink.device_added(
                DeviceIdentity(
                    bus=Bus.USB,
                    uid=f"sim:{i}",
                    vid=parse_int(d["vid"]),
                    pid=parse_int(d["pid"]),
                    name=d.get("name", f"Simulated device {i}"),
                    serial=f"SIM{i:04d}",
                    attrs={"simulated": True},
                )
            )

    async def stop(self) -> None:
        pass
