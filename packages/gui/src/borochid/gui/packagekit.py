"""Installs driver packages through PackageKit.

The desktop's polkit agent handles authentication, so the app never sees a
password. Installs are restricted to packages from signed repositories
(ONLY_TRUSTED), and only names matching ``borochid-driver-*`` are accepted.
"""

from __future__ import annotations

from PyQt6.QtCore import QMetaType, QObject, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusArgument, QDBusConnection, QDBusInterface, QDBusMessage

from borochid.common.manifest import DRIVER_PACKAGE_RE

SERVICE = "org.freedesktop.PackageKit"
PATH = "/org/freedesktop/PackageKit"
IFACE = "org.freedesktop.PackageKit"
TX_IFACE = "org.freedesktop.PackageKit.Transaction"

# Bitfields of PkFilterEnum / PkTransactionFlagEnum (values checked against
# PackageKit 1.4). Filters are 1 << enum.
FILTER_NOT_INSTALLED = 1 << 3
FILTER_NEWEST = 1 << 16
FILTER_ARCH = 1 << 18
FLAG_ONLY_TRUSTED = 1 << 1
EXIT_SUCCESS = 1
INFO_INSTALLED = 1


def _u64(value: int) -> QDBusArgument:
    arg = QDBusArgument()
    arg.add(value, QMetaType.Type.ULongLong.value)
    return arg


def _strings(values: list[str]) -> QDBusArgument:
    arg = QDBusArgument()
    arg.add(values, QMetaType.Type.QStringList.value)
    return arg


class PackageInstaller(QObject):
    """One install at a time: ``install(name)`` then wait for ``finished``."""

    finished = pyqtSignal(bool, str)  # ok, message
    progress = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._bus = QDBusConnection.systemBus()
        self._tx_path: str | None = None
        self._stage = ""
        self._name = ""
        self._found: str | None = None
        self._installed = False
        self._error: str | None = None

    @staticmethod
    def available() -> bool:
        iface = QDBusConnection.systemBus().interface()
        return bool(iface and iface.isServiceRegistered(SERVICE).value())

    def install(self, name: str) -> None:
        if not DRIVER_PACKAGE_RE.match(name):
            self.finished.emit(False, f"refusing to install {name!r}: not a borochid driver package")
            return
        if not self.available():
            self.finished.emit(False, "PackageKit is not available; install the package with your package manager")
            return
        self._name, self._found, self._installed, self._error = name, None, False, None
        self._stage = "resolve"
        self.progress.emit(f"Looking up {name}…")
        self._call("Resolve", _u64(FILTER_NEWEST | FILTER_ARCH), _strings([name]))

    def _call(self, method: str, *args) -> None:
        reply = QDBusInterface(SERVICE, PATH, IFACE, self._bus).call("CreateTransaction")
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            self.finished.emit(False, reply.errorMessage())
            return
        path = reply.arguments()[0]
        self._tx_path = path if isinstance(path, str) else path.path()
        for signal in ("Package", "ErrorCode", "Finished"):
            self._bus.connect(SERVICE, self._tx_path, TX_IFACE, signal, getattr(self, f"_on_{signal.lower()}"))
        tx = QDBusInterface(SERVICE, self._tx_path, TX_IFACE, self._bus)
        # Lets polkit show the desktop's authentication dialog.
        tx.call("SetHints", _strings(["interactive=true"]))
        reply = tx.call(method, *args)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            self._disconnect()
            self.finished.emit(False, reply.errorMessage())

    def _disconnect(self) -> None:
        if self._tx_path:
            for signal in ("Package", "ErrorCode", "Finished"):
                self._bus.disconnect(SERVICE, self._tx_path, TX_IFACE, signal, getattr(self, f"_on_{signal.lower()}"))
        self._tx_path = None

    @pyqtSlot(QDBusMessage)
    def _on_package(self, msg: QDBusMessage) -> None:
        info, package_id, _summary = msg.arguments()
        # package_id is "name;version;arch;repo"; Resolve matches by name, but check.
        if package_id.split(";")[0] != self._name:
            return
        if info == INFO_INSTALLED:
            self._installed = True
        elif self._found is None:
            self._found = package_id

    @pyqtSlot(QDBusMessage)
    def _on_errorcode(self, msg: QDBusMessage) -> None:
        self._error = msg.arguments()[1]

    @pyqtSlot(QDBusMessage)
    def _on_finished(self, msg: QDBusMessage) -> None:
        exit_code = msg.arguments()[0]
        self._disconnect()
        if self._stage == "resolve":
            if self._installed:
                self.finished.emit(True, f"{self._name} is already installed")
            elif self._found is None:
                self.finished.emit(False, self._error or f"{self._name} was not found in any enabled repository")
            else:
                self._stage = "install"
                self.progress.emit(f"Installing {self._name} {self._found.split(';')[1]}…")
                self._call("InstallPackages", _u64(FLAG_ONLY_TRUSTED), _strings([self._found]))
        elif exit_code == EXIT_SUCCESS:
            self.finished.emit(True, f"Installed {self._name}")
        else:
            self.finished.emit(False, self._error or "installation was cancelled or failed")
