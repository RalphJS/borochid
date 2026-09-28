import pytest

from borochid.common.manifest import Manifest, ManifestError, shard_key_for_rule
from borochid.common.models import Bus, DeviceIdentity, MatchRule


def usb(vid, pid, **kw):
    return DeviceIdentity(Bus.USB, f"usb:{vid:x}:{pid:x}", vid=vid, pid=pid, **kw)


def test_more_specific_rule_scores_higher():
    dev = usb(0x046D, 0xC52B)
    vendor_wide = MatchRule.from_json({"bus": "usb", "vid": "0x046d"})
    ranged = MatchRule.from_json({"bus": "usb", "vid": "0x046d", "pid_range": ["0xc500", "0xc5ff"]})
    exact = MatchRule.from_json({"bus": "usb", "vid": "0x046d", "pid": "0xc52b"})
    assert 0 < vendor_wide.score(dev) < ranged.score(dev) < exact.score(dev)


def test_non_matching_rules_score_zero():
    dev = usb(0x046D, 0xC52B)
    assert MatchRule.from_json({"bus": "usb", "vid": "0x046d", "pid": "0x0001"}).score(dev) == 0
    assert MatchRule.from_json({"bus": "ble", "service_uuid": "abcd"}).score(dev) == 0


def test_shard_keys_round_trip():
    dev = usb(0x046D, 0xC52B)
    rule = MatchRule.from_json({"bus": "usb", "vid": "0x046d", "pid": "0xc52b"})
    assert shard_key_for_rule(rule) in dev.shard_keys()

    ble = DeviceIdentity(Bus.BLE, "ble:x", attrs={"service_uuids": ["0000FFE0-0000-1000-8000-00805F9B34FB"], "company_ids": [0x59]})
    rule = MatchRule.from_json({"bus": "ble", "service_uuid": "0000ffe0-0000-1000-8000-00805f9b34fb"})
    assert shard_key_for_rule(rule) in ble.shard_keys()
    assert rule.score(ble) > 0


def test_ble_rules_must_be_shardable():
    with pytest.raises(ManifestError):
        shard_key_for_rule(MatchRule.from_json({"bus": "ble", "name_prefix": "Acme"}))


def test_examples_are_valid(examples):
    for d in examples.iterdir():
        Manifest.load(d)


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"id": "NoDots"}, "invalid package id"),
        ({"version": "1"}, "invalid version"),
        ({"match": []}, "at least one match rule"),
        ({"tier": "code"}, "data only"),
        ({"driver": {"type": "x", "provided_by": "gnome-shell"}}, "provided_by must match"),
        ({"driver": {"type": "x", "version": "~1"}}, "invalid version spec"),
        ({"schema": 99}, "newer than supported"),
        ({"category": ["headset"]}, "category must be a string"),
    ],
)
def test_manifest_validation(patch, message):
    base = {"id": "a.b", "version": "1.0.0", "match": [{"bus": "usb", "vid": 1}], "channel": {"type": "hid"}}
    with pytest.raises(ManifestError, match=message):
        Manifest.from_json({**base, **patch})


@pytest.mark.parametrize("given, expected", [(None, "other"), ("headset", "headset"), ("hologram", "other")])
def test_category_defaults_and_tolerates_newer_values(given, expected):
    d = {"id": "a.b", "version": "1.0.0", "match": [{"bus": "usb", "vid": 1}], "channel": {"type": "hid"}}
    if given:
        d["category"] = given
    assert Manifest.from_json(d).category == expected


def test_version_specs():
    from borochid.common.manifest import VersionSpec

    spec = VersionSpec(">=0.2,<1")
    assert spec.allows("0.2.0") and spec.allows("0.9.9")
    assert not spec.allows("0.1.9") and not spec.allows("1.0.0")
    assert VersionSpec("").allows("7.0.0")


VIRTUOSO_NAMES = [
    {"match": "corsair virtuoso {model:upper} *", "format": "Corsair Virtuoso {model}"},
    {"match": "corsair virtuoso *", "format": "Corsair Virtuoso"},
]


