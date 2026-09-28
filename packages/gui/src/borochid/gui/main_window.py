from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QEvent, QModelIndex, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QCursor,
    QFont,
    QFontMetrics,
    QHelpEvent,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from borochid.common.rpc import PROTOCOL_VERSION
from borochid.gui.client import ServiceClient
from borochid.gui.icons import (
    FADED_OPACITY,
    Pictures,
    battery_icon,
    battery_text,
    connection_icon,
    connection_text,
    device_icon,
    usable,
)
from borochid.gui.packagekit import PackageInstaller
from borochid.gui.profiles import ProfileSelector
from borochid.gui.widgets import IconText, PanelContext, build_column, build_compact, build_form, theme_icon

STATUS_TEXT = {
    "detected": "Detected",
    "resolving": "Looking up…",
    "unsupported": "Unsupported",
    "needs_driver": "Driver needed",
    "blocked": "Blocked",
    "connecting": "Connecting…",
    "disconnected": "Reconnecting…",
    "ready": "Ready",
    "error": "Error",
}

# What the settings area says while there is nothing to configure yet.
_WAITING = {
    "detected": "Looking for this device's settings…",
    "resolving": "Looking for this device's settings…",
    "connecting": "Connecting to the device…",
    "disconnected": "The device went away. Waiting for it to come back…",
    "unsupported": "Borochid doesn't know how to configure this device yet.",
}


def status_label(d: dict[str, Any]) -> str:
    return d.get("status_text") or STATUS_TEXT.get(d["status"], d["status"])


def _is_status(item: dict[str, Any]) -> bool:
    default = "status" if item.get("widget") == "readout" else "settings"
    return item.get("section", default) == "status"


def settings_box(content: QWidget) -> QFrame:
    """A rounded panel around settings, in the same style as the device cards."""
    box = QFrame()
    box.setObjectName("settingsBox")
    box.setStyleSheet(
        "#settingsBox { background: palette(base); border: 1px solid palette(mid); border-radius: 10px; }"
    )
    col = QVBoxLayout(box)
    col.setContentsMargins(16, 16, 16, 16)
    col.addWidget(content)
    return box


class SectionSwitcher(QWidget):
    """A row of icon buttons over a stack of pages: the device page's
    sections. The selected button stays pressed; hover shows its name."""

    currentChanged = pyqtSignal(int)
    ICON_PX = 28

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        self._bar = QHBoxLayout()
        self._bar.setSpacing(4)
        self._bar.addStretch(1)
        col.addLayout(self._bar)
        self._stack = QStackedWidget()
        col.addWidget(settings_box(self._stack))
        col.addStretch(1)
        self._buttons = QButtonGroup(self)
        self._buttons.setExclusive(True)
        self._buttons.idClicked.connect(self.setCurrentIndex)

    def add(self, page: QWidget, icon: QIcon | None, label: str) -> None:
        index = self._stack.count()
        holder = QWidget()
        holder_col = QVBoxLayout(holder)
        holder_col.setContentsMargins(0, 0, 0, 0)
        holder_col.addWidget(page)
        holder_col.addStretch(1)
        self._stack.addWidget(holder)
        b = QToolButton()
        b.setCheckable(True)
        b.setAutoRaise(True)
        b.setToolTip(label)
        b.setAccessibleName(label)
        if icon:
            b.setIcon(icon)
            b.setIconSize(QSize(self.ICON_PX, self.ICON_PX))
        else:
            b.setText(label)  # no icon in this theme: the name will do
        self._buttons.addButton(b, index)
        self._bar.insertWidget(self._bar.count() - 1, b)
        if index == 0:
            b.setChecked(True)

    def count(self) -> int:
        return self._stack.count()

    def label(self, index: int) -> str:
        return self._buttons.button(index).toolTip()

    def widget(self, index: int) -> QWidget:
        return self._stack.widget(index)

    def currentIndex(self) -> int:
        return self._stack.currentIndex()

    def setCurrentIndex(self, index: int) -> None:
        index = index if 0 <= index < self.count() else 0
        self._buttons.button(index).setChecked(True)
        # Hidden pages don't count toward the size, so the box fits the section shown.
        for i in range(self.count()):
            policy = QSizePolicy.Policy.Preferred if i == index else QSizePolicy.Policy.Ignored
            self._stack.widget(i).setSizePolicy(policy, policy)
        if index != self._stack.currentIndex():
            self._stack.setCurrentIndex(index)
            self.currentChanged.emit(index)
        self._stack.updateGeometry()


