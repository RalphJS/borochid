"""Builds device control panels from a package's ``ui`` schema.

Each schema item names a ``widget`` type. Builders are registered with
``@widget("name")``; adding a new control type means adding one function.
Common keys: ``label``, ``state`` (state key the widget displays),
``action`` + ``param`` (what the widget invokes when the user changes it),
``enabled_if`` (state key; the widget is disabled while it is falsy, or
while it is truthy with a leading ``!``).

``tooltip`` sets hover text on any widget.

Icons are names from the desktop icon theme (``"view-refresh"``), never
files, so packages don't ship images for them. A list of names picks the
first one the theme has (themes name things differently); unknown names
show nothing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFrame,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

Invoke = Callable[[str, dict[str, Any]], None]


class PanelContext:
    def __init__(self, invoke: Invoke):
        self.invoke = invoke
        self._watchers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)

    def watch(self, key: str | None, setter: Callable[[Any], None]) -> None:
        if key:
            self._watchers[key].append(setter)

    def apply(self, changes: dict[str, Any]) -> None:
        for key, value in changes.items():
            for setter in self._watchers.get(key, []):
                setter(value)

    def send(self, item: dict[str, Any], value: Any = None) -> None:
        if not (action := item.get("action")):
            return
        params = dict(item.get("params", {}))
        if item.get("param"):
            params[item["param"]] = value
        self.invoke(action, params)


Builder = Callable[[dict[str, Any], PanelContext], QWidget]
_BUILDERS: dict[str, Builder] = {}


def widget(name: str) -> Callable[[Builder], Builder]:
    def register(fn: Builder) -> Builder:
        _BUILDERS[name] = fn
        return fn

    return register


def _build(item: dict[str, Any], ctx: PanelContext) -> QWidget:
    builder = _BUILDERS.get(item.get("widget", ""))
    if builder is None:
        return QLabel(f"(unsupported widget {item.get('widget')!r})")
    w = builder(item, ctx)
    if tip := item.get("tooltip"):
        w.setToolTip(str(tip))
    if key := item.get("enabled_if"):
        # "!key" enables the widget while the key is falsy instead.
        negate = key.startswith("!")
        w.setEnabled(negate)
        ctx.watch(key.lstrip("!"), lambda v, w=w: w.setEnabled(bool(v) != negate))
    return w


def build_compact(items: list[dict[str, Any]], ctx: PanelContext, parent: QWidget | None = None) -> QWidget:
    """A centred column without group frames, for status beside the device's
    picture. Groups are flattened; an item with an icon shows its label as a
    tooltip instead of text."""
    host = QWidget(parent)
    col = QVBoxLayout(host)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(6)
    flat = [c for i in items for c in (i.get("children", []) if i.get("widget") == "group" else [i])]
    for item in flat:
        w = _build(item, ctx)
        label = item.get("label", "")
        if label and "icon" not in item:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(QLabel(f"{label}:"))
            line.addWidget(w)
            w = row
        elif label:
            w.setToolTip(label)
        col.addWidget(w, 0, Qt.AlignmentFlag.AlignHCenter)
    return host


def build_form(items: list[dict[str, Any]], ctx: PanelContext, parent: QWidget | None = None) -> QWidget:
    host = QWidget(parent)
    form = QFormLayout(host)
    for item in items:
        w = _build(item, ctx)
        if item.get("widget") in ("group", "form", "buttons"):
            form.addRow(w)
        else:
            form.addRow(item.get("label", ""), w)
    return host


_ICON_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ICON_PX = 22


def theme_icon(*names: Any) -> QIcon | None:
    """The first of ``names`` the icon theme has; a name may also be a list
    of names. Only plain names are looked up: package data must not be able
    to point Qt at a file."""
    for name in [n for spec in names for n in (spec if isinstance(spec, list) else [spec])]:
        if isinstance(name, str) and _ICON_NAME_RE.match(name) and QIcon.hasThemeIcon(name):
            return QIcon.fromTheme(name)
    return None


class IconText(QWidget):
    """An icon followed by text; the icon is hidden when there is none."""

    def __init__(self, text: str = "—"):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.icon = QLabel()
        self.icon.hide()
        self.text = QLabel(text)
        row.addWidget(self.icon)
        row.addWidget(self.text)
        row.addStretch(1)  # icon and text stay together when given a wide cell

    def set_icon(self, icon: QIcon | None) -> None:
        if icon is None:
            self.icon.hide()
        else:
            self.icon.setPixmap(icon.pixmap(ICON_PX, ICON_PX))
            self.icon.show()


def _silently(w: QWidget, fn: Callable[[], None]) -> None:
    """Apply a state update without echoing it back to the device."""
    w.blockSignals(True)
    try:
        fn()
    finally:
        w.blockSignals(False)


@widget("form")
def _form(item, ctx):
    """A group's children without the frame (settings tabs use it)."""
    return build_form(item.get("children", []), ctx)


