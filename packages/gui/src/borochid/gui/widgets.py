"""Builds device control panels from a package's ``ui`` schema.

Each schema item names a ``widget`` type. Builders are registered with
``@widget("name")``; adding a new control type means adding one function.
Common keys: ``label``, ``state`` (state key the widget displays),
``action`` + ``param`` (what the widget invokes when the user changes it),
``enabled_if`` (state key; the widget is disabled while it is falsy).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
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


def build_form(items: list[dict[str, Any]], ctx: PanelContext, parent: QWidget | None = None) -> QWidget:
    host = QWidget(parent)
    form = QFormLayout(host)
    for item in items:
        builder = _BUILDERS.get(item.get("widget", ""))
        if builder is None:
            form.addRow(item.get("label", ""), QLabel(f"(unsupported widget {item.get('widget')!r})"))
            continue
        w = builder(item, ctx)
        if key := item.get("enabled_if"):
            w.setEnabled(False)
            ctx.watch(key, lambda v, w=w: w.setEnabled(bool(v)))
        if item.get("widget") == "group":
            form.addRow(w)
        else:
            form.addRow(item.get("label", ""), w)
    return host


def _silently(w: QWidget, fn: Callable[[], None]) -> None:
    """Apply a state update without echoing it back to the device."""
    w.blockSignals(True)
    try:
        fn()
    finally:
        w.blockSignals(False)


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
    """``map`` translates raw values ({"true": "Charging", "null": "—"})."""
    lbl = QLabel("—")
    fmt, suffix, mapping = item.get("format", "{}"), item.get("suffix", ""), item.get("map", {})

    def show(v):
        if _map_key(v) in mapping:
            lbl.setText(mapping[_map_key(v)])
            return
        try:
            lbl.setText("—" if v is None else fmt.format(v) + suffix)
        except (ValueError, TypeError):
            lbl.setText(str(v))

    ctx.watch(item.get("state"), show)
    return lbl


@widget("slider")
def _slider(item, ctx):
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
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


@widget("color")
def _color(item, ctx):
    btn = QPushButton()
    btn.setFixedWidth(80)
    current = {"c": QColor("#ffffff")}

    def paint(c: QColor):
        current["c"] = c
        btn.setStyleSheet(f"background-color: {c.name()}; border: 1px solid palette(mid); min-height: 22px;")

    def pick():
        c = QColorDialog.getColor(current["c"], btn, item.get("label", "Color"))
        if c.isValid():
            paint(c)
            ctx.send(item, c.name())

    btn.clicked.connect(pick)
    ctx.watch(item.get("state"), lambda v: v and paint(QColor(v)))
    paint(current["c"])
    return btn


@widget("button")
def _button(item, ctx):
    b = QPushButton(item.get("text", item.get("label", "Run")))
    b.clicked.connect(lambda: ctx.send(item))
    return b
