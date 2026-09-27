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


def test_icon_names_from_packages_cannot_be_paths(app):
    from borochid.gui.widgets import theme_icon

    assert theme_icon("/usr/share/icons/breeze/actions/16/view-refresh.svg") is None
    assert theme_icon("../view-refresh") is None
    assert theme_icon(None) is None


def test_compact_status_flattens_groups_and_keeps_enabled_if(app):
    from borochid.gui.widgets import build_compact

    ctx = PanelContext(lambda *_: None)
    host = build_compact(
        [{"widget": "group", "label": "Status", "children": [
            {"widget": "readout", "label": "Link", "state": "link"},
            {"widget": "button", "text": "Refresh", "icon": "view-refresh", "action": "r", "enabled_if": "online"},
        ]}],
        ctx,
    )
    assert host.findChild(QtWidgets.QGroupBox) is None
    assert "Link:" in [w.text() for w in host.findChildren(QtWidgets.QLabel)]
    button = host.findChild(QtWidgets.QPushButton)
    assert not button.isEnabled()
    ctx.apply({"online": True})
    assert button.isEnabled()


def test_color_opens_from_the_swatch_or_the_picker(app, monkeypatch):
    from PyQt6.QtGui import QColor

    sent = []
    ctx = PanelContext(lambda action, params: sent.append(params["value"]))
    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor", lambda *a: QColor("#123456"))
    form = build_form([{"widget": "color", "label": "Logo", "state": "c", "action": "set", "param": "value"}], ctx)
    swatch = form.findChild(QtWidgets.QPushButton)
    picker = form.findChild(QtWidgets.QToolButton)
    swatch.click()
    picker.click()
    assert sent == ["#123456", "#123456"]


def test_row_puts_children_side_by_side_with_inline_labels(app):
    ctx = PanelContext(lambda *_: None)
    form = build_form(
        [{"widget": "row", "label": "Microphone", "children": [
            {"widget": "color", "tooltip": "Microphone color", "state": "a", "action": "x", "param": "v"},
            {"widget": "color", "label": "Muted", "state": "b", "action": "y", "param": "v"},
            {"widget": "slider", "tooltip": "Brightness", "state": "c", "action": "z", "param": "v", "enabled_if": "on"},
        ]}],
        ctx,
    )
    labels = [w.text() for w in form.findChildren(QtWidgets.QLabel)]
    assert "Microphone" in labels and "Muted" in labels
    slider = form.findChild(QtWidgets.QSlider)
    assert slider.parentWidget().toolTip() == "Brightness" and not slider.parentWidget().isEnabled()
    ctx.apply({"on": True})
    assert slider.parentWidget().isEnabled()


def test_icon_lists_pick_the_first_the_theme_has(app, monkeypatch):
    from PyQt6.QtGui import QIcon

    from borochid.gui.widgets import theme_icon

    monkeypatch.setattr(QIcon, "hasThemeIcon", staticmethod(lambda n: n == "b"))
    monkeypatch.setattr(QIcon, "fromTheme", staticmethod(lambda n: n))
    assert theme_icon(["a", "b"]) == "b"
    assert theme_icon(["a", "/b"], "b") == "b"


def test_compound_widgets_keep_their_parts_together_in_a_wide_form(app):
    ctx = PanelContext(lambda *_: None)
    form = build_form(
        [{"widget": "color", "label": "Muted", "state": "c", "action": "x", "param": "v"},
         {"widget": "readout", "label": "Mic", "state": "m", "icon": "audio-volume-high", "map": {"true": "Muted"}}],
        ctx,
    )
    form.resize(800, 200)
    form.show()
    app.processEvents()
    swatch, picker = form.findChild(QtWidgets.QPushButton), form.findChild(QtWidgets.QToolButton)
    assert picker.x() - (swatch.x() + swatch.width()) < 16
    form.close()
