"""The ``keyboard`` widget: a drawing of the device's keys, from a key map in
the package, for per-key lighting (``"mode": "paint"``) or for picking keys
(``"mode": "toggle"``, e.g. the keys game mode disables).

The key map is a top-level manifest section named by ``layout`` (the
service sends it with the UI), or an inline ``{"keys": [...]}``::

    {"id": "esc", "label": "Escape", "cap": "Esc", "x": 1.25, "y": 1,
     "w": 1, "h": 1, "led": 38, "usage": 41}

``x``/``y``/``w``/``h`` are in key units (``w`` and ``h`` default to 1),
``label`` names the key (tooltips), ``cap`` is the short text drawn on it
(``label`` when absent), ``led`` is its lighting zone (no ``led``: it can't
be painted) and ``usage`` its HID keyboard usage (no ``usage``: it can't be
picked).

Paint mode::

    {"widget": "keyboard", "layout": "keyboard", "mode": "paint",
     "state": "lighting.keys", "base": "lighting.color",
     "action": "set_key_colors", "clear": "clear_key_colors"}

``state`` maps LED ids (as strings) to ``"#rrggbb"``; other keys show the
``base`` state's colour. Each stroke (click, or drag across keys) sends one
``action`` with ``{"keys": [led ids], "color": "#rrggbb"}``, or ``"color":
null`` with the eraser (back to the base colour). "Fill all" paints every
key; "Clear" invokes ``clear`` with ``{}``.

Toggle mode::

    {"widget": "keyboard", "layout": "keyboard", "mode": "toggle",
     "state": "game_mode_keys", "action": "set_game_mode_keys",
     "param": "keys", "locked": [227]}

``state`` is a list of usages, drawn marked. A click toggles a key and
sends the whole new list, sorted. ``locked`` usages are always marked and
can't be clicked (keys the device itself always includes).

Preview mode::

    {"widget": "keyboard", "layout": "keyboard", "mode": "preview",
     "state": "lighting.keys", "base": "lighting.color", "brightness": "brightness"}

The lighting as paint mode draws it, read-only, dimmed by the
``brightness`` state (0-100).

Pick mode::

    {"widget": "keyboard", "layout": "keyboard", "mode": "pick",
     "keys": ["g1", "g2"], "opens": "set_binding"}

Only the listed key ids stand out; clicking one opens the editor another
widget on the page registered for it (``ctx.editors[(opens, id)]``, e.g.
the ``buttons`` widget's binding editor). ``"keys_when": {"x_mode": [...]}``
adds keys while that state is truthy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPen
from PyQt6.QtWidgets import (
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from borochid.gui.widgets import PanelContext, theme_icon, widget

MAX_KEYS = 256
MIN_UNIT = 14  # px per key unit below which labels stop being readable
PREFERRED_UNIT = 40
GAP = 0.1  # between keys, in key units
_ERASE_ICONS = ["draw-eraser", "edit-clear", "edit-delete"]
_FILL_ICONS = ["color-fill", "fill-color", "format-fill-color"]
_CLEAR_ICONS = ["edit-clear-all", "edit-clear", "edit-undo"]


@dataclass(frozen=True)
class Key:
    id: str
    label: str
    cap: str
    x: float
    y: float
    w: float = 1.0
    h: float = 1.0
    led: int | None = None
    usage: int | None = None


def _number(v: Any, default: float | None = None) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return float(v)


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def parse_keys(layout: Any) -> list[Key]:
    """Keys from package data; malformed entries are skipped, since a bad key
    map must not stop the rest of the page from showing."""
    raw = layout.get("keys") if isinstance(layout, dict) else None
    keys = []
    for k in (raw if isinstance(raw, list) else [])[:MAX_KEYS]:
        if not isinstance(k, dict):
            continue
        x, y = _number(k.get("x")), _number(k.get("y"))
        w, h = _number(k.get("w"), 1.0), _number(k.get("h"), 1.0)
        if x is None or y is None or x < 0 or y < 0 or not (0 < w <= 16 and 0 < h <= 4):
            continue
        label = str(k.get("label", k.get("id", "")))
        cap = k.get("cap")
        keys.append(Key(str(k.get("id", label)), label, str(cap) if cap is not None else label, x, y, w, h,
                        _int(k.get("led")), _int(k.get("usage"))))
    return keys


def resolve_layout(item: dict[str, Any], ctx: PanelContext) -> list[Key]:
    ref = item.get("layout")
    return parse_keys(ctx.layouts.get(ref) if isinstance(ref, str) else ref)


def _readable_on(fill: QColor) -> QColor:
    """Black or white text, whichever reads better on ``fill``."""
    r, g, b = (c / 255 for c in (fill.red(), fill.green(), fill.blue()))
    return QColor("#000000") if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5 else QColor("#ffffff")


class KeyboardView(QWidget):
    """The drawing: scales to its width, keeps the key map's aspect ratio."""

    stroke = pyqtSignal(list)  # LED ids painted in one stroke (paint mode)
    toggled = pyqtSignal(int)  # a usage clicked (toggle mode)
    picked = pyqtSignal(str)  # a key id clicked (pick mode)

    def __init__(self, keys: list[Key], mode: str, locked: set[int] | None = None,
                 pickable: set[str] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.keys = keys
        self.mode = mode
        self.pickable = pickable or set()
        self.level = 1.0  # preview mode: the brightness, 0-1
        self.colors: dict[int, QColor] = {}
        self.base = QColor("#ffffff")
        self.marked: set[int] = set()
        self.locked = locked or set()
        self._stroke: list[int] = []
        self._stroke_color: QColor | None = None
        self.brush: QColor | None = QColor("#ffffff")  # None: the eraser
        self.cols = max((k.x + k.w for k in keys), default=1.0)
        self.rows = max((k.y + k.h for k in keys), default=1.0)
        self.setMouseTracking(True)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumSize(int(self.cols * MIN_UNIT), int(self.rows * MIN_UNIT))

    # -- geometry ---------------------------------------------------------------

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt API)
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 (Qt API)
        return math.ceil(width * self.rows / self.cols)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return QSize(int(self.cols * PREFERRED_UNIT), int(self.rows * PREFERRED_UNIT))

    def _unit(self) -> float:
        return min(self.width() / self.cols, self.height() / self.rows)

    def key_rect(self, k: Key) -> QRectF:
        u = self._unit()
        ox = (self.width() - self.cols * u) / 2
        return QRectF(ox + (k.x + GAP / 2) * u, (k.y + GAP / 2) * u, (k.w - GAP) * u, (k.h - GAP) * u)

    def key_at(self, pos: QPointF) -> Key | None:
        return next((k for k in self.keys if self.key_rect(k).contains(pos)), None)

    def _active(self, k: Key) -> bool:
        if self.mode in ("paint", "preview"):
            return k.led is not None
        if self.mode == "pick":
            return k.id in self.pickable
        return k.usage is not None and k.usage not in self.locked

    def _clickable(self, k: Key) -> bool:
        return self.mode != "preview" and self._active(k)

    # -- state ------------------------------------------------------------------

    def set_colors(self, value: Any) -> None:
        self.colors = {}
        if isinstance(value, dict):
            for led, c in value.items():
                color = QColor(c) if isinstance(c, str) else QColor()
                if str(led).isdigit() and color.isValid():
                    self.colors[int(led)] = color
        self.update()

    def set_base(self, value: Any) -> None:
        if isinstance(value, str) and QColor(value).isValid():
            self.base = QColor(value)
            self.update()

    def set_pickable(self, ids: set[str]) -> None:
        self.pickable = ids
        self.update()

    def set_level(self, value: Any) -> None:
        if (v := _number(value)) is not None:
            self.level = min(max(v, 0.0), 100.0) / 100
            self.update()

    def set_marked(self, value: Any) -> None:
        self.marked = {u for u in value if _int(u) is not None} if isinstance(value, list) else set()
        self.update()

    def color_of(self, k: Key) -> QColor:
        return self.colors.get(k.led, self.base) if k.led is not None else self.base

    # -- input ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return
        self._stroke = []
        if self.mode == "paint":
            self._paint_at(event.position())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self.mode == "paint" and event.buttons() & Qt.MouseButton.LeftButton and self.isEnabled():
            self._paint_at(event.position())
        k = self.key_at(event.position())
        self.setCursor(Qt.CursorShape.PointingHandCursor if k and self._clickable(k) else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return
        if self.mode == "paint":
            if self._stroke:
                self.stroke.emit(list(self._stroke))
            self._stroke = []
        elif (k := self.key_at(event.position())) and self._clickable(k):
            if self.mode == "pick":
                self.picked.emit(k.id)
            else:
                self.toggled.emit(k.usage)

    def _paint_at(self, pos: QPointF) -> None:
        k = self.key_at(pos)
        if k is None or not self._active(k) or k.led in self._stroke:
            return
        self._stroke.append(k.led)
        # Shown at once; the service's state update confirms it.
        if self.brush is None:
            self.colors.pop(k.led, None)
        else:
            self.colors[k.led] = QColor(self.brush)
        self.update(self.key_rect(k).toAlignedRect().adjusted(-2, -2, 2, 2))

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            k = self.key_at(QPointF(event.pos()))
            if k is not None:
                QToolTip.showText(event.globalPos(), k.label, self)
            else:
                QToolTip.hideText()
                if self.toolTip():
                    QToolTip.showText(event.globalPos(), self.toolTip(), self)
            return True
        return super().event(event)

    # -- drawing ----------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        pal = self.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        u = self._unit()
        font = QFont(self.font())
        font.setPixelSize(max(8, int(u * 0.28)))
        p.setFont(font)
        metrics = QFontMetrics(font)
        radius = max(2.0, u * 0.12)
        outline = pal.color(QPalette.ColorRole.Mid)
        highlight = pal.color(QPalette.ColorRole.Highlight)
        for k in self.keys:
            r = self.key_rect(k)
            active = self._active(k)
            fill, ink = self._colors_for(k, active, pal)
            p.setPen(QPen(outline, 1))
            p.setBrush(fill)
            p.drawRoundedRect(r, radius, radius)
            if self.mode == "toggle" and k.usage is not None and (k.usage in self.marked or k.usage in self.locked):
                # Marked keys: a strike through, so it reads as "off" in any theme.
                p.setPen(QPen(ink, max(1.0, u * 0.05)))
                inset = r.adjusted(u * 0.18, u * 0.18, -u * 0.18, -u * 0.18)
                p.drawLine(inset.topRight(), inset.bottomLeft())
                if k.usage in self.locked:
                    p.setPen(QPen(highlight, 1, Qt.PenStyle.DashLine))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(r.adjusted(1.5, 1.5, -1.5, -1.5), radius, radius)
            if k.cap:
                p.setPen(ink)
                text = metrics.elidedText(k.cap, Qt.TextElideMode.ElideRight, int(r.width() - 4))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        p.end()

    def _colors_for(self, k: Key, active: bool, pal: QPalette) -> tuple[QColor, QColor]:
        button = pal.color(QPalette.ColorRole.Button)
        text = pal.color(QPalette.ColorRole.ButtonText)
        if not active and not (self.mode == "toggle" and k.usage in self.locked):
            faded = QColor(text)
            faded.setAlphaF(0.4)
            return button, faded
        if self.mode == "paint":
            fill = self.color_of(k)
            return fill, _readable_on(fill)
        if self.mode == "preview":
            c = self.color_of(k)
            fill = QColor.fromRgbF(c.redF() * self.level, c.greenF() * self.level, c.blueF() * self.level)
            return fill, _readable_on(fill)
        if self.mode == "pick":
            return pal.color(QPalette.ColorRole.Highlight), pal.color(QPalette.ColorRole.HighlightedText)
        if k.usage in self.marked or k.usage in self.locked:
            fill = pal.color(QPalette.ColorRole.Highlight)
            return fill, pal.color(QPalette.ColorRole.HighlightedText)
        return button, text


def _tool(icons: list[str], text: str, tip: str) -> QToolButton:
    b = QToolButton()
    b.setAutoRaise(True)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    if icon := theme_icon(icons):
        b.setIcon(icon)
    b.setText(text)
    b.setToolTip(tip)
    return b


class PaintBar(QWidget):
    """Brush colour, eraser, fill all, clear."""

    def __init__(self, view: KeyboardView, parent: QWidget | None = None):
        super().__init__(parent)
        self.view = view
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)  # one row, never stretched
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(QLabel("Brush:"))
        self.swatch = QPushButton()
        self.swatch.setFixedSize(28, 22)
        self.swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch.setToolTip("Choose the colour to paint keys with")
        self.swatch.clicked.connect(self.pick)
        self.eraser = _tool(_ERASE_ICONS, "Eraser", "Paint keys back to the base colour")
        self.eraser.setCheckable(True)
        self.eraser.toggled.connect(self._erase)
        self.fill = _tool(_FILL_ICONS, "Fill all", "Paint every key with the brush colour")
        self.clear = _tool(_CLEAR_ICONS, "Clear", "Put every key back to the base colour")
        for w in (self.swatch, self.eraser, self.fill, self.clear):
            row.addWidget(w)
        row.addStretch(1)
        self.color = QColor("#ffffff")
        self._show()

    def pick(self) -> None:
        c = QColorDialog.getColor(self.color, self, "Brush colour")
        if c.isValid():
            self.set_color(c)

    def set_color(self, c: QColor) -> None:
        self.color = QColor(c)
        self.eraser.setChecked(False)
        self._show()

    def _erase(self, on: bool) -> None:
        self._show()

    def _show(self) -> None:
        erasing = self.eraser.isChecked()
        self.view.brush = None if erasing else QColor(self.color)
        border = "palette(highlight)" if not erasing else "palette(mid)"
        self.swatch.setStyleSheet(f"background-color: {self.color.name()}; border: 2px solid {border}; border-radius: 4px;")


