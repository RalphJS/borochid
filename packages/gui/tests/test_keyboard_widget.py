"""The keyboard widget (per-key painting and key picking), run headless."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QEvent, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QMouseEvent  # noqa: E402

from borochid.gui.keyboard_widget import KeyboardView, PaintBar, parse_keys  # noqa: E402
from borochid.gui.widgets import PanelContext, build_form  # noqa: E402

# A tiny board: two lit keys with usages, a lit G key without one, a key
# with neither, and Super (locked in game mode).
LAYOUT = {
    "keys": [
        {"id": "a", "label": "A", "x": 0, "y": 0, "led": 1, "usage": 4},
        {"id": "b", "label": "B", "x": 1, "y": 0, "led": 2, "usage": 5},
        {"id": "g1", "label": "G1", "x": 2, "y": 0, "led": 180},
        {"id": "m1", "label": "M1", "x": 3, "y": 0, "h": 0.75},
        {"id": "super", "label": "Super", "cap": "❖", "x": 0, "y": 1, "w": 2, "led": 107, "usage": 227},
    ]
}
PAINT = {"widget": "keyboard", "label": "", "layout": "board", "mode": "paint", "state": "lighting.keys",
         "base": "lighting.color", "action": "set_key_colors", "clear": "clear_key_colors"}
TOGGLE = {"widget": "keyboard", "label": "", "layout": "board", "mode": "toggle", "state": "game_mode_keys",
          "action": "set_game_mode_keys", "param": "keys", "locked": [227]}


_alive = []  # Qt deletes a form once Python drops it; tests often only keep the view


def build(item):
    sent = []
    ctx = PanelContext(lambda action, params: sent.append((action, params)), {"board": LAYOUT})
    form = build_form([item], ctx)
    form.resize(400, 300)
    form.show()
    QtWidgets.QApplication.processEvents()
    _alive.append(form)
    return form, ctx, sent, form.findChild(KeyboardView)


def mouse(view, kind, key_id, buttons=Qt.MouseButton.LeftButton):
    k = next(k for k in view.keys if k.id == key_id)
    pos = view.key_rect(k).center()
    held = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else buttons
    event = QMouseEvent(kind, pos, view.mapToGlobal(pos), Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton,
                        held, Qt.KeyboardModifier.NoModifier)
    QtWidgets.QApplication.sendEvent(view, event)


def stroke(view, *key_ids):
    mouse(view, QEvent.Type.MouseButtonPress, key_ids[0])
    for k in key_ids[1:]:
        mouse(view, QEvent.Type.MouseMove, k)
    mouse(view, QEvent.Type.MouseButtonRelease, key_ids[-1])


def test_layout_comes_from_the_named_package_section_or_inline(app):
    _, _, _, view = build(PAINT)
    assert [k.id for k in view.keys] == ["a", "b", "g1", "m1", "super"]
    assert view.keys[4].cap == "❖" and view.keys[0].cap == "A"  # cap falls back to the label
    _, _, _, inline = build({**PAINT, "layout": LAYOUT})
    assert len(inline.keys) == 5
    missing = build_form([{**PAINT, "layout": "nope"}], PanelContext(lambda *_: None))
    assert missing.findChild(KeyboardView) is None


def test_bad_key_entries_are_skipped():
    keys = parse_keys({"keys": [{"id": "ok", "x": 0, "y": 0}, {"id": "no-pos"}, {"x": -1, "y": 0}, "junk",
                                {"id": "wide", "x": 0, "y": 0, "w": 99}, {"id": "led", "x": 1, "y": 0, "led": True}]})
    assert [k.id for k in keys] == ["ok", "led"] and keys[1].led is None
    assert parse_keys("keyboard") == [] and parse_keys({"keys": {}}) == []


def test_the_drawing_keeps_the_key_maps_aspect_ratio(app):
    _, _, _, view = build(PAINT)
    assert view.hasHeightForWidth()
    assert view.heightForWidth(400) == 200  # 4 units wide, 2 tall


def test_a_stroke_paints_the_keys_it_crosses_in_one_action(app):
    form, ctx, sent, view = build(PAINT)
    form.findChild(PaintBar).set_color(QColor("#ff8000"))
    stroke(view, "a", "m1", "b", "a")  # M1 has no LED; A only once
    assert sent == [("set_key_colors", {"keys": [1, 2], "color": "#ff8000"})]
    assert view.colors[1].name() == "#ff8000"  # shown before the service answers


def test_the_eraser_sends_null(app):
    form, _, sent, view = build(PAINT)
    bar = form.findChild(PaintBar)
    bar.eraser.setChecked(True)
    stroke(view, "g1")
    assert sent == [("set_key_colors", {"keys": [180], "color": None})]
    bar.set_color(QColor("#00ff00"))  # picking a colour leaves the eraser
    assert not bar.eraser.isChecked() and view.brush.name() == "#00ff00"


def test_fill_all_and_clear(app):
    form, _, sent, view = build(PAINT)
    bar = form.findChild(PaintBar)
    bar.set_color(QColor("#123456"))
    bar.fill.click()
    bar.clear.click()
    assert sent == [("set_key_colors", {"keys": [1, 2, 107, 180], "color": "#123456"}), ("clear_key_colors", {})]
    assert view.colors == {}


def test_state_updates_repaint_and_unpainted_keys_show_the_base(app):
    _, ctx, sent, view = build(PAINT)
    ctx.apply({"lighting.color": "#0000ff", "lighting.keys": {"1": "#ff0000", "x": "#fff", "2": "not a colour"}})
    a, b = view.keys[0], view.keys[1]
    assert view.color_of(a).name() == "#ff0000" and view.color_of(b).name() == "#0000ff"
    assert sent == []  # state never echoes back to the device


def test_toggle_sends_the_whole_sorted_list(app):
    _, ctx, sent, view = build(TOGGLE)
    ctx.apply({"game_mode_keys": [5]})
    stroke(view, "a")
    assert sent == [("set_game_mode_keys", {"keys": [4, 5]})]
    stroke(view, "b")
    assert sent[-1] == ("set_game_mode_keys", {"keys": [4]})


def test_locked_keys_and_keys_without_a_usage_cant_be_toggled(app):
    _, ctx, sent, view = build(TOGGLE)
    ctx.apply({"game_mode_keys": []})
    for key in ("super", "g1", "m1"):
        stroke(view, key)
    assert sent == []
    stroke(view, "a")
    assert sent == [("set_game_mode_keys", {"keys": [4]})]  # the locked Super isn't sent


def test_enabled_if_disables_painting(app):
    _, ctx, sent, view = build({**PAINT, "enabled_if": "lighting.per_key"})
    ctx.apply({"lighting.per_key": False})
    stroke(view, "a")
    assert sent == []
    ctx.apply({"lighting.per_key": True})
    stroke(view, "a")
    assert len(sent) == 1


def test_pick_opens_the_registered_editor_and_adds_keys_when_asked(app):
    opened = []
    item = {"widget": "keyboard", "label": "", "layout": "board", "mode": "pick", "keys": ["g1"],
            "opens": "set_binding", "keys_when": {"x_mode": ["m1"]}}
    _, ctx, sent, view = build(item)
    for key_id in ("g1", "m1"):
        ctx.editors[("set_binding", key_id)] = lambda key_id=key_id: opened.append(key_id)
    stroke(view, "m1")  # not a macro key yet
    stroke(view, "g1")
    ctx.apply({"x_mode": True})
    stroke(view, "m1")
    ctx.apply({"x_mode": False})
    stroke(view, "m1")
    assert opened == ["g1", "m1"] and sent == []


def test_preview_is_read_only_and_dimmed_by_brightness(app):
    item = {"widget": "keyboard", "label": "", "layout": "board", "mode": "preview",
            "state": "lighting.keys", "base": "lighting.color", "brightness": "brightness"}
    _, ctx, sent, view = build(item)
    ctx.apply({"lighting.color": "#ff0000", "brightness": 50})
    stroke(view, "a")
    fill, _ = view._colors_for(view.keys[0], True, view.palette())
    assert sent == [] and fill.red() == 128 and fill.green() == 0
