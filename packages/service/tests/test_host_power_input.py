import asyncio
from types import SimpleNamespace

import pytest

from borochid.common.models import Bus
from borochid.service.detectors.udev import paired_identity
from borochid.service.host import Host
from borochid.service.host.input import Chord, HostInput, InputError
from borochid.service.host.power import HostPower

HID = "/sys/devices/pci0000:00/usb1/1-2/1-2.3/1-2.3:1.2/0003:046D:C547.0008/0003:046D:409F.000B"


def fake_hid(parent_subsystem="hid", phys="usb-0000:08:00.1-2.3/input2:1"):
    usb = SimpleNamespace(sys_name="1-2.3", sys_path="/sys/devices/pci0000:00/usb1/1-2/1-2.3")
    props = {"HID_ID": "0003:0000046D:0000409F", "HID_NAME": "Logitech G502 X LS", "HID_PHYS": phys, "HID_UNIQ": "01-ab-09-45"}
    return SimpleNamespace(
        sys_path=HID,
        parent=SimpleNamespace(subsystem=parent_subsystem),
        properties=props,
        find_parent=lambda subsystem, devtype=None: usb if (subsystem, devtype) == ("usb", "usb_device") else None,
    )


# -- paired devices ------------------------------------------------------------


def test_device_on_a_receiver_gets_its_own_identity():
    ident = paired_identity(fake_hid())
    assert ident.bus is Bus.USB
    assert (ident.uid, ident.vid, ident.pid, ident.serial) == ("usb:1-2.3/1", 0x046D, 0x409F, "01-ab-09-45")
    # The channel opens the node under the paired device, never the receiver's.
    assert ident.attrs["sys_path"] == HID


def test_hid_device_directly_on_usb_is_left_to_the_usb_detector():
    assert paired_identity(fake_hid(parent_subsystem="usb")) is None


def test_hid_device_without_a_slot_is_ignored():
    assert paired_identity(fake_hid(phys="usb-0000:08:00.1-2.3/input2")) is None


# -- power ------------------------------------------------------------------------


def make_supply(device, capacity="63", status="Discharging", online="1", name="hidpp_battery_0"):
    p = device / "power_supply" / name
    p.mkdir(parents=True, exist_ok=True)
    for attr, value in (("type", "Battery"), ("capacity", capacity), ("status", status), ("online", online)):
        if value is not None:
            (p / attr).write_text(value + "\n")
    return p


def never():
    async def gen(_device):
        await asyncio.Event().wait()
        yield

    return gen


def test_power_reads_the_battery_under_the_device(tmp_path):
    make_supply(tmp_path, capacity="63")
    seen = []
    p = HostPower(tmp_path, seen.append, watch=never())
    p.read()
    assert p.state == {"power.level": 63, "power.charging": False, "power.online": True}
    assert seen == [{"power.level": 63, "power.online": True}]  # only what changed


def test_power_without_a_supply_reports_nothing(tmp_path):
    p = HostPower(tmp_path, lambda _c: None, watch=never())
    p.read()
    assert p.state["power.level"] is None and not p.state["power.online"]


def test_power_rejects_nonsense_levels(tmp_path):
    make_supply(tmp_path, capacity="262")
    p = HostPower(tmp_path, lambda _c: None, watch=never())
    p.read()
    assert p.state["power.level"] is None


def test_power_follows_kernel_change_events(tmp_path):
    supply = make_supply(tmp_path, capacity="50")
    kick = asyncio.Queue()

    async def watch(_device):
        while True:
            await kick.get()
            yield

    async def main():
        seen = []
        p = HostPower(tmp_path, seen.append, watch=watch)
        await p.start()
        (supply / "capacity").write_text("49\n")
        (supply / "status").write_text("Charging\n")
        await kick.put(None)
        for _ in range(20):
            await asyncio.sleep(0)
        await p.stop()
        return p, seen

    p, seen = asyncio.run(main())
    assert p.state["power.level"] == 49 and p.state["power.charging"]
    assert seen[-1] == {"power.level": 49, "power.charging": True}


# -- input ------------------------------------------------------------------------


class FakeUinput:
    def __init__(self):
        self.events, self.closed = [], False

    def factory(self):
        return lambda: (self.events.extend, self._close)

    def _close(self):
        self.closed = True


def test_chords_are_validated_against_the_allow_list():
    assert Chord.parse({"keys": ["KEY_LEFTMETA", "KEY_V"]}).keys == ("KEY_LEFTMETA", "KEY_V")
    assert Chord.parse({"wheel": -1}).wheel == ("wheel", -1)
    for bad in (
        {"keys": ["KEY_POWER"]},
        {"keys": ["KEY_A"] * 2},
        {"keys": [f"KEY_{c}" for c in "ABCDEFG"]},
        {"keys": []},
        {"text": "rm -rf ~"},
        {"wheel": 5},
        {"keys": ["KEY_A"], "wheel": 1},
    ):
        with pytest.raises(InputError):
            Chord.parse(bad)


def test_chord_is_held_as_long_as_the_button_and_released_in_reverse():
    fake = FakeUinput()
    inp = HostInput(fake.factory)
    asyncio.run(inp.open())
    combo = Chord.parse({"keys": ["KEY_LEFTMETA", "KEY_LEFTSHIFT", "KEY_4"]})
    inp.down(5, combo)
    inp.down(5, combo)  # repeated report while held: no second press
    assert fake.events == [("EV_KEY", "KEY_LEFTMETA", 1), ("EV_KEY", "KEY_LEFTSHIFT", 1), ("EV_KEY", "KEY_4", 1)]
    fake.events.clear()
    inp.up(5)
    assert fake.events == [("EV_KEY", "KEY_4", 0), ("EV_KEY", "KEY_LEFTSHIFT", 0), ("EV_KEY", "KEY_LEFTMETA", 0)]


def test_closing_releases_held_keys():
    fake = FakeUinput()
    inp = HostInput(fake.factory)
    asyncio.run(inp.open())
    inp.down(1, Chord.parse({"keys": ["KEY_LEFTCTRL"]}))
    inp.close()
    assert fake.events[-1] == ("EV_KEY", "KEY_LEFTCTRL", 0) and fake.closed


def test_nothing_is_emitted_while_closed():
    fake = FakeUinput()
    inp = HostInput(fake.factory)
    inp.down(1, Chord.parse({"keys": ["KEY_A"]}))
    assert fake.events == []


def test_input_cannot_be_driven_over_rpc():
    fake = FakeUinput()
    host = Host(input=HostInput(fake.factory))
    asyncio.run(host.input.open())
    with pytest.raises(PermissionError):
        asyncio.run(host.invoke("input.down", {"keys": ["KEY_A"]}))
    assert fake.events == []


def test_creating_the_device_never_blocks_the_event_loop():
    import time

    def slow_factory():
        def make():
            time.sleep(0.3)  # evdev used to sleep up to 2 s here
            return (lambda events: None), (lambda: None)

        return make

    async def main():
        inp = HostInput(slow_factory)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        await inp.open()
        t.cancel()
        return ticks, inp.is_open

    ticks, opened = asyncio.run(main())
    assert opened and ticks >= 10  # other work kept running meanwhile


def test_the_real_device_is_write_only():
    evdev = pytest.importorskip("evdev")
    from borochid.service.host.input import _uinput_factory

    import time

    make = _uinput_factory()
    start = time.monotonic()
    try:
        emit, close = make()
    except OSError:
        pytest.skip("no access to /dev/uinput here")
    took = time.monotonic() - start
    close()
    assert evdev and took < 0.5  # no waiting to read its event node back