class DevicePanel(QWidget):
    """A device's page: what can be set on the left, the device and its
    status on the right.

    A top-level item of the package UI goes in the status column when it sets
    ``"section": "status"`` (e.g. a "Status" group with the battery). Top-level
    ``readout`` items default to status, everything else to settings. Status
    sits under the picture, without group frames.
    """

    PICTURE_SIZE = 384
    STATUS_WIDTH = PICTURE_SIZE + 2 * 16 + 16  # picture, its margin, breathing room
    # A wide device (a keyboard) shows its picture smaller: its settings (the
    # lighting keyboard) need the width more than the picture does.
    WIDE_PICTURE = QSize(288, 144)
    WIDE_STATUS_WIDTH = WIDE_PICTURE.width() + 2 * 16 + 16

    def __init__(self, detail: dict[str, Any], client: ServiceClient, picture: QPixmap | None = None,
                 tab: int = 0, parent: QWidget | None = None):
        super().__init__(parent)
        self.uid = detail["uid"]
        self.status = detail["status"]  # a change of status rebuilds the page
        self.tabs: SectionSwitcher | None = None
        self.settings_host: QWidget | None = None
        self._staged: list[bool] = []  # per section: does its keyboard take the picture's place?
        self._picture = picture
        self.client = client
        self.wide = detail.get("category") in WIDE_CATEGORIES
        self.ctx = PanelContext(self._invoke, detail.get("layouts") or {})
        ready = detail["status"] == "ready"
        ui = detail.get("ui", []) if ready else []
        readouts = [i for i in ui if _is_status(i)]
        header_items = [i for i in ui if i.get("section") == "header"]
        settings = [i for i in ui if not _is_status(i) and i.get("section") != "header"]

        page = QVBoxLayout(self)
        page.setContentsMargins(24, 8, 16, 16)
        page.setSpacing(16)
        # The device's name heads the page, on its own above both columns.
        header = QVBoxLayout()
        header.setSpacing(2)
        title = QLabel(f"<h1 style='margin:0'>{html.escape(detail['display_name'])}</h1>")
        title.setWordWrap(True)
        header.addWidget(title)
        header.addLayout(self._status_line(header_items))
        page.addLayout(header)
        row = self._row = QHBoxLayout()
        row.setSpacing(24)
        page.addLayout(row, 1)

        status_col = QVBoxLayout()
        status_col.setContentsMargins(0, 0, 0, 0)
        status_col.setSpacing(8)
        if self.wide:  # a short picture or keyboard: centred beside the settings, not stuck to the top
            status_col.addStretch(1)
        status_col.addLayout(self._identity(detail, picture))
        if readouts:
            status_col.addSpacing(8)
            status_col.addWidget(build_compact(readouts, self.ctx))
        status_col.addStretch(1)
        # Technical details are for bug reports, not everyday use: hover to
        # read them, click to copy them.
        details = self._meta_text(detail)
        about = QToolButton()
        about.setIcon(QIcon.fromTheme("help-about", QIcon.fromTheme("dialog-information")))
        about.setAutoRaise(True)
        about.setToolTip(f"{details}\n\nClick to copy")
        about.clicked.connect(lambda: self._copy_details(details))
        status_col.addWidget(about, 0, Qt.AlignmentFlag.AlignRight)
        status_host = QWidget()
        status_host.setLayout(status_col)
        status_host.setMinimumWidth(self.WIDE_STATUS_WIDTH if self.wide else self.STATUS_WIDTH)

        settings_col = QVBoxLayout()
        settings_col.setSpacing(12)
        if ready:
            if settings:
                self.settings_host = self._settings(settings, tab)
                settings_col.addWidget(self.settings_host)
            else:
                settings_col.addWidget(self._note("This device has no settings."))
        elif detail["status"] == "needs_driver" and detail.get("needs"):
            self._driver_prompt(settings_col, detail["needs"])
        elif detail.get("error"):
            err = self._note(detail["error"])
            err.setStyleSheet("color: palette(link-visited);")
            settings_col.addWidget(err)
        else:
            settings_col.addWidget(self._note(_WAITING.get(detail["status"], "Settings will appear once the device is ready.")))
        settings_col.addStretch(1)

        # Settings first (where the eye starts), the device itself on the right.
        # Settings take under half the width and the picture's column the rest,
        # except on a wide device, whose settings get most of it (the column
        # widths change with the section there: see _show_stage).
        row.addLayout(settings_col, 70 if self.wide else 45)
        row.addWidget(status_host, 30 if self.wide else 55)
        if self.tabs is not None:
            self._show_stage(self.tabs.currentIndex())

        if ready:
            self.ctx.apply(detail.get("state", {}))
        self.update_summary(detail)

    def _identity(self, d: dict[str, Any], picture: QPixmap | None) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(2)
        self.picture_label = QLabel()  # drawn by update_summary: it fades while unavailable
        self.picture_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture_label.setContentsMargins(16, 16, 16, 16)  # room around the picture
        col.addWidget(self.picture_label)
        # A wide device's keyboard editor takes the picture's place while its
        # section is shown (see _settings).
        # Pages shown one at a time in a plain layout rather than a
        # QStackedWidget, which sizes to the keyboards' preferred width and
        # leaves a gap under them (it ignores height-for-width).
        self.stage = QWidget()
        self._stage_col = QVBoxLayout(self.stage)
        self._stage_col.setContentsMargins(0, 0, 0, 0)
        self.stage.hide()
        col.addWidget(self.stage)
        return col

    def _status_line(self, items: list[dict[str, Any]]) -> QHBoxLayout:
        """One line under the name: "Connected  [battery] 80%  [connection]".
        Items with ``"section": "header"`` (e.g. a subprofile selector) take
        the status text's place while the device can be used."""
        line = QHBoxLayout()
        line.setSpacing(10)
        self.status_text = QLabel()
        line.addWidget(self.status_text)
        self.header_items = build_column([{"widget": "row", "children": items}], self.ctx) if items else None
        if self.header_items is not None:
            line.addWidget(self.header_items)
        self.battery = IconText()
        self.battery.setToolTip("Battery")
        line.addWidget(self.battery)
        self.connection = IconText()  # wireless, cable or Bluetooth
        line.addWidget(self.connection)
        line.addStretch(1)
        return line

    def update_summary(self, d: dict[str, Any]) -> None:
        """Status line, battery, picture and whether settings can be used,
        updated in place (no page rebuild)."""
        self.status_text.setText(status_label(d))
        in_header = self.header_items is not None and usable(d)
        self.status_text.setVisible(not in_header)
        if self.header_items is not None:
            self.header_items.setVisible(in_header)
        size = self.WIDE_PICTURE if self.wide else QSize(self.PICTURE_SIZE, self.PICTURE_SIZE)
        self.picture_label.setPixmap(device_icon(d, size, self._picture).pixmap(size))
        for host in (self.settings_host, self.stage if self._staged else None):
            if host is None:
                continue
            host.setEnabled(usable(d))
            # Faded like the picture: disabled colour swatches would otherwise look live.
            fade = None
            if not usable(d):
                fade = QGraphicsOpacityEffect(host)
                fade.setOpacity(FADED_OPACITY + 0.15)
            host.setGraphicsEffect(fade)
        battery = d.get("battery") if usable(d) else None  # no reading from a headset that's off
        self.battery.setVisible(bool(battery))
        self.battery.set_icon(battery_icon(battery))
        self.battery.text.setText(battery_text(battery))
        link = connection_icon(d.get("connection"))
        self.connection.setVisible(link is not None)
        self.connection.set_icon(link)
        self.connection.setToolTip(connection_text(d.get("connection")))

    def _settings(self, items: list[dict[str, Any]], tab: int) -> QWidget:
        """Settings split into sections, one per top-level group, picked with
        a row of icon buttons (the group's ``icon``; its ``label`` shows on
        hover). Loose items sit above. A single group, or none, is shown as
        it is."""
        groups = [i for i in items if i.get("widget") == "group"]
        if len(groups) < 2:
            return settings_box(build_form(items, self.ctx))
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 0, 0)
        if loose := [i for i in items if i.get("widget") != "group"]:
            col.addWidget(build_form(loose, self.ctx))
        self.tabs = SectionSwitcher()
        for group in groups:
            children = group.get("children", [])
            # On a wide device a section's keyboard goes on the right, where
            # it has room; the rest of the section stays here.
            staged = [c for c in children if c.get("widget") == "keyboard"] if self.wide else []
            rest = [c for c in children if c not in staged]
            if staged and not rest:  # nothing else to set: say what the keyboard is for
                section = self._note(" ".join(str(c["tooltip"]) for c in staged if c.get("tooltip")))
            else:
                # The group's children, without its frame: the section is the frame.
                section = build_form([{**group, "widget": "form", "children": rest}], self.ctx)
            self.tabs.add(section, theme_icon(group.get("icon", "")), group.get("label", ""))
            page = build_column(staged, self.ctx) if staged else QWidget()
            page.hide()
            self._stage_col.addWidget(page)
            self._staged.append(bool(staged))
        self.tabs.currentChanged.connect(self._show_stage)
        self.tabs.setCurrentIndex(tab)
        col.addWidget(self.tabs)
        return host

    def _show_stage(self, index: int) -> None:
        """The section's keyboard in place of the picture, if it has one,
        with most of the width; otherwise the picture and the usual widths."""
        staged = 0 <= index < len(self._staged) and self._staged[index]
        for i in range(self._stage_col.count()):
            self._stage_col.itemAt(i).widget().setVisible(staged and i == index)
        self.stage.setVisible(staged)
        self.picture_label.setVisible(not staged)
        if self.wide:
            self._row.setStretch(0, 35 if staged else 70)
            self._row.setStretch(1, 65 if staged else 30)

    def _copy_details(self, details: str) -> None:
        QApplication.clipboard().setText(details)
        self.window().statusBar().showMessage("Device details copied", 3000)

    @staticmethod
    def _note(text: str) -> QLabel:
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)  # may quote what the device reported
        label.setWordWrap(True)
        return label

    def _driver_prompt(self, layout: QVBoxLayout, needs: dict[str, Any]) -> None:
        pkg = needs.get("provided_by")
        why = f"installed version {needs['installed']} is not compatible" if needs.get("installed") else "it is not installed"
        text = QLabel(
            f"This device needs the <b>{needs['driver']}</b> driver, but {why}."
            + (f"<br>It is provided by the system package <code>{pkg}</code>." if pkg else "")
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        if not pkg:
            return
        self.install_status = QLabel()
        self.install_btn = QPushButton(f"Install {pkg}")
        if not PackageInstaller.available():
            self.install_btn.setEnabled(False)
            self.install_status.setText("PackageKit is not available: install it with your package manager, then press Refresh on the device list.")
        self.installer = PackageInstaller(self)
        self.installer.progress.connect(self.install_status.setText)
        self.installer.finished.connect(self._on_installed)
        self.install_btn.clicked.connect(lambda: (self.install_btn.setEnabled(False), self.installer.install(pkg)))
        layout.addWidget(self.install_btn)
        layout.addWidget(self.install_status)

    def _on_installed(self, ok: bool, message: str) -> None:
        self.install_status.setText(message)
        if ok:
            # The service rescans installed plugins and brings the device up.
            self.client.call("device.retry", {"uid": self.uid})
        else:
            self.install_btn.setEnabled(True)

    @staticmethod
    def _meta_text(d: dict[str, Any]) -> str:
        parts = [d["uid"]]
        if d.get("vid") is not None:
            parts.append(f"{d['vid']:04x}:{d['pid']:04x}")
        if pkg := d.get("package"):
            parts.append(f"{pkg['id']} {pkg['version']} (driver {pkg['driver']}, from {pkg['source']})")
        return " · ".join(parts)

    def _invoke(self, action: str, params: dict[str, Any]) -> None:
        def done(_result, error):
            if error:
                self.window().statusBar().showMessage(f"{action} failed: {error['message']}", 5000)

        self.client.call("device.invoke", {"uid": self.uid, "action": action, "params": params}, done)


BATTERY_ROLE = Qt.ItemDataRole.UserRole + 1
CONNECTION_ROLE = Qt.ItemDataRole.UserRole + 3
WIDE_ROLE = Qt.ItemDataRole.UserRole + 2
# Devices whose pictures are wide get a card two columns wide on the home
# grid (a full-size keyboard is about 3.5 times wider than it is tall).
WIDE_CATEGORIES = frozenset({"keyboard"})


class TileDelegate(QStyledItemDelegate):
    """Draws each device as a card: its name at the top left, the battery
    icon under the name (hover it for the percentage), the picture in the
    middle, and an edit button in its own row at the bottom right. A wide
    card (``WIDE_ROLE``) spans two columns and its picture keeps its aspect
    ratio across the card's width."""

    PAD = 12
    PICTURE = 192
    BATTERY_PX = 24
    EDIT = QSize(28, 28)  # the edit button, square
    GAP = 4
    CARD = QSize(238, 304)  # pad, name, battery, picture, edit row, pad
    SPACING = 16  # between cards
    WIDE_CARD = QSize(2 * CARD.width() + SPACING, CARD.height())  # two columns and the gap between them
    RADIUS = 10
    EDIT_ICONS = ("document-edit-symbolic", "document-edit", "document-properties")

    edit_requested = pyqtSignal(QModelIndex)

    def edit_rect(self, option: QStyleOptionViewItem) -> QRect:
        r, e = option.rect, self.EDIT
        return QRect(r.right() - self.PAD - e.width() + 1, r.bottom() - self.PAD - e.height() + 1, e.width(), e.height())

    def _name_rect(self, option: QStyleOptionViewItem) -> QRect:
        r = option.rect
        return QRect(r.left() + self.PAD, r.top() + self.PAD, r.width() - 2 * self.PAD, self._name_font(option).pointSize() * 2)

    @staticmethod
    def _name_font(option: QStyleOptionViewItem) -> QFont:
        font = QFont(option.font)
        font.setBold(True)
        return font

    def battery_rect(self, option: QStyleOptionViewItem) -> QRect:
        name = self._name_rect(option)
        return QRect(name.left(), name.bottom() + 4, self.BATTERY_PX, self.BATTERY_PX)

    def connection_rect(self, option: QStyleOptionViewItem, index) -> QRect:
        """After the battery, or in its place when there is none."""
        r = self.battery_rect(option)
        shift = r.width() + 6 if battery_icon(index.data(BATTERY_ROLE)) else 0
        return QRect(r.left() + shift, r.top(), r.height(), r.height())

    def picture_rect(self, option: QStyleOptionViewItem) -> QRect:
        r, top = option.rect, self.battery_rect(option).bottom() + self.GAP
        bottom = self.edit_rect(option).top() - self.GAP  # the edit row is the picture's floor
        area = QRect(r.left() + self.PAD, top, r.width() - 2 * self.PAD, bottom - top)
        if r.width() > self.CARD.width():  # wide: the picture keeps its aspect ratio (QIcon.paint centres it)
            height = min(self.PICTURE, area.height())
            return QRect(area.left(), area.center().y() - height // 2 + 1, area.width(), height)
        side = min(self.PICTURE, area.width(), area.height())
        return QRect(area.center().x() - side // 2 + 1, area.center().y() - side // 2 + 1, side, side)

    @classmethod
    def picture_box(cls, wide: bool) -> QSize:
        """The largest picture a card shows."""
        return QSize(cls.WIDE_CARD.width() - 2 * cls.PAD, cls.PICTURE) if wide else QSize(cls.PICTURE, cls.PICTURE)

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        return self.WIDE_CARD if index.data(WIDE_ROLE) else self.CARD

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        pal, state = option.palette, option.state
        selected = bool(state & QStyle.StateFlag.State_Selected)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        border = pal.color(QPalette.ColorRole.Highlight) if selected or hovered else pal.color(QPalette.ColorRole.Mid)
        painter.setPen(QPen(border, 2 if selected else 1))
        # The app palette: the grid's own background is restyled to the window colour.
        painter.setBrush(QApplication.palette().color(QPalette.ColorRole.Base))
        painter.drawRoundedRect(QRectF(option.rect).adjusted(1, 1, -1, -1), self.RADIUS, self.RADIUS)

        font = self._name_font(option)
        name_rect = self._name_rect(option)
        painter.setFont(font)
        painter.setPen(pal.color(QPalette.ColorRole.Text))
        name = QFontMetrics(font).elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "", Qt.TextElideMode.ElideRight, name_rect.width())
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        if icon := battery_icon(index.data(BATTERY_ROLE)):
            icon.paint(painter, self.battery_rect(option))
        if icon := connection_icon(index.data(CONNECTION_ROLE)):
            icon.paint(painter, self.connection_rect(option, index))
        if isinstance(picture := index.data(Qt.ItemDataRole.DecorationRole), QIcon):
            picture.paint(painter, self.picture_rect(option))
        painter.restore()
        self._paint_edit_button(painter, option)

    def _paint_edit_button(self, painter: QPainter, option: QStyleOptionViewItem) -> None:
        """A real push button, drawn by the widget style so it matches the theme."""
        view = option.widget
        button = QStyleOptionButton()
        button.rect = self.edit_rect(option)
        button.palette = option.palette
        button.icon = theme_icon(list(self.EDIT_ICONS)) or QIcon()
        button.iconSize = QSize(16, 16)
        if button.icon.isNull():
            button.text = "Edit"  # no pencil in this theme
        button.state = QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Raised
        if view is not None and button.rect.contains(view.viewport().mapFromGlobal(QCursor.pos())):
            button.state |= QStyle.StateFlag.State_MouseOver
        style = view.style() if view is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_PushButton, button, painter, view)

    def editorEvent(self, event, model, option: QStyleOptionViewItem, index) -> bool:
        if event.type() == QEvent.Type.MouseMove and option.widget is not None:
            option.widget.viewport().update(self.edit_rect(option))  # button hover highlight
        # A click on the edit button opens the device, as a double-click does.
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and self.edit_rect(option).contains(event.position().toPoint())
        ):
            self.edit_requested.emit(index)
            return True
        return super().editorEvent(event, model, option, index)

    def helpEvent(self, event: QHelpEvent, view, option: QStyleOptionViewItem, index) -> bool:
        battery = index.data(BATTERY_ROLE)
        if battery and self.battery_rect(option).contains(event.pos()):
            QToolTip.showText(event.globalPos(), f"Battery {battery_text(battery)}", view)
            return True
        connection = index.data(CONNECTION_ROLE)
        if connection_icon(connection) and self.connection_rect(option, index).contains(event.pos()):
            QToolTip.showText(event.globalPos(), connection_text(connection), view)
            return True
        if self.edit_rect(option).contains(event.pos()):
            QToolTip.showText(event.globalPos(), "Settings", view)
            return True
        return super().helpEvent(event, view, option, index)


