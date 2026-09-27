import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtGui import QIcon  # noqa: E402

from borochid.gui.tray import Tray  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_menu_has_open_and_quit(app):
    tray = Tray(QtWidgets.QMainWindow(), QIcon())
    assert [a.text() for a in tray.contextMenu().actions() if not a.isSeparator()] == ["Open Borochid", "Quit"]


def test_open_and_click_show_the_closed_window(app):
    win = QtWidgets.QMainWindow()
    tray = Tray(win, QIcon())
    tray.open_action.trigger()
    assert win.isVisible()
    win.close()
    tray.activated.emit(QtWidgets.QSystemTrayIcon.ActivationReason.Trigger)
    assert win.isVisible()


def test_without_a_tray_closing_the_window_still_quits(app, monkeypatch):
    monkeypatch.setattr(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    assert Tray(QtWidgets.QMainWindow(), QIcon()).install() is False
    assert app.quitOnLastWindowClosed()
