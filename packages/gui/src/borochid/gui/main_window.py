from __future__ import annotations

import html
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from borochid.common.rpc import PROTOCOL_VERSION
from borochid.gui.client import ServiceClient
from borochid.gui.packagekit import PackageInstaller
from borochid.gui.widgets import PanelContext, build_form

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


class DevicePanel(QWidget):
    def __init__(self, detail: dict[str, Any], client: ServiceClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.uid = detail["uid"]
        self.client = client
        layout = QVBoxLayout(self)

        title = QLabel(f"<h2>{html.escape(detail['display_name'])}</h2>")
        meta = QLabel(self._meta_text(detail))
        meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(title)
        layout.addWidget(meta)

        self.ctx = PanelContext(self._invoke)
        if detail["status"] == "ready":
            layout.addWidget(build_form(detail.get("ui", []), self.ctx))
            self.ctx.apply(detail.get("state", {}))
        elif detail["status"] == "needs_driver" and detail.get("needs"):
            self._driver_prompt(layout, detail["needs"])
        elif detail.get("error"):
            err = QLabel(detail["error"])
            err.setWordWrap(True)
            err.setStyleSheet("color: palette(link-visited);")
            layout.addWidget(err)
        layout.addStretch(1)

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
            self.install_status.setText("PackageKit is not available: install it with your package manager, then press Refresh.")
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
        parts = [d.get("status_text") or STATUS_TEXT.get(d["status"], d["status"])]
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


class MainWindow(QMainWindow):
    def __init__(self, client: ServiceClient):
        super().__init__()
        self.client = client
        self.setWindowTitle("Borochid")
        self.resize(900, 560)

        self.list = QListWidget()
        self.list.setMinimumWidth(240)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._show(cur.data(Qt.ItemDataRole.UserRole) if cur else None))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self._placeholder()

        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(self.scroll)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)

        tb = self.addToolBar("Main")
        tb.setMovable(False)
        refresh = QAction("Refresh registry", self)
        refresh.triggered.connect(self._refresh_registry)
        tb.addAction(refresh)
        scan = QAction("Scan Bluetooth", self)
        scan.setToolTip("Look for nearby Bluetooth devices for 30 seconds")
        scan.triggered.connect(self._scan_bluetooth)
        tb.addAction(scan)
        self.show_unsupported = QAction("Show unsupported", self, checkable=True)
        self.show_unsupported.toggled.connect(lambda _on: self._reload())
        tb.addAction(self.show_unsupported)

        self.panel: DevicePanel | None = None
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
        self._reload()

    def _on_disconnected(self) -> None:
        self.statusBar().showMessage("Service not running — retrying…")
        self.list.clear()
        self._placeholder()

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method in ("device.added", "device.changed"):
            self._upsert(params)
            if self.panel and self.panel.uid == params["uid"] and method == "device.changed":
                self._show(params["uid"])  # status changed: rebuild panel
        elif method == "device.removed":
            if item := self._item(params["uid"]):
                self.list.takeItem(self.list.row(item))
            if self.panel and self.panel.uid == params["uid"]:
                self._placeholder()
        elif method == "device.state":
            if self.panel and self.panel.uid == params["uid"]:
                self.panel.ctx.apply(params["changes"])

    # -- list management --------------------------------------------------------

    def _reload(self) -> None:
        def fill(devices, error):
            if error:
                return
            self.list.clear()
            for d in devices:
                self._upsert(d)

        self.client.call("devices.list", {"include_unsupported": self.show_unsupported.isChecked()}, fill)

    def _item(self, uid: str) -> QListWidgetItem | None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == uid:
                return self.list.item(i)
        return None

    def _upsert(self, d: dict[str, Any]) -> None:
        hidden = d["status"] == "unsupported" and not self.show_unsupported.isChecked()
        item = self._item(d["uid"])
        if hidden:
            if item:
                self.list.takeItem(self.list.row(item))
            return
        if item is None:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, d["uid"])
            self.list.addItem(item)
        name = d["display_name"]
        item.setText(f"{name}\n{d.get('status_text') or STATUS_TEXT.get(d['status'], d['status'])}")

    def _show(self, uid: str | None) -> None:
        if uid is None:
            self._placeholder()
            return

        def render(detail, error):
            if error or self._current_uid() != uid:
                return
            self.panel = DevicePanel(detail, self.client)
            self.scroll.setWidget(self.panel)

        self.client.call("device.get", {"uid": uid}, render)

    def _current_uid(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _placeholder(self) -> None:
        self.panel = None
        host = QWidget()
        col = QVBoxLayout(host)
        col.addStretch(1)
        logo = QLabel()
        logo.setPixmap(QApplication.windowIcon().pixmap(128, 128))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("Select a device")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setEnabled(False)
        col.addWidget(logo)
        col.addWidget(hint)
        col.addStretch(1)
        self.scroll.setWidget(host)

    def _scan_bluetooth(self) -> None:
        self.client.call(
            "bluetooth.scan",
            {"seconds": 30},
            lambda r, e: self.statusBar().showMessage(
                e["message"] if e else f"Scanning for Bluetooth devices for {r['seconds']:.0f} s…", 5000
            ),
        )

    def _refresh_registry(self) -> None:
        self.client.call(
            "registry.refresh",
            {},
            lambda r, e: self.statusBar().showMessage(e["message"] if e else f"Retried {r['retried']} device(s)", 4000),
        )
