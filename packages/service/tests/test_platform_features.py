import asyncio
from pathlib import Path

import pytest

from borochid.common.manifest import DriverRef
from borochid.common.models import Bus, DeviceIdentity
from borochid.service.channels import ChannelError, pick_node
from borochid.service.drivers.declarative import DeclarativeDriver
from borochid.service.drivers.loader import DriverUnavailable, driver_class
from borochid.service.host.audio import AudioError, HostAudio
from borochid.service.settings import MemoryStore, SettingsStore

NODES = {3: ["/dev/hidraw5"], 4: ["/dev/hidraw6"], 0: ["/dev/hidraw4"]}


def test_interface_preference_list_takes_first_present():
    assert pick_node(NODES, [4, 3], "x") == "/dev/hidraw6"
    assert pick_node({3: ["/dev/hidraw5"]}, [4, 3], "x") == "/dev/hidraw5"
    assert pick_node(NODES, 0, "x") == "/dev/hidraw4"
    with pytest.raises(ChannelError):
        pick_node(NODES, [7, 8], "x")


def test_settings_follow_serial_and_are_namespaced(tmp_path):
    ident = DeviceIdentity(Bus.USB, "usb:1-2", vid=1, pid=2, serial="AB/12")
    SettingsStore(tmp_path, "acme.thing", ident).save({"color": "#fff"})
    SettingsStore(tmp_path, "acme.thing", ident, namespace="audio").save({"volume": 40})
    moved = DeviceIdentity(Bus.USB, "usb:3-1", vid=1, pid=2, serial="AB/12")
    assert SettingsStore(tmp_path, "acme.thing", moved).load() == {"color": "#fff"}
    assert SettingsStore(tmp_path, "acme.thing", moved, namespace="audio").load() == {"volume": 40}


def test_missing_driver_names_the_package_to_install():
    ref = DriverRef.from_json({"type": "test-never-installed", "version": ">=0.1", "provided_by": "borochid-driver-test-never-installed"})
    with pytest.raises(DriverUnavailable) as exc:
        driver_class(ref)
    assert exc.value.to_json()["provided_by"] == "borochid-driver-test-never-installed"


def test_builtin_driver_resolves():
    assert driver_class(DriverRef.from_json({"type": "declarative"})) is DeclarativeDriver


class FakeTools:
    """Stands in for amixer/pactl/paplay; records argv."""

    def __init__(self, muted=False, mic_present=True):
        self.calls, self.muted, self.mic_present = [], muted, mic_present

    async def __call__(self, *argv):
        self.calls.append(argv)
        if argv[:3] == ("pactl", "list", "sources"):
            ours = "57\talsa_input.usb-Corsair_VIRTUOSO-00.mono\tPipeWire\n" if self.mic_present else ""
            return 0, ours + "58\talsa_input.pci-0000_00.analog\tPipeWire\n"
        if argv[:3] == ("pactl", "list", "sinks"):
            return 0, "60\talsa_output.usb-Corsair_VIRTUOSO-00.analog-stereo\tPipeWire\n"
        if argv[:2] == ("pactl", "get-source-mute"):
            return 0, f"Mute: {'yes' if self.muted else 'no'}\n"
        if argv[:2] == ("pactl", "set-source-mute"):
            self.muted = argv[3] == "1"
        return 0, ""


class FakeSubscribe:
    """Stands in for ``pactl subscribe``: tests push event lines."""

    def __init__(self):
        self.lines: asyncio.Queue[str] = asyncio.Queue()

    async def __call__(self):
        while True:
            yield await self.lines.get()

    async def emit(self, line):
        await self.lines.put(line)
        for _ in range(5):
            await asyncio.sleep(0)


def make_audio(tmp_path, tools, settings=None, subscribe=None):
    cards = tmp_path / "cards"
    cards.write_text(
        " 0 [Generic ]: HDA-Intel - HD-Audio Generic\n"
        " 3 [Speaker ]: USB-Audio - G560 Gaming Speaker\n"
        " 4 [Ga      ]: USB-Audio - CORSAIR VIRTUOSO SE Wireless Ga\n"
    )
    events = []
    audio = HostAudio(
        ["corsair", "virtuoso"], MemoryStore(settings), events.append, tmp_path / "tones", cards, tools,
        subscribe or FakeSubscribe(),
    )
    return audio, events


def test_audio_targets_only_the_matching_card(tmp_path):
    async def go():
        tools = FakeTools()
        audio, _ = make_audio(tmp_path, tools)
        await audio.start()
        assert not [c for c in tools.calls if c[0] == "amixer"], "unset settings must not touch the mixer"
        await audio.invoke("set_volume", {"value": 55})
        assert ("amixer", "-c", "4", "sset", "Headset", "55%") in tools.calls

    asyncio.run(go())


def test_audio_toggle_follows_pipewire_and_notifies(tmp_path):
    async def go():
        tools = FakeTools(muted=True)  # muted from the desktop behind our back
        audio, events = make_audio(tmp_path, tools, {"mute_tone": False})
        await audio.start()
        seen = []
        audio.on_mic_muted(seen.append)
        assert await audio.toggle_mic_mute() is False
        assert seen == [False] and {"audio.mic_muted": False} in events
        assert ("pactl", "set-source-mute", "alsa_input.usb-Corsair_VIRTUOSO-00.mono", "0") in tools.calls

    asyncio.run(go())


def test_audio_refuses_vague_names(tmp_path):
    with pytest.raises(ValueError):
        HostAudio(["hea"], MemoryStore(), print, tmp_path)


def test_audio_without_matching_card_fails_loudly(tmp_path):
    async def go():
        audio, _ = make_audio(tmp_path, FakeTools())
        audio.names = ["steelseries"]
        with pytest.raises(AudioError, match="no sound card"):
            await audio.invoke("set_volume", {"value": 10})

    asyncio.run(go())


def test_mic_that_appears_after_the_device_is_picked_up(tmp_path):
    """After a dongle reset PipeWire creates the mic a moment after the device."""

    async def go():
        tools, sub = FakeTools(muted=True, mic_present=False), FakeSubscribe()
        audio, _ = make_audio(tmp_path, tools, subscribe=sub)
        await audio.start()
        assert audio.state["audio.mic_muted"] is None
        tools.mic_present = True
        await sub.emit("Event 'new' on source #57")
        assert audio.state["audio.mic_muted"] is True
        await audio.stop()

    asyncio.run(go())


def test_mute_from_the_desktop_reaches_listeners(tmp_path):
    async def go():
        tools, sub = FakeTools(), FakeSubscribe()
        audio, _ = make_audio(tmp_path, tools, subscribe=sub)
        seen = []
        audio.on_mic_muted(seen.append)
        await audio.start()
        tools.muted = True  # the user muted in GNOME/KDE
        await sub.emit("Event 'change' on source #57")
        assert seen == [True]
        reads = len(tools.calls)
        await sub.emit("Event 'change' on source #58")  # someone else's mic
        await sub.emit("Event 'change' on sink #60")
        assert len(tools.calls) == reads, "unrelated events cost nothing"
        await audio.stop()

    asyncio.run(go())