@widget("keyboard")
def _keyboard(item: dict[str, Any], ctx: PanelContext) -> QWidget:
    keys = resolve_layout(item, ctx)
    if not keys:
        return QLabel("(no key map in this package)")
    mode = item.get("mode") if item.get("mode") in ("toggle", "preview", "pick") else "paint"
    locked = {u for u in item.get("locked", []) if _int(u) is not None} if isinstance(item.get("locked"), list) else set()
    pickable = {str(i) for i in item.get("keys", [])} if isinstance(item.get("keys"), list) else set()
    view = KeyboardView(keys, mode, locked, pickable)
    host = QWidget()
    col = QVBoxLayout(host)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(6)
    params = dict(item.get("params", {}))

    bar = PaintBar(view)
    col.addWidget(bar)
    if mode != "paint":
        # Not shown, but its room kept: the keys sit at the same height in
        # every mode, so switching sections doesn't move the keyboard.
        policy = bar.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        bar.setSizePolicy(policy)
        bar.hide()

    if mode == "paint":

        def send(leds: list[int], color: str | None) -> None:
            if leds and item.get("action"):
                ctx.invoke(item["action"], {**params, "keys": leds, "color": color})

        view.stroke.connect(lambda leds: send(leds, bar.color.name() if not bar.eraser.isChecked() else None))

        def fill_all() -> None:
            leds = sorted({k.led for k in keys if k.led is not None})
            for led in leds:
                view.colors[led] = QColor(bar.color)
            view.update()
            send(leds, bar.color.name())

        def clear() -> None:
            view.colors.clear()
            view.update()
            if item.get("clear"):
                ctx.invoke(item["clear"], dict(params))

        bar.fill.clicked.connect(fill_all)
        bar.clear.clicked.connect(clear)
        ctx.watch(item.get("state"), view.set_colors)
        ctx.watch(item.get("base"), view.set_base)
    elif mode == "preview":
        ctx.watch(item.get("state"), view.set_colors)
        ctx.watch(item.get("base"), view.set_base)
        ctx.watch(item.get("brightness"), view.set_level)
    elif mode == "pick":

        def pick(key_id: str) -> None:
            if opener := ctx.editors.get((item.get("opens"), key_id)):
                opener()

        view.picked.connect(pick)
        when = item.get("keys_when")
        for key, extra in (when.items() if isinstance(when, dict) else ()):
            if isinstance(extra, list):
                more = {str(i) for i in extra}
                ctx.watch(key, lambda v, more=more: view.set_pickable(pickable | more if v else pickable))
    else:

        def toggle(usage: int) -> None:
            chosen = set(view.marked) - locked
            chosen ^= {usage}
            view.marked = chosen
            view.update()
            ctx.send(item, sorted(chosen))

        view.toggled.connect(toggle)
        ctx.watch(item.get("state"), view.set_marked)
    col.addWidget(view)
    col.addStretch(1)  # spare height goes below the keys, not between them and the bar
    return host
