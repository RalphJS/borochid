"""End-to-end: simulated device -> local package -> declarative driver -> RPC."""

import asyncio
import json

from borochid.common import rpc
from borochid.service.config import Config
from borochid.service.detectors.sim import SimDetector
from borochid.service.devices import DeviceManager
from borochid.service.registry.client import Registry
from borochid.service.server import RpcServer


def test_sim_device_end_to_end(tmp_path, examples):
    async def go():
        cfg = Config(local_packages_dir=examples, cache_dir=tmp_path / "cache", data_dir=tmp_path / "data", socket_path=tmp_path / "d.sock")
        server = RpcServer(cfg.socket_path)
        manager = DeviceManager(Registry(cfg), server.broadcast)
        server.manager = manager
        await server.start()
        reader, writer = await asyncio.open_unix_connection(str(cfg.socket_path))
        await SimDetector(manager).start()

        async def call(id_, method, **params):
            writer.write(rpc.request(id_, method, params))
            while True:
                msg = json.loads(await reader.readline())
                if msg.get("id") == id_:
                    return msg
                notes.append(msg)

        notes = []
        for _ in range(50):
            devs = (await call(1, "devices.list"))["result"]
            if devs and devs[0]["status"] == "ready":
                break
            await asyncio.sleep(0.02)
        assert devs[0]["package"]["id"] == "acme.macropad"

        uid = devs[0]["uid"]
        detail = (await call(2, "device.get", uid=uid))["result"]
        assert detail["ui"] and "set_brightness" in detail["actions"]
        assert detail["layouts"] == {}  # no ui item names a layout section

        assert "result" in await call(3, "device.invoke", uid=uid, action="set_brightness", params={"value": 42})
        # The sim echoes the report; the declarative input rule turns it into state.
        while not any(n.get("method") == "device.state" and n["params"]["changes"].get("brightness") == 42 for n in notes):
            notes.append(json.loads(await asyncio.wait_for(reader.readline(), 2)))

        err = await call(4, "device.invoke", uid=uid, action="set_brightness", params={"value": 999})
        assert err["error"]["code"] == rpc.DEVICE_ERROR

        writer.close()
        await manager.shutdown()
        await server.stop()

    asyncio.run(go())


def test_missing_driver_reports_package_without_opening_device(tmp_path):
    """The device stays untouched until the driver package is installed."""
    import shutil

    from borochid.common.models import Bus, DeviceIdentity

    pkgs = tmp_path / "pkgs"
    pkg = pkgs / "acme.headset"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json").write_text(json.dumps({
        "id": "acme.headset", "version": "1.0.0",
        "match": [{"bus": "usb", "vid": "0x1234", "pid": "0x0001"}],
        "channel": {"type": "hid"},
        "driver": {"type": "acme-proto", "version": ">=1.0", "provided_by": "borochid-driver-acme-proto"},
    }))

    async def go():
        events = []
        cfg = Config(local_packages_dir=pkgs, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: events.append((m, p)))
        manager.device_added(DeviceIdentity(Bus.USB, "usb:9-9", vid=0x1234, pid=0x0001, attrs={"sys_path": "/nonexistent"}))
        await manager.devices["usb:9-9"].task
        dev = manager.devices["usb:9-9"]
        assert dev.status == "needs_driver" and dev.channel is None
        assert dev.summary()["needs"]["provided_by"] == "borochid-driver-acme-proto"
        assert manager.retry() == 1  # e.g. after the GUI installed the package
        await manager.devices["usb:9-9"].task
        await manager.shutdown()

    asyncio.run(go())
    shutil.rmtree(pkgs)


