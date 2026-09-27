"""System tray icon: reopens the window after it is closed, and quits.

While the icon is shown, closing the window only hides it, so the app keeps
running in the tray. Without a tray (e.g. GNOME without an AppIndicator
extension) closing the window quits as before.
"""

from __future__ import annotations

from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget


class Tray(QSystemTrayIcon):
    def __init__(self, window: QWidget, icon: QIcon):
        super().__init__(icon, window)
        self.window = window
        self.setToolTip("Borochid")
        menu = QMenu(window)
        self.open_action = QAction(QIcon.fromTheme("window-new"), "Open Borochid", menu)
        self.open_action.triggered.connect(self.open_window)
        self.quit_action = QAction(QIcon.fromTheme("application-exit"), "Quit", menu)
        self.quit_action.triggered.connect(QApplication.quit)
        menu.addAction(self.open_action)
        menu.addSeparator()
        menu.addAction(self.quit_action)
        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def install(self) -> bool:
        """Show the icon and keep the app running when the window closes.
        False, changing nothing, when the desktop has no tray."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        self.show()
        QApplication.setQuitOnLastWindowClosed(False)
        return True

    def open_window(self) -> None:
        self.window.showNormal()  # also restores it when minimized
        self.window.raise_()
        self.window.activateWindow()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # A left click opens the window; the right-click menu is handled by Qt.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_window()
