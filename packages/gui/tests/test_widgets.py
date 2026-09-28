"""Schema-driven panel building, run headless."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from borochid.gui.widgets import PanelContext, build_form  # noqa: E402


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


def test_enabled_if_can_be_negated(app):
    ctx = PanelContext(lambda *_: None)
    form = build_form([{"widget": "button", "text": "Go", "action": "go", "enabled_if": "!busy"}], ctx)
    button = form.findChild(QtWidgets.QPushButton)
    assert button.isEnabled()
    ctx.apply({"busy": True})
    assert not button.isEnabled()


CATEGORIES = [
    {"label": "Shortcut", "kind": "keys"},
    {"label": "Mouse", "options": [{"value": {"button": 1}, "label": "Left click"}, {"value": {"button": 4}, "label": "Back"}]},
    {"label": "DPI", "options": [{"value": "dpi_up", "label": "DPI up"}]},
    {"label": "Nothing", "options": [{"value": "disabled", "label": "Do nothing"}]},
]


def key_event(scancode, press=True, key=0):
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent

    kind = QEvent.Type.KeyPress if press else QEvent.Type.KeyRelease
    return QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier, scancode, 0, 0, "")


def test_buttons_list_spells_out_each_binding(app):
    from borochid.gui.binding_editor import describe

    ctx = PanelContext(lambda *_: None)
    form = build_form(
        [{"widget": "buttons", "action": "set_binding", "buttons": [{"id": "g4", "label": "G4 (back)"}, {"id": "g8", "label": "G8"}], "categories": CATEGORIES}],
        ctx,
    )
    ctx.apply({"bind.g4": {"keys": ["KEY_LEFTMETA", "KEY_V"]}, "bind.g8": "dpi_up"})
    texts = [b.text() for b in form.findChildren(QtWidgets.QPushButton)]
    assert "Super+V" in texts and "DPI up" in texts
    assert describe({"button": 4}, CATEGORIES) == "Back"


def test_recorder_records_a_combination_by_physical_key(app):
    from borochid.gui.binding_editor import ShortcutRecorder

    rec = ShortcutRecorder()
    rec.start()
    # Super (evdev 125) held, then "4" (evdev 5): XKB codes are evdev + 8.
    rec.keyPressEvent(key_event(125 + 8))
    rec.keyPressEvent(key_event(42 + 8))  # Shift
    assert rec.chord() == []  # modifiers alone aren't a shortcut
    rec.keyPressEvent(key_event(5 + 8))
    assert rec.chord() == ["KEY_LEFTSHIFT", "KEY_LEFTMETA", "KEY_4"] and not rec.recording
    rec.releaseKeyboard()


def test_recorder_modifier_buttons_cover_shortcuts_the_desktop_keeps(app):
    from borochid.gui.binding_editor import ShortcutRecorder

    rec = ShortcutRecorder()
    rec.start()
    rec.keyPressEvent(key_event(47 + 8))  # "V"; the desktop swallowed Super
    rec.toggles["KEY_LEFTMETA"].setChecked(True)
    assert rec.chord() == ["KEY_LEFTMETA", "KEY_V"]


def test_recorder_ignores_keys_a_button_may_not_send(app):
    from borochid.gui.binding_editor import ShortcutRecorder

    rec = ShortcutRecorder()
    rec.start()
    rec.keyPressEvent(key_event(116 + 8))  # KEY_POWER
    assert rec.chord() == [] and rec.recording
    rec.stop()


def test_binding_dialog_opens_on_the_current_choice(app):
    from borochid.gui.binding_editor import BindingDialog

    dlg = BindingDialog(None, "G4", CATEGORIES, {"button": 4}, can_reset=True)
    assert dlg.nav.currentRow() == 1 and dlg.selected() == {"button": 4}
    dlg.nav.setCurrentRow(3)
    dlg.pages.currentWidget().setCurrentRow(0)
    assert dlg.selected() == "disabled"
    keys = BindingDialog(None, "G5", CATEGORIES, {"keys": ["KEY_LEFTCTRL", "KEY_C"]}, can_reset=False)
    assert keys.nav.currentRow() == 0 and keys.selected() == {"keys": ["KEY_LEFTCTRL", "KEY_C"]}


def test_stages_widget(app):
    sent = []
    ctx = PanelContext(lambda action, params: sent.append((action, params)))
    form = build_form(
        [{"widget": "stages", "state": "stages", "current": "stage", "default": "default_stage", "min": 100, "max": 25600, "step": 50,
          "set": "set_stage", "add": "add_stage", "remove": "remove_stage", "make_default": "set_default_stage", "select": "select_stage"}],
        ctx,
    )
    ctx.apply({"stages": [800, 1600, 3200], "stage": 2, "default_stage": 2})
    spins = form.findChildren(QtWidgets.QSpinBox)
    assert [s.value() for s in spins] == [800, 1600, 3200]
    stars = [b.text() for b in form.findChildren(QtWidgets.QToolButton) if b.text() in "★☆"]
    assert stars == ["☆", "★", "☆"]
    spins[0].setValue(900)
    spins[0].editingFinished.emit()
    assert sent == [("set_stage", {"stage": 1, "value": 900})]



def test_readout_maps_null(app):
    ctx = PanelContext(lambda *_: None)
    form = build_form([{"widget": "readout", "label": "Input", "state": "err", "map": {"null": "Ready"}}], ctx)
    ctx.apply({"err": None})
    assert "Ready" in [w.text() for w in form.findChildren(QtWidgets.QLabel)]


def test_keys_the_desktop_keeps_can_be_chosen_from_the_menu(app):
    from borochid.gui.binding_editor import ShortcutRecorder

    rec = ShortcutRecorder()
    menu = rec.choose_button.menu()
    system = next(a.menu() for a in menu.actions() if a.text() == "System")
    print_screen = next(a for a in system.actions() if a.text() == "Print")
    rec.toggles["KEY_LEFTMETA"].setChecked(True)
    print_screen.trigger()
    assert rec.chord() == ["KEY_LEFTMETA", "KEY_SYSRQ"]


def test_visible_if_hides_a_widget_and_its_row_label(app):
    ctx = PanelContext(lambda *_: None)
    form = build_form(
        [{"widget": "row", "label": "", "children": [
            {"widget": "readout", "state": "name"},
            {"widget": "readout", "label": "·", "state": "sub", "visible_if": "!x_mode"},
            {"widget": "readout", "label": "·", "state": "x_mode", "visible_if": "x_mode"},
        ]}],
        ctx,
    )
    form.show()

    def shown():
        return [w.text() for w in form.findChildren(QtWidgets.QLabel) if w.isVisible() and w.text()]

    ctx.apply({"name": "Default", "sub": 2, "x_mode": False})
    assert shown() == ["Default", "·", "2"]
    ctx.apply({"x_mode": True})
    assert shown() == ["Default", "·", "True"]


def test_visible_if_on_a_form_row_hides_its_label_too(app):
    ctx = PanelContext(lambda *_: None)
    form = build_form([{"widget": "toggle", "label": "M lights", "state": "a", "visible_if": "x_mode"}], ctx)
    form.show()
    label = next(w for w in form.findChildren(QtWidgets.QLabel) if w.text() == "M lights")
    ctx.apply({"x_mode": False})
    hidden = label.isVisible()
    ctx.apply({"x_mode": True})
    assert not hidden and label.isVisible()