def test_bring_up_waits_for_the_node_ready_event(tmp_path, examples, monkeypatch):
    """udev announces the USB device before its hidraw node is usable."""
    from borochid.common.models import Bus, DeviceIdentity
    from borochid.service import plugins
    from borochid.service.channels import ChannelNotReady
    from borochid.service.channels.sim import SimChannel

    attempts = []

    class LateNode(SimChannel):
        ready = False

        def __init__(self, ident, spec):
            super().__init__(ident, spec)

        async def open(self):
            attempts.append(LateNode.ready)
            if not LateNode.ready:
                raise ChannelNotReady("no access to /dev/hidraw9 yet")

    real_load = plugins.load
    monkeypatch.setattr(plugins, "load", lambda g, n: LateNode if g == plugins.CHANNELS else real_load(g, n))

    async def go():
        cfg = Config(local_packages_dir=examples, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: None)
        manager.device_added(DeviceIdentity(Bus.USB, "usb:1-2.4", vid=0x1209, pid=0xB0C1))
        await manager.devices["usb:1-2.4"].task
        dev = manager.devices["usb:1-2.4"]
        assert dev.status == "connecting" and "no access" in dev.error

        LateNode.ready = True
        manager.device_changed("usb:1-2.4")  # udev: hidraw node processed
        await manager.devices["usb:1-2.4"].task
        assert dev.status == "ready" and attempts == [False, True]
        await manager.shutdown()

    asyncio.run(go())


def test_ready_event_during_bring_up_is_not_lost(tmp_path, examples, monkeypatch):
    from borochid.common.models import Bus, DeviceIdentity
    from borochid.service import plugins
    from borochid.service.channels import ChannelNotReady
    from borochid.service.channels.sim import SimChannel

    manager_ref = {}

    class RacingNode(SimChannel):
        calls = 0

        def __init__(self, ident, spec):
            super().__init__(ident, spec)

        async def open(self):
            RacingNode.calls += 1
            if RacingNode.calls == 1:
                manager_ref["m"].device_changed("usb:1-2.4")  # arrives mid-bring-up
                raise ChannelNotReady("not yet")

    real_load = plugins.load
    monkeypatch.setattr(plugins, "load", lambda g, n: RacingNode if g == plugins.CHANNELS else real_load(g, n))

    async def go():
        cfg = Config(local_packages_dir=examples, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = manager_ref["m"] = DeviceManager(Registry(cfg), lambda m, p: None)
        manager.device_added(DeviceIdentity(Bus.USB, "usb:1-2.4", vid=0x1209, pid=0xB0C1))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if manager.devices["usb:1-2.4"].status == "ready":
                break
        assert manager.devices["usb:1-2.4"].status == "ready" and RacingNode.calls == 2
        await manager.shutdown()

    asyncio.run(go())


def _mode_switching_package(root):
    """A dongle that is 1234:0001 with its headset linked and 1234:0002 without."""
    pkg = root / "acme.dongle"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json").write_text(json.dumps({
        "id": "acme.dongle", "version": "1.0.0",
        "match": [
            {"bus": "usb", "vid": "0x1234", "pid": "0x0001"},
            {"bus": "usb", "vid": "0x1234", "pid": "0x0002", "channel": None},
        ],
        "channel": {"type": "hid"},
        "driver": {"type": "declarative"},
        "state": {"mode": "on"},
        "summary": {"state": "mode", "map": {"on": "Connected"}},
    }))
    return root


def test_mode_switch_re_enumeration_keeps_one_device(tmp_path, monkeypatch):
    from borochid.common.models import Bus, DeviceIdentity
    from borochid.service import plugins
    from borochid.service.channels import NullChannel
    from borochid.service.channels.sim import SimChannel

    opened = []

    class Hid(SimChannel):
        def __init__(self, ident, spec):
            super().__init__(ident, spec)

        async def open(self):
            opened.append(self.ident.pid)

    real_load = plugins.load
    monkeypatch.setattr(plugins, "load", lambda g, n: Hid if g == plugins.CHANNELS else real_load(g, n))

    async def go():
        events = []
        cfg = Config(local_packages_dir=_mode_switching_package(tmp_path / "pkgs"), cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: events.append((m, p)), grace_s=0.2)
        linked = DeviceIdentity(Bus.USB, "usb:1-2.4", vid=0x1234, pid=0x0001, serial="S1")
        idle = DeviceIdentity(Bus.USB, "usb:1-2.4", vid=0x1234, pid=0x0002, serial="S1")

        manager.device_added(linked)
        await manager.devices["usb:1-2.4"].task
        dev = manager.devices["usb:1-2.4"]
        assert dev.summary()["status_text"] == "Connected"

        manager.device_removed("usb:1-2.4")  # headset switched off: dongle drops off USB...
        assert dev.status == "disconnected"
        manager.device_added(idle)  # ...and comes back as the idle product
        await dev.task
        assert manager.devices["usb:1-2.4"] is dev and dev.status == "ready"
        assert isinstance(dev.channel, NullChannel), "nothing is opened in idle mode"
        assert opened == [0x0001]

        manager.device_removed("usb:1-2.4")
        manager.device_added(linked)  # headset back on
        await dev.task
        assert opened == [0x0001, 0x0001]
        assert [m for m, _ in events].count("device.removed") == 0
        assert [m for m, _ in events].count("device.added") == 1

        manager.device_removed("usb:1-2.4")  # dongle unplugged for real
        await asyncio.sleep(0.3)
        assert "usb:1-2.4" not in manager.devices and ("device.removed", {"uid": "usb:1-2.4"}) in events
        await manager.shutdown()

    asyncio.run(go())


