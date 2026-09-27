"""Builds device control panels from a package's ``ui`` schema.

Each schema item names a ``widget`` type. Builders are registered with
``@widget("name")``; adding a new control type means adding one function.
Common keys: ``label``, ``state`` (state key the widget displays),
``action`` + ``param`` (what the widget invokes when the user changes it),
``enabled_if`` (state key; the widget is disabled while it is falsy).

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
        w.setEnabled(False)
        ctx.watch(key, lambda v, w=w: w.setEnabled(bool(v)))
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
        if item.get("widget") in ("group", "form"):
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
    return str(v).lower() if isinstance(v, bool) or v is None else str(v)


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
