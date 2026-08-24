"""Hand-rolled ACP (Agent Client Protocol) stdio JSON-RPC client.

Implements only the slice of ACP needed to drive a disposable coding-agent
worker through RuntimePort: initialize, session/new, session/prompt,
session/cancel, session/update, and the client-side fs/permission callbacks
the agent may invoke. Framing is newline-delimited JSON-RPC 2.0, matching
the published protocol (agentclientprotocol.com/protocol/v1/transports):
the client writes to the agent's stdin, the agent writes to its stdout, and
stderr is free for the agent's own logs.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import threading
from typing import Any, Callable, Mapping

from weftmark.application.ports.runtime import RuntimeAdapterError, RuntimeErrorCode

RequestHandler = Callable[[dict[str, Any]], dict[str, Any]]
NotificationHandler = Callable[[str, dict[str, Any]], None]


class _PendingCall:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: str | None = None


class AcpConnection:
    """One live JSON-RPC connection to an ACP agent subprocess."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        request_handlers: Mapping[str, RequestHandler],
        on_notification: NotificationHandler,
    ) -> None:
        self._process = process
        self._request_handlers = dict(request_handlers)
        self._on_notification = on_notification
        self._ids = itertools.count(1)
        self._pending: dict[int, _PendingCall] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def request(self, method: str, params: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
        call_id = next(self._ids)
        pending = _PendingCall()
        with self._pending_lock:
            self._pending[call_id] = pending
        self._write({"jsonrpc": "2.0", "id": call_id, "method": method, "params": params})
        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(call_id, None)
            raise RuntimeAdapterError(
                RuntimeErrorCode.TRANSPORT_FAILED, "acp", method, "timed out waiting for a response"
            )
        if pending.error is not None:
            raise RuntimeAdapterError(RuntimeErrorCode.RUNTIME_FAILED, "acp", method, pending.error)
        assert pending.result is not None
        return pending.result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        self._closed = True
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except OSError:
            pass

    def _write(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":")) + "\n"
        with self._write_lock:
            assert self._process.stdin is not None
            self._process.stdin.write(line.encode("utf-8"))
            self._process.stdin.flush()

    def _read_loop(self) -> None:
        assert self._process.stdout is not None
        for raw_line in self._process.stdout:
            if self._closed:
                return
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._dispatch(message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            self._handle_inbound_request(message)
        elif "method" in message:
            self._on_notification(message["method"], message.get("params", {}))
        elif "id" in message:
            self._handle_response(message)

    def _handle_inbound_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        handler = self._request_handlers.get(method)
        if handler is None:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )
            return
        try:
            result = handler(message.get("params", {}))
        except Exception as error:  # noqa: BLE001 - relay any handler failure to the agent
            self._write(
                {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": str(error)}}
            )
            return
        self._write({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _handle_response(self, message: dict[str, Any]) -> None:
        with self._pending_lock:
            pending = self._pending.pop(message["id"], None)
        if pending is None:
            return
        if "error" in message:
            pending.error = message["error"].get("message", "unknown ACP error")
        else:
            pending.result = message.get("result", {})
        pending.event.set()