@widget("group")
def _group(item, ctx):
    box = QGroupBox(item.get("label", ""))
    layout = QHBoxLayout(box)
    layout.addWidget(build_form(item.get("children", []), ctx))
    return box


def _map_key(v: Any) -> str:
    if v is None:
        return "null"  # JSON's name, as manifests write it
    return str(v).lower() if isinstance(v, bool) else str(v)


@widget("readout")
def _readout(item, ctx):
    """``map`` translates raw values ({"true": "Charging", "null": "—"}).
    ``icon`` is a theme icon name, or ``{"map": {value: name}}`` to change
    with the value."""
    out = IconText()
    lbl = out.text
    fmt, suffix, mapping = item.get("format", "{}"), item.get("suffix", ""), item.get("map", {})
    icon = item.get("icon")
    if isinstance(icon, (str, list)):
        out.set_icon(theme_icon(icon))

    def show(v):
        if isinstance(icon, dict):
            out.set_icon(theme_icon(icon.get("map", {}).get(_map_key(v), "")))
        if _map_key(v) in mapping:
            lbl.setText(mapping[_map_key(v)])
            return
        try:
            lbl.setText("—" if v is None else fmt.format(v) + suffix)
        except (ValueError, TypeError):
            lbl.setText(str(v))

    ctx.watch(item.get("state"), show)
    return out


@widget("slider")
def _slider(item, ctx):
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    if icon := theme_icon(item.get("icon", "")):
        lead = QLabel()
        lead.setPixmap(icon.pixmap(ICON_PX, ICON_PX))
        row.addWidget(lead)
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(int(item.get("min", 0)), int(item.get("max", 100)))
    s.setSingleStep(int(item.get("step", 1)))
    value = QLabel(str(s.value()))
    value.setMinimumWidth(36)
    row.addWidget(s)
    row.addWidget(value)
    # Debounce so dragging does not flood the device.
    debounce = QTimer(host, singleShot=True, interval=int(item.get("debounce_ms", 80)))
    debounce.timeout.connect(lambda: ctx.send(item, s.value()))
    s.valueChanged.connect(lambda v: (value.setText(str(v)), debounce.start()))
    ctx.watch(item.get("state"), lambda v: v is not None and _silently(s, lambda: (s.setValue(int(v)), value.setText(str(int(v))))))
    return host


@widget("spin")
def _spin(item, ctx):
    s = QSpinBox()
    s.setRange(int(item.get("min", 0)), int(item.get("max", 100)))
    s.editingFinished.connect(lambda: ctx.send(item, s.value()))
    ctx.watch(item.get("state"), lambda v: v is not None and _silently(s, lambda: s.setValue(int(v))))
    return s


@widget("toggle")
def _toggle(item, ctx):
    c = QCheckBox()
    c.toggled.connect(lambda on: ctx.send(item, on))
    ctx.watch(item.get("state"), lambda v: _silently(c, lambda: c.setChecked(bool(v))))
    return c


@widget("select")
def _select(item, ctx):
    cb = QComboBox()
    for opt in item.get("options", []):
        cb.addItem(str(opt.get("label", opt["value"])), opt["value"])
    cb.activated.connect(lambda i: ctx.send(item, cb.itemData(i)))

    def show(v):
        if (i := cb.findData(v)) >= 0:
            _silently(cb, lambda: cb.setCurrentIndex(i))

    ctx.watch(item.get("state"), show)
    return cb


_PICKER_ICONS = ["color-picker", "color-select-symbolic", "preferences-color-symbolic", "color-management"]


@widget("color")
def _color(item, ctx):
    """A swatch of the current colour and a colour-picker button; either
    opens the colour chooser."""
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(2)
    swatch = QPushButton()
    swatch.setFixedSize(28, 22)
    swatch.setCursor(Qt.CursorShape.PointingHandCursor)
    picker = QToolButton()
    picker.setAutoRaise(True)
    if icon := theme_icon(_PICKER_ICONS):
        picker.setIcon(icon)
    else:
        picker.setText("…")
    picker.setToolTip("Choose color")
    row.addWidget(swatch)
    row.addWidget(picker)
    row.addStretch(1)  # swatch and picker stay together when given a wide cell
    current = {"c": QColor("#ffffff")}

    def paint(c: QColor):
        current["c"] = c
        swatch.setStyleSheet(f"background-color: {c.name()}; border: 1px solid palette(mid); border-radius: 4px;")

    def pick():
        c = QColorDialog.getColor(current["c"], host, item.get("tooltip") or item.get("label") or "Color")
        if c.isValid():
            paint(c)
            ctx.send(item, c.name())

    swatch.clicked.connect(pick)
    picker.clicked.connect(pick)
    ctx.watch(item.get("state"), lambda v: v and paint(QColor(v)))
    paint(current["c"])
    return host


