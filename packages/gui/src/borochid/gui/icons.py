"""Device icons: the package's picture of the device if it has one,
otherwise an icon for its ``category`` from the desktop icon theme.

Pictures are read from the service's image store (see
``borochid.common.images``), which re-checks each file before it is decoded.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import QApplication

from borochid.common import images

# freedesktop icon names, most specific first.
_CATEGORY_ICONS = {
    "headset": ("audio-headset", "audio-headphones"),
    "headphones": ("audio-headphones", "audio-headset"),
    "speaker": ("audio-speakers", "audio-card"),
    "microphone": ("audio-input-microphone",),
    "keyboard": ("input-keyboard",),
    "keypad": ("input-keyboard",),
    "mouse": ("input-mouse",),
    "gamepad": ("input-gaming",),
    "tablet": ("input-tablet",),
    "webcam": ("camera-web", "camera-video"),
}
_BUS_ICONS = {"ble": ("bluetooth", "preferences-system-bluetooth"), "usb": ("drive-removable-media-usb",)}

# Statuses where the user has to do something; the icon gets a warning badge.
ATTENTION = frozenset({"needs_driver", "blocked", "error"})
# Statuses where the device can't be configured yet; the icon is dimmed.
INACTIVE = frozenset({"detected", "resolving", "connecting", "disconnected", "unsupported"})


# How a device is connected ("connection" in its summary): theme icon
# names, first one the theme has, and the words for a tooltip.
CONNECTIONS = {
    "wireless": (("network-wireless-symbolic", "network-wireless"), "Wireless"),
    "cable": (("drive-removable-media-usb-symbolic", "drive-removable-media-usb", "network-wired-symbolic"), "USB cable"),
    "bluetooth": (("preferences-system-bluetooth", "bluetooth-active-symbolic", "bluetooth"), "Bluetooth"),
}


def connection_icon(connection: Any) -> QIcon | None:
    spec = CONNECTIONS.get(connection) if isinstance(connection, str) else None
    return _theme(spec[0]) if spec else None


def connection_text(connection: Any) -> str:
    spec = CONNECTIONS.get(connection) if isinstance(connection, str) else None
    return f"Connected by {spec[1].lower()}" if connection == "cable" else (spec[1] if spec else "")


def _theme(names: tuple[str, ...]) -> QIcon | None:
    for name in names:
        if QIcon.hasThemeIcon(name):
            return QIcon.fromTheme(name)
    return None


def base_icon(device: dict[str, Any]) -> QIcon:
    return (
        _theme(_CATEGORY_ICONS.get(device.get("category") or "", ()))
        or _theme(_BUS_ICONS.get(device.get("bus") or "", ()))
        or QIcon(str(files("borochid.gui") / "resources" / "borochid.png"))
    )


class Pictures:
    """Device pictures from the image store, decoded once per digest."""

    def __init__(self) -> None:
        self.store: Path | None = None
        self._cache: dict[str, QPixmap | None] = {}

    def get(self, device: dict[str, Any]) -> QPixmap | None:
        digest = device.get("image")
        if not digest or self.store is None:
            return None
        if digest not in self._cache:
            pixmap = QPixmap()
            data = images.load(self.store, digest)
            self._cache[digest] = pixmap if data and pixmap.loadFromData(data, "PNG") else None
        return self._cache[digest]


LOW_BATTERY = 15  # percent; the fill turns red at or below this
_LOW, _CHARGING = QColor("#da4453"), QColor("#27ae60")


def paint_battery(painter: QPainter, rect: QRectF, level: Any, charging: bool, ink: QColor) -> None:
    """A horizontal battery in ``rect``: an outline in ``ink``, filled in
    proportion to ``level`` (percent). Red when low, green with a bolt while
    charging, just the outline when the level is unknown."""
    s = min(rect.width(), rect.height() * 2)  # a 2:1 body, centred
    body = QRectF(0, 0, s * 0.84, s * 0.46)
    body.moveCenter(rect.center() - QPointF(s * 0.04, 0))
    pen = max(1.0, s / 16)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(ink, pen))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(body, s * 0.08, s * 0.08)
    nub = QRectF(body.right() + pen / 2, body.center().y() - body.height() * 0.22, s * 0.07, body.height() * 0.44)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(ink)
    painter.drawRoundedRect(nub, pen / 2, pen / 2)
    if isinstance(level, (int, float)) and not isinstance(level, bool):
        inner = body.adjusted(pen * 1.5, pen * 1.5, -pen * 1.5, -pen * 1.5)
        inner.setWidth(inner.width() * max(0.0, min(100.0, float(level))) / 100)
        painter.setBrush(_CHARGING if charging else _LOW if level <= LOW_BATTERY else ink)
        painter.drawRoundedRect(inner, pen / 2, pen / 2)
    if charging:
        c, h = body.center(), body.height()
        bolt = QPolygonF([c + QPointF(x * h, y * h) for x, y in
                          ((0.08, -0.42), (-0.2, 0.06), (0.0, 0.06), (-0.08, 0.42), (0.2, -0.06), (0.0, -0.06))])
        painter.setBrush(ink)
        painter.drawPolygon(bolt)
    painter.restore()


def battery_icon(battery: dict[str, Any] | None) -> QIcon | None:
    """Icon for a summary's ``battery``; None when the device has no battery.
    Drawn rather than taken from the theme, so it looks the same everywhere."""
    if not battery:
        return None
    ink = QApplication.palette().color(QPalette.ColorRole.WindowText)
    icon = QIcon()
    for px in (16, 22, 24, 32, 48, 64):
        pixmap = QPixmap(px, px)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        paint_battery(painter, QRectF(0, 0, px, px), battery.get("level"), bool(battery.get("charging")), ink)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def battery_text(battery: dict[str, Any] | None) -> str:
    if not battery or battery.get("level") is None:
        return "—" if battery else ""
    return f"{battery['level']}%" + (", charging" if battery.get("charging") else "")


FADED_OPACITY = 0.35


def usable(device: dict[str, Any]) -> bool:
    """Whether the device can be used right now: not still connecting, and
    not reported unavailable (e.g. its wireless headset is switched off)."""
    return device.get("status") not in INACTIVE and device.get("available", True) is not False


def _faded(pixmap: QPixmap) -> QPixmap:
    out = QPixmap(pixmap.size())
    out.setDevicePixelRatio(pixmap.devicePixelRatio())
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setOpacity(FADED_OPACITY)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return out


def device_icon(device: dict[str, Any], size: int | QSize, picture: QPixmap | None = None) -> QIcon:
    """The device's icon, faded while it can't be used, and badged when it
    needs attention. ``size`` is a square side, or a box for wide pictures
    (which keep their aspect ratio inside it)."""
    status = device.get("status")
    box = size if isinstance(size, QSize) else QSize(size, size)
    pixmap = (QIcon(picture) if picture else base_icon(device)).pixmap(box)
    if not usable(device):
        pixmap = _faded(pixmap)
    if status in ATTENTION and (badge := _theme(("dialog-warning", "emblem-important"))):
        pixmap = QPixmap(pixmap)
        painter = QPainter(pixmap)
        b = min(min(box.width(), box.height()) * 2 // 5, 48)  # a badge, not a second icon
        logical = pixmap.deviceIndependentSize()  # HiDPI pixmaps are larger than `size`
        painter.drawPixmap(QPointF(logical.width() - b, logical.height() - b), badge.pixmap(b, b))
        painter.end()
    icon = QIcon()
    icon.addPixmap(pixmap, QIcon.Mode.Normal)
    icon.addPixmap(pixmap, QIcon.Mode.Selected)
    return icon