def test_different_serial_on_the_same_port_is_a_new_device(tmp_path):
    from borochid.common.models import Bus, DeviceIdentity

    async def go():
        events = []
        cfg = Config(local_packages_dir=tmp_path / "none", cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: events.append(m), grace_s=5)
        manager.device_added(DeviceIdentity(Bus.USB, "usb:1-1", vid=1, pid=1, serial="A"))
        manager.device_removed("usb:1-1")
        manager.device_added(DeviceIdentity(Bus.USB, "usb:1-1", vid=1, pid=1, serial="B"))
        assert events.count("device.removed") == 1 and events.count("device.added") == 2
        assert manager.devices["usb:1-1"].ident.serial == "B"
        await manager.shutdown()

    asyncio.run(go())


def test_display_name_prefers_what_the_device_reports(tmp_path, examples):
    from borochid.common.models import Bus, DeviceIdentity

    async def go():
        cfg = Config(local_packages_dir=examples, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: None)
        manager.device_added(DeviceIdentity(Bus.USB, "usb:1", vid=0x1209, pid=0xB0C1, name="Acme Macropad Pro 2 (Rev B)", attrs={"simulated": True}))
        manager.device_added(DeviceIdentity(Bus.USB, "usb:2", vid=0x1209, pid=0xB0C1, name="  ", attrs={"simulated": True}))
        await asyncio.gather(manager.devices["usb:1"].task, manager.devices["usb:2"].task)
        assert manager.devices["usb:1"].summary()["display_name"] == "Acme Macropad Pro 2 (Rev B)"
        assert manager.devices["usb:2"].summary()["display_name"] == "Acme Macropad"  # package name fallback
        assert manager.devices["usb:1"].summary()["category"] == "keypad"
        await manager.shutdown()

    asyncio.run(go())


def test_device_picture_is_exported_to_the_image_store(tmp_path, examples, png):
    import shutil

    from borochid.common import images
    from borochid.common.models import Bus, DeviceIdentity

    pkgs = tmp_path / "pkgs"
    shutil.copytree(examples / "acme.macropad", pkgs / "acme.macropad")
    manifest = json.loads((pkgs / "acme.macropad" / "manifest.json").read_text())
    (pkgs / "acme.macropad" / "manifest.json").write_text(json.dumps({**manifest, "image": "pad.png"}))
    (pkgs / "acme.macropad" / "pad.png").write_bytes(png(32, 32))

    async def go():
        cfg = Config(local_packages_dir=pkgs, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: None)
        manager.device_added(DeviceIdentity(Bus.USB, "usb:1", vid=0x1209, pid=0xB0C1, attrs={"simulated": True}))
        await manager.devices["usb:1"].task
        digest = manager.devices["usb:1"].summary()["image"]
        assert images.load(manager.image_store, digest) == png(32, 32)

        # A bad picture is skipped; the device still comes up.
        (pkgs / "acme.macropad" / "pad.png").write_bytes(png(999, 999))
        manager.device_added(DeviceIdentity(Bus.USB, "usb:2", vid=0x1209, pid=0xB0C1, attrs={"simulated": True}))
        await manager.devices["usb:2"].task
        assert manager.devices["usb:2"].summary()["image"] is None
        assert manager.devices["usb:2"].status == "ready"
        await manager.shutdown()

    asyncio.run(go())