class DeviceGrid(QListWidget):
    """The home grid: the columns the cards use, centred in the window, with
    the cards filling rows from the left (a short last row stays left)."""

    PAD = 12  # around the cards

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # The scrollbar coming or going changes the width without a resize.
        self.verticalScrollBar().rangeChanged.connect(lambda *_: self._centre())
        model = self.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.dataChanged, model.layoutChanged):
            signal.connect(lambda *_: self._centre())

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._centre()

    def _used_columns(self, columns: int) -> int:
        """How many of ``columns`` the cards fill, flowed left to right as
        the view does (a wide card spans two)."""
        widest = row = 0
        for i in range(self.count()):
            span = min(columns, 2 if self.item(i).data(WIDE_ROLE) else 1)
            if row + span > columns:
                row = 0
            row += span
            widest = max(widest, row)
        return widest or columns

    def _centre(self) -> None:
        bar = self.verticalScrollBar()
        room = self.width() - 2 * self.frameWidth() - (bar.sizeHint().width() if bar.isVisible() else 0) - 2 * self.PAD
        # Icon mode puts the spacing before each card and once more at the
        # end, and wraps unless it also has room for the scrollbar while that
        # is hidden, plus a pixel (measured). That reserve is ours to give
        # but not to show: the cards are centred without it.
        s, cell = self.spacing(), TileDelegate.CARD.width() + self.spacing()
        reserve = 1 + (0 if bar.isVisible() else bar.sizeHint().width())
        columns = max(2, (room - s - reserve) // cell)  # a wide card spans two
        # Only the columns in use: narrowing the view to them flows the cards
        # into the same rows, since each row already fit in that many.
        columns = self._used_columns(columns)
        gap = max(0, room - (s + columns * cell))
        left = self.PAD + gap // 2
        right = max(0, self.PAD + gap - gap // 2 - reserve)
        self.setViewportMargins(left, self.PAD, right, self.PAD)


class MainWindow(QMainWindow):
    """Home is a grid of connected devices; clicking one opens its settings.

    Both are pages of one window, so there is always a single place to look
    and "back" returns to the grid.
    """

    ICON_SIZE = 192

    def __init__(self, client: ServiceClient):
        super().__init__()
        self.client = client
        self.setWindowTitle("Borochid")
        self.resize(1200, 760)  # five device cards across; room for the device page's picture
        self.devices: dict[str, dict[str, Any]] = {}  # uid -> latest summary
        self.pictures = Pictures()
        self._tab: dict[str, int] = {}  # uid -> the settings tab last shown

        self.grid = DeviceGrid()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setIconSize(QSize(self.ICON_SIZE, self.ICON_SIZE))
        # No fixed grid size: cards come in two widths (see TileDelegate), so
        # the view flows them by their size hints with even spacing.
        self.grid.setSpacing(TileDelegate.SPACING)
        self.grid.setUniformItemSizes(False)
        # Cards wrap, never scroll sideways; a wide card always fits.
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.setMinimumWidth(TileDelegate.WIDE_CARD.width() + 2 * TileDelegate.SPACING + 2 * 12 + 24)  # + padding, scrollbar
        delegate = TileDelegate(self.grid)
        self.grid.setItemDelegate(delegate)
        self.grid.setMouseTracking(True)  # hover highlight on the cards' edit buttons
        self.grid.setSortingEnabled(True)
        self.grid.setFrameShape(QFrame.Shape.NoFrame)
        # Same background as the window, so the grid and the actions row read as one page.
        # (No stylesheet padding: DeviceGrid sets its own margins to centre the cards.)
        self.grid.setStyleSheet("QListWidget { background: palette(window); }")
        # Double-click a card, click its edit button, or press Enter to open it. Not
        # itemActivated: with KDE's single-click setting, that fires on any click.
        self.grid.itemDoubleClicked.connect(lambda item: self._open(item.data(Qt.ItemDataRole.UserRole)))
        delegate.edit_requested.connect(lambda index: self._open(index.data(Qt.ItemDataRole.UserRole)))
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            QShortcut(QKeySequence(key), self.grid, activated=self._open_current,
                      context=Qt.ShortcutContext.WidgetShortcut)

        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.home = QStackedWidget()
        self.home.addWidget(self._empty_page())
        self.home.addWidget(self.grid)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        device_page = QWidget()
        col = QVBoxLayout(device_page)
        col.setContentsMargins(8, 0, 8, 0)
        col.addWidget(self.scroll, 1)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.home)
        self.pages.addWidget(device_page)
        central = QWidget()
        central_col = QVBoxLayout(central)
        central_col.setContentsMargins(0, 0, 0, 0)
        central_col.setSpacing(0)
        central_col.addLayout(self._top_bar())
        central_col.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        for keys in (QKeySequence.StandardKey.Back, QKeySequence(Qt.Key.Key_Escape)):
            QShortcut(keys, self, activated=self._go_home)

        self.panel: DevicePanel | None = None
        self._open_uid: str | None = None
        self.empty.setText("Connecting to the Borochid service…")
        client.connected.connect(self._on_connected)
        client.disconnected.connect(self._on_disconnected)
        client.notification.connect(self._on_notification)
        self.statusBar().showMessage("Connecting to service…")

    # -- service events --------------------------------------------------------

    def _on_connected(self) -> None:
        self.client.call("service.info", {}, self._on_service_info)

    def _on_service_info(self, info, error) -> None:
        if error:
            return
        if info.get("protocol") != PROTOCOL_VERSION:
            self.statusBar().showMessage(
                f"Service {info.get('version')} speaks protocol {info.get('protocol')}, "
                f"this app expects {PROTOCOL_VERSION}; update the older side"
            )
            return
        self.statusBar().showMessage(f"Connected to service {info['version']}", 3000)
        self.profile_selector.refresh()
        store = info.get("image_store")
        self.pictures.store = Path(store) if store and Path(store).is_absolute() else None
        self.empty.setText("No devices connected.\nPlug one in, or pair it in your Bluetooth settings.")
        self._reload()

    def _on_disconnected(self) -> None:
        self.statusBar().showMessage("Service not running — retrying…")
        self.empty.setText("The Borochid service is not running.\nWaiting for it to start…")
        self.devices.clear()
        self.grid.clear()
        self._go_home()
        self._update_home()
        self._update_profiles()

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method in ("device.added", "device.changed"):
            self._upsert(params)
            if self._open_uid == params["uid"] and method == "device.changed":
                if params["uid"] not in self.devices:
                    self._go_home()  # hidden now (became unsupported)
                elif self.panel and self.panel.status == params["status"]:
                    self.panel.update_summary(params)  # battery, status line
                else:
                    self._show(params["uid"])  # status changed: rebuild the page
        elif method == "device.removed":
            self._remove(params["uid"])
            if self._open_uid == params["uid"]:
                self._go_home()
        elif method == "profiles.changed":
            self.profile_selector.show_state(params)
        elif method == "device.state":
            if self.panel and self.panel.uid == params["uid"]:
                self.panel.ctx.apply(params["changes"])

    # -- home grid --------------------------------------------------------------

    def _reload(self) -> None:
        def fill(devices, error):
            if error:
                return
            self.devices.clear()
            self.grid.clear()
            for d in devices:
                self._upsert(d)
            if self._open_uid and self._open_uid not in self.devices:
                self._go_home()
            self._update_home()

        self.client.call("devices.list", {"include_unsupported": self.show_unsupported.isChecked()}, fill)

    def _item(self, uid: str) -> QListWidgetItem | None:
        for i in range(self.grid.count()):
            if self.grid.item(i).data(Qt.ItemDataRole.UserRole) == uid:
                return self.grid.item(i)
        return None

    def _upsert(self, d: dict[str, Any]) -> None:
        if d["status"] == "unsupported" and not self.show_unsupported.isChecked():
            self._remove(d["uid"])
            return
        if d.get("shadowed"):  # the same device on another connection is the one shown
            self._remove(d["uid"])
            return
        self.devices[d["uid"]] = d
        item = self._item(d["uid"])
        if item is None:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, d["uid"])
            self.grid.addItem(item)
        item.setText(d["display_name"])
        wide = d.get("category") in WIDE_CATEGORIES
        item.setData(WIDE_ROLE, wide)
        item.setIcon(device_icon(d, TileDelegate.picture_box(wide) if wide else self.ICON_SIZE, self.pictures.get(d)))
        item.setData(BATTERY_ROLE, d.get("battery") if usable(d) else None)
        item.setData(CONNECTION_ROLE, d.get("connection"))
        item.setToolTip(f"{d['display_name']}\n{status_label(d)}")
        self.grid.sortItems()
        self._update_home()
        self._update_profiles()

    def _remove(self, uid: str) -> None:
        self.devices.pop(uid, None)
        if item := self._item(uid):
            self.grid.takeItem(self.grid.row(item))
        self._update_home()
        self._update_profiles()

    def _update_home(self) -> None:
        self.home.setCurrentIndex(1 if self.grid.count() else 0)

    def _top_bar(self) -> QHBoxLayout:
        """Shared by both pages: "All devices" on the left (device page); on
        the right the home page's small icon buttons, then the profile
        switcher at the far right."""
        row = QHBoxLayout()
        row.setContentsMargins(8, 6, 12, 0)
        row.setSpacing(2)
        self.back = QToolButton()
        self.back.setIcon(QIcon.fromTheme("go-previous"))
        self.back.setText("All devices")
        self.back.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.back.setAutoRaise(True)
        self.back.clicked.connect(self._go_home)
        self.back.hide()
        row.addWidget(self.back)
        row.addStretch(1)

        def button(icon: str, tip: str) -> QToolButton:
            b = QToolButton()
            b.setIcon(QIcon.fromTheme(icon))
            b.setIconSize(QSize(16, 16))
            b.setToolTip(tip)
            b.setAutoRaise(True)
            row.addWidget(b)
            return b

        refresh = button("view-refresh", "Refresh: look up packages for your devices again")
        refresh.clicked.connect(self._refresh_registry)
        self.show_unsupported = button("view-hidden", "Show devices Borochid can't configure")
        self.show_unsupported.setCheckable(True)
        self.show_unsupported.toggled.connect(self._on_show_unsupported)
        self.home_actions = [refresh, self.show_unsupported]
        # Rightmost; only while a device that supports profiles is connected.
        row.addSpacing(8)
        self.profile_selector = ProfileSelector(self.client)
        self.profile_selector.failed.connect(lambda message: self.statusBar().showMessage(message, 5000))
        self.profile_selector.hide()
        row.addWidget(self.profile_selector)
        return row

    def _update_profiles(self) -> None:
        """Home: while any connected device supports profiles. A device's
        page: only if that device does, since switching can't affect it otherwise."""
        uid = getattr(self, "_open_uid", None)
        if uid is not None:
            shown = bool(self.devices.get(uid, {}).get("profiles"))
        else:
            shown = any(d.get("profiles") for d in self.devices.values())
        self.profile_selector.setVisible(shown)

    def _on_show_unsupported(self, on: bool) -> None:
        self.show_unsupported.setIcon(QIcon.fromTheme("view-visible" if on else "view-hidden"))
        self.show_unsupported.setToolTip(
            "Hide devices Borochid can't configure" if on else "Show devices Borochid can't configure"
        )
        self._reload()

    def _empty_page(self) -> QWidget:
        host = QWidget()
        col = QVBoxLayout(host)
        col.addStretch(1)
        logo = QLabel()
        logo.setPixmap(QApplication.windowIcon().pixmap(128, 128))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setEnabled(False)
        col.addWidget(logo)
        col.addWidget(self.empty)
        col.addStretch(1)
        return host

    # -- device page --------------------------------------------------------------

    def _open(self, uid: str) -> None:
        if uid == self._open_uid:
            return  # a double-click is also a click
        self._open_uid = uid
        self.scroll.setWidget(QWidget())  # don't flash the previous device while loading
        self.pages.setCurrentIndex(1)
        self.back.show()
        for b in self.home_actions:
            b.hide()
        self._update_profiles()
        self._show(uid)

    def _open_current(self) -> None:
        if item := self.grid.currentItem():
            self._open(item.data(Qt.ItemDataRole.UserRole))

    def _go_home(self) -> None:
        self._open_uid = None
        self.panel = None
        self.grid.clearSelection()
        self.pages.setCurrentIndex(0)
        self.back.hide()
        for b in self.home_actions:
            b.show()
        self._update_profiles()

    def _show(self, uid: str) -> None:
        def render(detail, error):
            if self._open_uid != uid:
                return  # the user moved on while this was in flight
            if error:
                self.statusBar().showMessage(f"Couldn't open device: {error['message']}", 5000)
                self._go_home()
                return
            self.panel = DevicePanel(detail, self.client, self.pictures.get(detail), self._tab.get(uid, 0))
            if self.panel.tabs is not None:
                self.panel.tabs.currentChanged.connect(lambda i: self._tab.__setitem__(uid, i))
            self.scroll.setWidget(self.panel)

        self.client.call("device.get", {"uid": uid}, render)

    # -- toolbar ------------------------------------------------------------------

    def _refresh_registry(self) -> None:
        self.client.call(
            "registry.refresh",
            {},
            lambda r, e: self.statusBar().showMessage(e["message"] if e else f"Retried {r['retried']} device(s)", 4000),
        )
