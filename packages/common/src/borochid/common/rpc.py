"""Wire format between service and clients: newline-delimited JSON-RPC 2.0.

Requests carry an ``id``; notifications (service -> client events) do not.
Kept dependency-free so any client (Qt, CLI, tests) can speak it.
"""

from __future__ import annotations

import json
from typing import Any

# Bump when methods/notifications change incompatibly. The service and GUI ship
# as separate distributions, so they check this on connect instead of assuming
# they were installed together.
PROTOCOL_VERSION = 2  # 2: service-wide profiles (profiles.*)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
DEVICE_ERROR = -32000


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def encode(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg, separators=(",", ":")).encode() + b"\n"


def request(id_: int, method: str, params: dict[str, Any] | None = None) -> bytes:
    return encode({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def notification(method: str, params: dict[str, Any]) -> bytes:
    return encode({"jsonrpc": "2.0", "method": method, "params": params})


def result(id_: int, value: Any) -> bytes:
    return encode({"jsonrpc": "2.0", "id": id_, "result": value})


def error(id_: int | None, err: RpcError) -> bytes:
    body: dict[str, Any] = {"code": err.code, "message": err.message}
    if err.data is not None:
        body["data"] = err.data
    return encode({"jsonrpc": "2.0", "id": id_, "error": body})
