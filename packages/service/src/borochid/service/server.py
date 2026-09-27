"""JSON-RPC server on a Unix socket. All connected clients receive every
device event as a notification."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from pathlib import Path
from typing import Any

from borochid.service import __version__
from borochid.common import rpc
from borochid.service.devices import DeviceManager

log = logging.getLogger(__name__)

# Drop clients that stop reading rather than buffering unbounded events.
MAX_CLIENT_BUFFER = 4 * 1024 * 1024


class RpcServer:
    def __init__(self, path: Path):
        self.path = path
        self.manager: DeviceManager | None = None
        self.detectors: dict[str, Any] = {}
        self._clients: set[asyncio.StreamWriter] = set()
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if _socket_alive(self.path):
                raise RuntimeError(f"another service is listening on {self.path}")
            self.path.unlink()
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(self.path), limit=1 << 20)
        finally:
            os.umask(old_umask)
        log.info("listening on %s", self.path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for w in list(self._clients):
            w.close()
        if self._server:
            await self._server.wait_closed()
        self.path.unlink(missing_ok=True)

    def broadcast(self, method: str, params: dict[str, Any]) -> None:
        data = rpc.notification(method, params)
        for w in list(self._clients):
            if w.transport.get_write_buffer_size() > MAX_CLIENT_BUFFER:
                log.warning("dropping slow client")
                w.close()
                self._clients.discard(w)
                continue
            w.write(data)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            while line := await reader.readline():
                writer.write(await self._dispatch(line))
                await writer.drain()
        except (ConnectionError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _dispatch(self, line: bytes) -> bytes:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return rpc.error(None, rpc.RpcError(rpc.PARSE_ERROR, "parse error"))
        id_ = msg.get("id")
        method = msg.get("method")
        handler = getattr(self, "rpc_" + str(method).replace(".", "_"), None)
        if handler is None:
            return rpc.error(id_, rpc.RpcError(rpc.METHOD_NOT_FOUND, f"unknown method {method}"))
        try:
            return rpc.result(id_, await handler(**(msg.get("params") or {})))
        except rpc.RpcError as e:
            return rpc.error(id_, e)
        except TypeError as e:
            return rpc.error(id_, rpc.RpcError(rpc.INVALID_PARAMS, str(e)))
        except Exception as e:
            log.exception("rpc %s failed", method)
            return rpc.error(id_, rpc.RpcError(rpc.INTERNAL_ERROR, str(e)))

    # -- methods -------------------------------------------------------------

    async def rpc_service_info(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "protocol": rpc.PROTOCOL_VERSION,
            # Where device pictures named in summaries live (see common.images).
            "image_store": str(self.manager.image_store),
        }

    async def rpc_devices_list(self, include_unsupported: bool = False) -> list[dict[str, Any]]:
        devs = self.manager.devices.values()
        return [d.summary() for d in devs if include_unsupported or d.status != "unsupported"]

    async def rpc_device_get(self, uid: str) -> dict[str, Any]:
        try:
            return self.manager.devices[uid].detail()
        except KeyError:
            raise rpc.RpcError(rpc.INVALID_PARAMS, f"no device {uid}") from None

    async def rpc_device_invoke(self, uid: str, action: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return await self.manager.invoke(uid, action, params or {})
        except KeyError:
            raise rpc.RpcError(rpc.INVALID_PARAMS, f"no device {uid}") from None
        except Exception as e:
            raise rpc.RpcError(rpc.DEVICE_ERROR, str(e)) from e

    async def rpc_device_retry(self, uid: str | None = None) -> dict[str, int]:
        """Called by clients after installing a driver package."""
        return {"retried": self.manager.retry(uid)}

    async def rpc_bluetooth_scan(self, seconds: float = 30) -> dict[str, float]:
        """Bounded, user-requested discovery; the service never scans on its own."""
        det = self.detectors.get("ble")
        if det is None:
            raise rpc.RpcError(rpc.DEVICE_ERROR, "Bluetooth detection is not enabled")
        return {"seconds": det.scan(seconds)}

    async def rpc_registry_refresh(self) -> dict[str, int]:
        self.manager.registry.rescan_local()
        self.manager.registry.invalidate_index()
        return {"retried": self.manager.retry()}


def _socket_alive(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX) as s:
        try:
            s.connect(str(path))
            return True
        except OSError:
            return False
