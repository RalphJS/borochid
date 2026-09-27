"""The profile switcher at the top right of the window.

Profiles belong to the service and are shared by every device that
supports them (``profiles.*`` RPC): switching here switches them all. The
window shows the switcher only while such a device is connected.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QInputDialog, QMenu, QMessageBox, QToolButton, QWidget

from borochid.gui.client import ServiceClient
from borochid.gui.widgets import theme_icon

_ICONS = ["dialog-layers", "view-list-details", "document-properties"]
_MENU_ICONS = ["overflow-menu", "application-menu", "open-menu-symbolic", "configure"]


class ProfileSelector(QWidget):
    failed = pyqtSignal(str)  # a message for the status bar

    def __init__(self, client: ServiceClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.client = client
        self.items: list[dict[str, Any]] = []
        self.active: str | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.combo = QComboBox()
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.combo.setMinimumWidth(150)
        self.combo.setToolTip("Profile: switches every device that supports profiles")
        if icon := theme_icon(_ICONS):
            self.icon = icon
        else:
            self.icon = None
        self.combo.activated.connect(self._selected)
        self.menu_button = QToolButton()
        self.menu_button.setAutoRaise(True)
        if icon := theme_icon(_MENU_ICONS):
            self.menu_button.setIcon(icon)
        else:
            self.menu_button.setText("⋯")
        self.menu_button.setToolTip("Manage profiles")
        menu = QMenu(self.menu_button)
        menu.addAction("New profile…", lambda: self._create(duplicate=False))
        menu.addAction("Duplicate this profile…", lambda: self._create(duplicate=True))
        menu.addAction("Rename…", self._rename)
        self.remove_action = menu.addAction("Delete…", self._remove)
        self.menu_button.setMenu(menu)
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        row.addWidget(self.combo)
        row.addWidget(self.menu_button)

    # -- state from the service ------------------------------------------------

    def refresh(self) -> None:
        self.client.call("profiles.list", {}, self._done)

    def show_state(self, snapshot: dict[str, Any]) -> None:
        self.items = [p for p in snapshot.get("profiles", []) if isinstance(p, dict) and "id" in p]
        self.active = snapshot.get("active")
        self.combo.blockSignals(True)
        self.combo.clear()
        for p in self.items:
            if self.icon is not None:
                self.combo.addItem(self.icon, str(p.get("name", p["id"])), p["id"])
            else:
                self.combo.addItem(str(p.get("name", p["id"])), p["id"])
        self.combo.setCurrentIndex(max(self.combo.findData(self.active), 0))
        self.combo.blockSignals(False)
        self.remove_action.setEnabled(len(self.items) > 1)

    def _done(self, result, error) -> None:
        if error:
            self.failed.emit(error["message"])
            self.show_state({"profiles": self.items, "active": self.active})  # undo the combo
        elif isinstance(result, dict):
            self.show_state(result)

    # -- user actions -------------------------------------------------------------

    def _current_name(self) -> str:
        return next((str(p.get("name")) for p in self.items if p["id"] == self.active), "")

    def _selected(self, index: int) -> None:
        pid = self.combo.itemData(index)
        if pid and pid != self.active:
            self.client.call("profiles.select", {"id": pid}, self._done)

    def _create(self, duplicate: bool) -> None:
        names = {str(p.get("name")) for p in self.items}
        suggestion = f"{self._current_name()} copy" if duplicate else next(f"Profile {n}" for n in range(1, 100) if f"Profile {n}" not in names)
        title = "Duplicate profile" if duplicate else "New profile"
        name, ok = QInputDialog.getText(self, title, "Name:", text=suggestion)
        if ok and name.strip():
            self.client.call("profiles.add", {"name": name.strip(), "duplicate": duplicate}, self._done)

    def _rename(self) -> None:
        name, ok = QInputDialog.getText(self, "Rename profile", "Name:", text=self._current_name())
        if ok and name.strip() and self.active:
            self.client.call("profiles.rename", {"id": self.active, "name": name.strip()}, self._done)

    def _remove(self) -> None:
        if not self.active:
            return
        answer = QMessageBox.question(
            self, "Delete profile", f"Delete the profile “{self._current_name()}”? Every device's settings for it are deleted too."
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.client.call("profiles.remove", {"id": self.active}, self._done)