def test_battery_is_part_of_the_summary_and_announced_when_it_changes(tmp_path, examples):
    from borochid.common.models import Bus, DeviceIdentity

    async def go():
        events = []
        cfg = Config(local_packages_dir=examples, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: events.append((m, p)))
        manager.device_added(DeviceIdentity(Bus.USB, "usb:1", vid=0x1209, pid=0xB0C1, attrs={"simulated": True}))
        dev = manager.devices["usb:1"]
        await dev.task
        assert dev.summary()["battery"] == {"level": None, "charging": False}

        events.clear()
        dev.driver.on_data(bytes.fromhex("0257"))  # the example's battery report: 0x57 = 87%
        changed = [p for m, p in events if m == "device.changed"]
        assert changed and changed[-1]["battery"]["level"] == 87

        events.clear()
        dev.driver.on_data(bytes.fromhex("0340"))  # brightness: not shown in the summary
        assert not [m for m, _ in events if m == "device.changed"]
        await manager.shutdown()

    asyncio.run(go())


def test_unavailable_device_is_announced_and_says_so(tmp_path, examples):
    import shutil

    from borochid.common.models import Bus, DeviceIdentity

    pkgs = tmp_path / "pkgs"
    shutil.copytree(examples / "acme.macropad", pkgs / "acme.macropad")
    path = pkgs / "acme.macropad" / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["available"] = {"state": "brightness", "values": [128]}  # stand-in for a link state
    path.write_text(json.dumps(manifest))

    async def go():
        events = []
        cfg = Config(local_packages_dir=pkgs, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: events.append((m, p)))
        manager.device_added(DeviceIdentity(Bus.USB, "usb:1", vid=0x1209, pid=0xB0C1, attrs={"simulated": True}))
        dev = manager.devices["usb:1"]
        await dev.task
        assert dev.summary()["available"] is True

        events.clear()
        dev.driver.on_data(bytes.fromhex("0340"))  # brightness 64: "unavailable"
        changed = [p for m, p in events if m == "device.changed"]
        assert changed and changed[-1]["available"] is False
        await manager.shutdown()

    asyncio.run(go())



def test_profile_switches_reach_every_device_that_supports_profiles(tmp_path, examples):
    from borochid.common.models import Bus, DeviceIdentity

    async def go():
        events = []
        cfg = Config(local_packages_dir=examples, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: events.append((m, p)))
        manager.device_added(DeviceIdentity(Bus.USB, "usb:1", vid=0x1209, pid=0xB0C1, attrs={"simulated": True}))
        dev = manager.devices["usb:1"]
        await dev.task
        assert dev.summary()["profiles"] is False  # the declarative driver has none

        seen = []

        async def use_profile(profile, known):
            seen.append((profile.name, profile.copy_of, sorted(known)))

        dev.driver.supports_profiles = True
        dev.driver.use_profile = use_profile
        assert dev.summary()["profiles"] is True

        gaming = manager.profiles.add("Gaming", duplicate=True)
        await manager._profile_task
        manager.profiles.remove("default")
        await manager._profile_task
        assert seen == [("Gaming", "default", ["default", gaming.id]), ("Gaming", None, [gaming.id])]
        assert [m for m, _ in events].count("profiles.changed") == 2
        await manager.shutdown()

    asyncio.run(go())


def test_settings_follow_the_device_id(tmp_path):
    from borochid.common.models import Bus, DeviceIdentity
    from borochid.service.settings import SettingsStore

    on_port = SettingsStore(tmp_path, "pkg", DeviceIdentity(Bus.USB, "usb:1-3"))
    on_port.save({"brightness": 40})
    assert on_port.rekey("9454DCB7") is False  # the first time, what was kept moves over
    assert on_port.load() == {"brightness": 40} and not (tmp_path / "device-settings/pkg/usb_1-3.driver.json").exists()

    on_cable = SettingsStore(tmp_path, "pkg", DeviceIdentity(Bus.USB, "usb:2-1", serial="XYZ"))
    on_cable.save({"brightness": 100})
    assert on_cable.rekey("9454DCB7") is True  # after that, the device's own settings win
    assert on_cable.load() == {"brightness": 40}
    assert [f.name for f in (tmp_path / "device-settings/pkg").iterdir()] == ["id-9454DCB7.driver.json"]