@widget("row")
def _row(item, ctx):
    """Children side by side, e.g. a light's colour and brightness. A child's
    ``label`` is shown just before it; sliders take the spare width."""
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    stretchy = False
    for child in item.get("children", []):
        if child.get("label"):
            row.addWidget(QLabel(child["label"]))
        is_slider = child.get("widget") == "slider"
        stretchy |= is_slider
        row.addWidget(_build(child, ctx), 1 if is_slider else 0)
    if not stretchy:
        row.addStretch(1)
    return host


@widget("button")
def _button(item, ctx):
    b = QPushButton(item.get("text", item.get("label", "Run")))
    if icon := theme_icon(item.get("icon", "")):
        b.setIcon(icon)
    if not b.text() and icon:
        b.setToolTip(item.get("label", ""))
    b.clicked.connect(lambda: ctx.send(item))
    return b


_ADD_ICONS = ["list-add", "list-add-symbolic"]
_REMOVE_ICONS = ["edit-delete", "list-remove", "user-trash"]


def _tool(icons: list[str], fallback: str, tip: str) -> QToolButton:
    b = QToolButton()
    b.setAutoRaise(True)
    if icon := theme_icon(icons):
        b.setIcon(icon)
    else:
        b.setText(fallback)
    b.setToolTip(tip)
    return b


class _StageCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked: Callable[[], None] = lambda: None

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self.clicked()
        super().mousePressEvent(event)


@widget("stages")
def _stages(item, ctx):
    """An editable list of values (DPI stages), one row each: number, value,
    a star for the default and a remove button. Clicking a row makes it
    current; "Add stage" below adds one. Keys: ``state`` (the list),
    ``current`` and ``default`` (1-based), ``min``/``max``/``step``,
    ``max_items``, and the actions ``set`` ({stage, value}), ``add``
    ({value}), ``remove``, ``make_default`` and ``select`` ({stage})."""
    host = QWidget()
    col = QVBoxLayout(host)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(4)
    known: dict[str, Any] = {"values": [], "current": None, "default": None}
    lo, hi, step = int(item.get("min", 0)), int(item.get("max", 100000)), int(item.get("step", 1))
    suffix = item.get("suffix", "")

    def rebuild():
        while col.count():
            if w := col.takeAt(0).widget():
                w.setParent(None)  # gone now, not at the next event loop turn
                w.deleteLater()
        values = known["values"]
        for i, value in enumerate(values, 1):
            card = _StageCard()
            active = i == known["current"]
            card.setStyleSheet(
                "_StageCard { border: 2px solid palette(highlight); border-radius: 6px; }" if active
                else "_StageCard { border: 1px solid palette(mid); border-radius: 6px; }"
            )
            line = QHBoxLayout(card)
            line.setContentsMargins(8, 3, 4, 3)
            num = QLabel(f"<b>{i}</b>")
            num.setMinimumWidth(16)
            spin = QSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(step)
            spin.setSuffix(suffix)
            spin.setValue(int(value))
            spin.setMinimumWidth(90)
            spin.editingFinished.connect(lambda i=i, spin=spin: spin.value() != known["values"][i - 1] and ctx.invoke(item["set"], {"stage": i, "value": spin.value()}))
            star = QToolButton()
            star.setAutoRaise(True)
            is_default = i == known["default"]
            star.setText("★" if is_default else "☆")
            star.setToolTip("Default stage (the profile starts here)" if is_default else "Make this the default stage")
            star.clicked.connect(lambda _=False, i=i: ctx.invoke(item["make_default"], {"stage": i}))
            drop = _tool(_REMOVE_ICONS, "×", "Remove this stage")
            drop.setEnabled(len(values) > 1)
            drop.clicked.connect(lambda _=False, i=i: ctx.invoke(item["remove"], {"stage": i}))
            line.addWidget(num)
            line.addWidget(spin, 1)
            line.addWidget(star)
            line.addWidget(drop)
            card.clicked = lambda i=i: i != known["current"] and ctx.invoke(item["select"], {"stage": i})
            card.setToolTip("Active stage" if active else "Click to switch to this stage")
            col.addWidget(card)
        add = QPushButton("Add stage")
        if icon := theme_icon(_ADD_ICONS):
            add.setIcon(icon)
        add.setEnabled(len(values) < int(item.get("max_items", 5)))
        add.clicked.connect(lambda: ctx.invoke(item["add"], {}))
        col.addWidget(add, 0, Qt.AlignmentFlag.AlignLeft)

    def update(key):
        def set_(v):
            v = list(v) if key == "values" and isinstance(v, list) else v
            if known[key] != v:  # a rebuild would drop a value being typed
                known[key] = v
                rebuild()

        return set_

    ctx.watch(item.get("state"), update("values"))
    ctx.watch(item.get("current"), update("current"))
    ctx.watch(item.get("default"), update("default"))
    return host


# Widgets that live in their own modules register themselves on import.
from borochid.gui import binding_editor  # noqa: E402,F401
