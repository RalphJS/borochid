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
    ],
)
def test_manifest_validation(patch, message):
    base = {"id": "a.b", "version": "1.0.0", "match": [{"bus": "usb", "vid": 1}], "channel": {"type": "hid"}}
    with pytest.raises(ManifestError, match=message):
        Manifest.from_json({**base, **patch})


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
