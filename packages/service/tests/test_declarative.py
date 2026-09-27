import asyncio

import pytest

from borochid.common.manifest import Manifest
from borochid.service.drivers import DriverError
from borochid.service.drivers.declarative import DeclarativeDriver


class FakeChannel:
    def __init__(self):
        self.written = []

    async def write(self, data):
        self.written.append(data)


@pytest.fixture
def driver(examples):
    d = examples / "acme.macropad"
    events = []
    drv = DeclarativeDriver(Manifest.load(d), d, FakeChannel(), events.append)
    drv.events = events
    return drv


def test_actions_encode_packets_and_update_state(driver):
    asyncio.run(driver.invoke("set_color", {"color": "#102030"}))
    asyncio.run(driver.invoke("set_brightness", {"value": 200}))
    assert driver.channel.written == [bytes([4, 0x10, 0x20, 0x30]), bytes([3, 200])]
    assert driver.state["color"] == "#102030"
    assert {"color": "#102030"} in driver.events


def test_inputs_decode_and_only_publish_changes(driver):
    driver.on_data(bytes([2, 77]))
    driver.on_data(bytes([2, 77]))
    driver.on_data(bytes([9, 1]))  # unknown report is ignored
    assert driver.state["battery"] == 77
    assert driver.events == [{"battery": 77}]


@pytest.mark.parametrize(
    "action, params",
    [
        ("set_brightness", {"value": 300}),
        ("set_brightness", {}),
        ("set_color", {"color": "red"}),
        ("set_mode", {"mode": 7}),
        ("nope", {}),
    ],
)
def test_invalid_invocations_are_rejected(driver, action, params):
    with pytest.raises(DriverError):
        asyncio.run(driver.invoke(action, params))
    assert driver.channel.written == []
