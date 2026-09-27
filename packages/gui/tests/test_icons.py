import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from borochid.gui.icons import battery_icon, battery_text  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _colours(battery):
    image = battery_icon(battery).pixmap(64, 64).toImage()
    return {image.pixelColor(x, y).name() for x in range(64) for y in range(64) if image.pixelColor(x, y).alpha() > 200}


def test_battery_is_drawn_and_shows_level_charging_and_low(app):
    assert battery_icon(None) is None
    unknown, half, full = _colours({"level": None}), _colours({"level": 50}), _colours({"level": 100})
    assert unknown and half and full
    assert "#da4453" in _colours({"level": 10})  # low: red fill
    assert "#27ae60" in _colours({"level": 60, "charging": True})  # charging: green fill
    image = lambda b: battery_icon(b).pixmap(64, 64).toImage()  # noqa: E731
    filled = lambda b: sum(image(b).pixelColor(x, 32).alpha() > 200 for x in range(64))  # noqa: E731
    assert filled({"level": 20}) < filled({"level": 90})


def test_battery_text():
    assert battery_text(None) == ""
    assert battery_text({"level": None}) == "—"
    assert battery_text({"level": 80, "charging": True}) == "80%, charging"
