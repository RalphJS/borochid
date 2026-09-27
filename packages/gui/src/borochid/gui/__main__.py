from __future__ import annotations

import sys
from importlib.resources import files

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from borochid.gui.client import ServiceClient
from borochid.gui.main_window import MainWindow
from borochid.gui.tray import Tray


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Borochid")
    # Matches borochid.desktop, so Wayland compositors show the installed icon.
    app.setDesktopFileName("borochid")
    # Theme icon when installed system-wide; the bundled copy when run from source.
    icon = QIcon.fromTheme("borochid", QIcon(str(files("borochid.gui") / "resources" / "borochid.png")))
    app.setWindowIcon(icon)
    client = ServiceClient()
    win = MainWindow(client)
    tray = Tray(win, icon)
    tray.install()
    win.show()
    client.connect_to_service()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
