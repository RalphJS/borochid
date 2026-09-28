import hashlib

import pytest

from borochid.common import images
from borochid.common.manifest import Manifest, ManifestError
from borochid.common.models import Bus, DeviceIdentity

BASE = {"id": "a.b", "version": "1.0.0", "channel": {"type": "hid"}}


def test_check_png_bounds(png):
    assert images.check_png(png(384, 128)) == (384, 128)
    assert images.check_png(png(768, 216)) == (768, 216)  # wide: a keyboard
    assert images.check_png(png(200, 768)) == (200, 768)
    with pytest.raises(images.ImageError, match="limit is 384x384"):
        images.check_png(png(769, 10))
    with pytest.raises(images.ImageError, match="limit is 384x384"):
        images.check_png(png(400, 400))  # only one side may exceed 384
    with pytest.raises(images.ImageError, match="must be a PNG"):
        images.check_png(b"GIF89a" + b"\0" * 40)
    with pytest.raises(images.ImageError, match="KiB"):
        images.check_png(png(8, 8) + b"\0" * images.MAX_BYTES)


def test_store_round_trip_and_refuses_tampered_files(tmp_path, png):
    data = png(16, 16)
    digest = images.store(tmp_path, data)
    assert digest == hashlib.sha256(data).hexdigest()
    assert images.load(tmp_path, digest) == data

    (tmp_path / f"{digest}.png").write_bytes(png(16, 16, (255, 0, 0, 255)))  # same name, other content
    assert images.load(tmp_path, digest) is None
    assert images.load(tmp_path, "../../etc/passwd") is None
    assert images.load(tmp_path, "0" * 64) is None


def test_match_rule_image_overrides_package_image():
    m = Manifest.from_json({
        **BASE,
        "image": "images/family.png",
        "match": [{"bus": "usb", "vid": 1, "pid": 2, "image": "images/xt.png"}, {"bus": "usb", "vid": 1}],
    })
    assert m.image_for(DeviceIdentity(Bus.USB, "u", vid=1, pid=2)) == "images/xt.png"
    assert m.image_for(DeviceIdentity(Bus.USB, "u", vid=1, pid=3)) == "images/family.png"


@pytest.mark.parametrize("path", ["../x.png", "/etc/x.png", "images/x.svg", "a\\x.png", 3])
def test_image_paths_stay_inside_the_package(path):
    with pytest.raises(ManifestError, match="image must be"):
        Manifest.from_json({**BASE, "image": path, "match": [{"bus": "usb", "vid": 1}]})
    with pytest.raises(ManifestError, match="image must be"):
        Manifest.from_json({**BASE, "match": [{"bus": "usb", "vid": 1, "image": path}]})


def test_battery_section():
    m = Manifest.from_json({**BASE, "match": [{"bus": "usb", "vid": 1}],
                            "battery": {"level": "bat", "charging": "chg"}})
    assert m.battery.keys == {"bat", "chg"} and m.battery.charging == "chg"
    for bad in ({}, {"level": 3}, {"level": "bat", "charging": ["x"]}, "bat"):
        with pytest.raises(ManifestError, match="battery"):
            Manifest.from_json({**BASE, "match": [{"bus": "usb", "vid": 1}], "battery": bad})


def test_availability_section():
    def make(spec):
        return Manifest.from_json({**BASE, "match": [{"bus": "usb", "vid": 1}], "available": spec}).available

    by_values = make({"state": "link", "values": ["online", "wired"]})
    assert by_values({"link": "wired"}) and not by_values({"link": "standby"}) and not by_values({})
    truthy = make("online")
    assert truthy({"online": True}) and not truthy({"online": False})
    assert make({"state": "on", "values": [True]})({"on": True})
    for bad in (3, {"values": ["x"]}, {"state": "link", "values": []}, {"state": "link", "values": "online"}):
        with pytest.raises(ManifestError, match="available"):
            make(bad)


def test_a_rule_overrides_battery_and_availability_for_its_connection():
    m = Manifest.from_json({
        **BASE,
        "match": [
            {"bus": "usb", "vid": 1, "pid": 2, "connection": "wireless"},
            {"bus": "usb", "vid": 1, "pid": 3, "connection": "cable",
             "battery": {"level": "bat", "charging": "chg"}, "available": {"state": "link", "values": ["online"]}},
        ],
        "battery": {"level": "power.level"},
        "available": "power.online",
    })
    wireless, cable = DeviceIdentity(Bus.USB, "w", vid=1, pid=2), DeviceIdentity(Bus.USB, "c", vid=1, pid=3)
    assert m.battery_for(wireless).level == "power.level" and m.available_for(wireless).state == "power.online"
    assert m.battery_for(cable).level == "bat" and m.available_for(cable)({"link": "online"})
    assert m.state_keys == {"power.level", "power.online", "bat", "chg", "link"}
    with pytest.raises(ManifestError, match="battery"):
        Manifest.from_json({**BASE, "match": [{"bus": "usb", "vid": 1, "battery": {}}]})