@pytest.mark.parametrize(
    "reported, expected",
    [
        ("CORSAIR VIRTUOSO SE Wireless Gaming Headset", "Corsair Virtuoso SE"),
        ("CORSAIR VIRTUOSO XT Wireless Gaming Headset", "Corsair Virtuoso XT"),
        ("CORSAIR  VIRTUOSO RGB Wireless", "Corsair Virtuoso RGB"),
        ("CORSAIR VIRTUOSO Wireless Gaming Headset", "Corsair Virtuoso"),  # "Wireless" is not a model
        ("CORSAIR VIRTUOSO", "Corsair Virtuoso"),
        ("CORSAIR HS80 RGB Wireless", None),  # no rule matches: keep the reported name
    ],
)
def test_display_name_rules(reported, expected):
    from borochid.common.manifest import DisplayNameRule, display_name

    rules = tuple(DisplayNameRule.from_json(r) for r in VIRTUOSO_NAMES)
    assert display_name(rules, reported) == expected


@pytest.mark.parametrize(
    "rule, message",
    [
        ({"match": "a * b", "format": "x"}, "last word"),
        ({"match": "{Model} x", "format": "x"}, "bad capture"),
        ({"match": "a {m}", "format": "{m.__class__}"}, "plain captures"),
        ({"match": "a {m}", "format": "{0}"}, "plain captures"),
        ({"match": "a {m}", "format": "{m!r}"}, "plain captures"),
        ({"match": "a {m}", "format": "{other}"}, "plain captures"),
    ],
)
def test_display_name_rules_are_validated(rule, message):
    from borochid.common.manifest import DisplayNameRule

    with pytest.raises(ManifestError, match=message):
        DisplayNameRule.from_json(rule)


def test_display_name_matching_is_linear_on_hostile_input():
    import time

    from borochid.common.manifest import DisplayNameRule

    rule = DisplayNameRule.from_json({"match": "a {x} {y} *", "format": "{x}{y}"})
    start = time.perf_counter()
    for _ in range(1000):
        rule.apply("a " * 5000)
    assert time.perf_counter() - start < 1.0


def test_key_codes_match_the_kernel():
    ecodes = pytest.importorskip("evdev.ecodes")
    from borochid.common.keys import CODES

    assert {name: ecodes.ecodes[name] for name in CODES} == CODES


def test_system_keys_are_never_allowed():
    from borochid.common.keys import ALLOWED

    assert not ALLOWED & {"KEY_POWER", "KEY_SLEEP", "KEY_WAKEUP", "KEY_SUSPEND", "KEY_SCREENLOCK", "KEY_COFFEE"}


def test_chord_labels():
    from borochid.common.keys import chord_label

    assert chord_label(["KEY_LEFTMETA", "KEY_LEFTSHIFT", "KEY_4"]) == "Super+Shift+4"
    assert chord_label(["KEY_LEFTCTRL", "KEY_PAGEUP"]) == "Ctrl+Page Up"


def test_every_key_is_in_exactly_one_picker_group():
    from borochid.common.keys import ALLOWED, GROUPS, MODIFIERS

    listed = [k for _, keys in GROUPS for k in keys]
    assert len(listed) == len(set(listed))
    assert set(listed) == {k for k in ALLOWED if k.startswith("KEY_") and k not in MODIFIERS}


def test_friendly_labels():
    from borochid.common.keys import label

    assert (label("KEY_SYSRQ"), label("KEY_KPPLUS"), label("KEY_F13")) == ("Print", "Keypad +", "F13")



def test_ui_layouts_are_the_sections_ui_items_name():
    keys = {"keys": [{"id": "a", "x": 0, "y": 0}]}
    d = {"id": "a.b", "version": "1.0.0", "match": [{"bus": "usb", "vid": 1}], "channel": {"type": "hid"},
         "keyboard": keys, "secret": {"x": 1}, "name": "not a dict",
         "ui": [{"widget": "group", "children": [{"widget": "keyboard", "layout": "keyboard"},
                                                  {"widget": "keyboard", "layout": "keyboard"}]},
                {"widget": "keyboard", "layout": "name"}, {"widget": "keyboard", "layout": "missing"},
                {"widget": "keyboard", "layout": {"keys": []}}]}
    assert Manifest.from_json(d).ui_layouts() == {"keyboard": keys}
