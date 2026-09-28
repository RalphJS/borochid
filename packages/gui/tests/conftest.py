import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def app():
    """One QApplication for the whole run. A per-module one is destroyed when
    its module ends, taking down widgets a test left open with it (and at
    times the interpreter)."""
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
