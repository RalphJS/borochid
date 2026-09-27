"""Schema-driven panel building, run headless."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from borochid.gui.widgets import PanelContext, build_form  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_state_updates_reach_widgets_without_echoing_to_device(app):
    sent = []
    ctx = PanelContext(lambda action, params: sent.append((action, params)))
    form = build_form(
        [
            {"widget": "readout", "label": "Battery", "state": "battery", "suffix": "%"},
            {"widget": "toggle", "label": "Lock", "state": "locked", "action": "set_lock", "param": "on"},
            {"widget": "mystery", "label": "?"},
        ],
        ctx,
    )
    ctx.apply({"battery": 42, "locked": True})
    labels = [w.text() for w in form.findChildren(QtWidgets.QLabel)]
    assert "42%" in labels
    assert any("unsupported widget" in t for t in labels)
    toggle = form.findChild(QtWidgets.QCheckBox)
    assert toggle.isChecked() and sent == []

    toggle.setChecked(False)
    assert sent == [("set_lock", {"on": False})]


def test_enabled_if_and_readout_map(app):
    ctx = PanelContext(lambda *_: None)
    form = build_form(
        [
            {"widget": "readout", "label": "Charging", "state": "charging", "map": {"true": "Charging", "false": "On battery"}},
            {"widget": "button", "label": "", "text": "Go", "action": "go", "enabled_if": "wireless"},
        ],
        ctx,
    )
    button = form.findChild(QtWidgets.QPushButton)
    assert not button.isEnabled()
    ctx.apply({"wireless": True, "charging": False})
    assert button.isEnabled()
    assert "On battery" in [w.text() for w in form.findChildren(QtWidgets.QLabel)]