def test_one_device_on_two_connections_shows_once_and_the_one_in_use_owns_settings(tmp_path, examples):
    import shutil

    from borochid.common.models import Bus, DeviceIdentity

    pkgs = tmp_path / "pkgs"
    shutil.copytree(examples / "acme.macropad", pkgs / "acme.macropad")
    path = pkgs / "acme.macropad" / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["available"] = {"state": "brightness", "values": [128]}  # stand-in for a link state
    manifest["match"][0]["connection"] = "wireless"
    path.write_text(json.dumps(manifest))

    async def go():
        events = []
        cfg = Config(local_packages_dir=pkgs, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: events.append((m, p)))
        for uid in ("usb:1", "usb:2"):
            manager.device_added(DeviceIdentity(Bus.USB, uid, vid=0x1209, pid=0xB0C1, attrs={"simulated": True}))
        one, two = manager.devices["usb:1"], manager.devices["usb:2"]
        await one.task
        await two.task
        assert one.summary()["connection"] == "wireless"

        await one.driver.identify("unit-7")
        await two.driver.identify("unit-7")
        assert (one.shadowed, two.shadowed) == (False, True)
        assert two.driver.passive and not one.driver.passive
        two.driver.settings["x"] = 1
        two.driver.save_settings()  # passive: not written
        one.driver.settings["x"] = 2
        one.driver.save_settings()

        one.driver.on_data(bytes.fromhex("0340"))  # the first connection becomes unusable
        await asyncio.sleep(0.01)
        assert (one.shadowed, two.shadowed) == (True, False)
        assert two.driver.settings["x"] == 2  # took over with what its twin saved
        summaries = [p for m, p in events if m == "device.changed" and p["uid"] == "usb:2"]
        assert summaries[-1]["shadowed"] is False and summaries[-1]["device_id"] == "unit-7"

        manager.device_removed("usb:2")  # unplugged: kept a moment in case it comes back
        await asyncio.sleep(0.01)
        assert (one.shadowed, two.shadowed) == (False, True)  # the other connection shows meanwhile
        await manager.shutdown()

    asyncio.run(go())


def test_a_known_serial_is_the_same_device_from_the_moment_it_appears(tmp_path, examples):
    from borochid.common.models import Bus, DeviceIdentity

    async def go():
        events = []
        cfg = Config(local_packages_dir=examples, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: events.append((m, p)))

        def plug(uid, serial=None):
            manager.device_added(DeviceIdentity(Bus.USB, uid, vid=0x1209, pid=0xB0C1, serial=serial,
                                                attrs={"simulated": True}))
            return manager.devices[uid]

        wireless = plug("usb:1")
        await wireless.task
        await wireless.driver.identify("unit-7")
        cable = plug("usb:2", serial="SN42")  # first time: unknown until its driver says so
        await cable.task
        await cable.driver.identify("unit-7")
        manager.device_removed("usb:2")
        manager._forget(cable)

        events.clear()
        cable = plug("usb:2", serial="SN42")  # plugged in again: known at once
        added = [p for m, p in events if m == "device.added"]
        assert added[-1]["device_id"] == "unit-7" and added[-1]["shadowed"] is True  # no second card, not even briefly
        await cable.task
        assert cable.driver.passive
        await manager.shutdown()

        again = DeviceManager(Registry(cfg), lambda m, p: None)  # remembered across restarts
        assert again._known_ids == {"usb:1209:b0c1:SN42": "unit-7"}

    asyncio.run(go())


def _dongle_and_headset_packages(root, examples):
    """Two packages for one USB ID: the dongle, and the headset behind it."""
    import shutil

    for pkg_id, paired in (("acme.dongle", False), ("acme.headset", True)):
        shutil.copytree(examples / "acme.macropad", root / pkg_id)
        path = root / pkg_id / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["id"] = pkg_id
        manifest["match"][0]["paired"] = paired
        path.write_text(json.dumps(manifest))
    return root


