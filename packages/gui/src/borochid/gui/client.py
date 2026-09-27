"""Qt-native client for the service socket (no asyncio in the GUI process)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QLocalSocket

from borochid.common import paths, rpc

Callback = Callable[[Any, dict[str, Any] | None], None]  # (result, error)


class ServiceClient(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    notification = pyqtSignal(str, dict)

    RECONNECT_MS = 2000

    def __init__(self, parent: QObject | None = None, socket_path: str | None = None):
        super().__init__(parent)
        self._path = socket_path or str(paths.socket_path())
        self._sock = QLocalSocket(self)
        self._sock.connected.connect(self.connected)
        self._sock.disconnected.connect(self._on_disconnected)
        self._sock.errorOccurred.connect(lambda _e: self._schedule_reconnect())
        self._sock.readyRead.connect(self._on_ready_read)
        self._buf = b""
        self._next_id = 1
        self._pending: dict[int, Callback] = {}
        self._retry = QTimer(self, singleShot=True, interval=self.RECONNECT_MS)
        self._retry.timeout.connect(self.connect_to_service)

    @property
    def is_connected(self) -> bool:
        return self._sock.state() == QLocalSocket.LocalSocketState.ConnectedState

    def connect_to_service(self) -> None:
        if self._sock.state() == QLocalSocket.LocalSocketState.UnconnectedState:
            self._sock.connectToServer(self._path)

    def call(self, method: str, params: dict[str, Any] | None = None, callback: Callback | None = None) -> None:
        if not self.is_connected:
            if callback:
                callback(None, {"code": -1, "message": "not connected to service"})
            return
        id_ = self._next_id
        self._next_id += 1
        if callback:
            self._pending[id_] = callback
        self._sock.write(rpc.request(id_, method, params))

    def _on_ready_read(self) -> None:
        self._buf += bytes(self._sock.readAll())
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and msg["id"] is not None:
                if cb := self._pending.pop(msg["id"], None):
                    cb(msg.get("result"), msg.get("error"))
            elif "method" in msg:
                self.notification.emit(msg["method"], msg.get("params") or {})

    def _on_disconnected(self) -> None:
        for cb in self._pending.values():
            cb(None, {"code": -1, "message": "service disconnected"})
        self._pending.clear()
        self._buf = b""
        self.disconnected.emit()
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if not self._retry.isActive():
            self._retry.start()