def test_a_device_a_receiver_driver_announces_is_its_own_device_and_goes_with_it(tmp_path, examples):
    from borochid.common.models import Bus, DeviceIdentity
    from borochid.service.channels import PairedChannel

    pkgs = _dongle_and_headset_packages(tmp_path / "pkgs", examples)

    async def go():
        events = []
        cfg = Config(local_packages_dir=pkgs, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: events.append((m, p)))
        manager.device_added(DeviceIdentity(Bus.USB, "usb:1-2", vid=0x1209, pid=0xB0C1, name="Acme Dongle",
                                            attrs={"simulated": True, "sys_path": "/sys/usb1/1-2"}))
        dongle = manager.devices["usb:1-2"]
        await dongle.task
        assert dongle.manifest.id == "acme.dongle"

        channel = dongle.driver.pair("headset", name="Acme Headset")
        headset = manager.devices["usb:1-2/headset"]
        await headset.task
        assert str(headset.status) == "ready" and headset.manifest.id == "acme.headset"
        assert headset.summary()["display_name"] == "Acme Headset"
        assert isinstance(headset.channel, PairedChannel) and headset.channel is channel
        assert channel.receiver is dongle.channel

        written = []
        dongle.channel.write = lambda data: written.append(data) or asyncio.sleep(0)
        await manager.invoke("usb:1-2/headset", "set_brightness", {"value": 9})
        assert written == [bytes([3, 9])]  # through the dongle's channel
        channel.deliver(bytes.fromhex("0340"))  # the dongle's driver hands it its input
        assert headset.driver.state["brightness"] == 0x40

        manager.device_removed("usb:1-2/headset")  # udev knows nothing of it: ignored
        assert "usb:1-2/headset" in manager.devices
        dongle.driver.unpair("headset")
        assert "usb:1-2/headset" not in manager.devices
        await asyncio.sleep(0.01)

        dongle.driver.pair("headset")
        again = manager.devices["usb:1-2/headset"]
        await again.task
        manager.device_removed("usb:1-2")  # the dongle is unplugged: the headset goes at once
        await dongle.teardown
        assert "usb:1-2/headset" not in manager.devices and again.driver is None
        assert ("device.removed", {"uid": "usb:1-2/headset"}) in events
        await manager.shutdown()

    asyncio.run(go())


def test_hidden_devices_stay_listed_flagged_and_are_remembered(tmp_path, examples):
    from borochid.common.models import Bus, DeviceIdentity

    async def go():
        events = []
        cfg = Config(local_packages_dir=examples, cache_dir=tmp_path / "c", data_dir=tmp_path / "d")
        manager = DeviceManager(Registry(cfg), lambda m, p: events.append((m, p)))

        def plug(m, uid, serial=None, pid=0xB0C1):
            m.device_added(DeviceIdentity(Bus.USB, uid, vid=0x1209, pid=pid, serial=serial, attrs={"simulated": True}))
            return m.devices[uid]

        dongle = plug(manager, "usb:1-2", serial="R1")
        other = plug(manager, "usb:1-5")
        await dongle.task
        await other.task
        assert manager.set_hidden("usb:1-2", True).summary()["hidden"] is True
        assert [p["hidden"] for m, p in events if m == "device.changed" and p["uid"] == "usb:1-2"][-1] is True
        assert other.hidden is False
        await manager.shutdown()

        # Remembered, and by its USB serial: the same dongle as another product
        # (re-enumerated) or on another port is still hidden.
        again = DeviceManager(Registry(cfg), lambda m, p: None)
        assert plug(again, "usb:3-1", serial="R1", pid=0xB0C2).hidden is True
        assert plug(again, "usb:1-5").hidden is False

        # A device with an ID: every connection of it is hidden.
        cable, radio = plug(again, "usb:1-7"), plug(again, "usb:1-8")
        await cable.task
        await radio.task
        await cable.driver.identify("unit-7")
        await radio.driver.identify("unit-7")
        again.set_hidden("usb:1-7", True)
        assert cable.hidden and radio.hidden
        again.set_hidden("usb:1-8", False)  # shown again from either one
        assert not cable.hidden and not radio.hidden
        await again.shutdown()

    asyncio.run(go())
