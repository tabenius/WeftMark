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

import hashlib
import itertools
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from weftmark.application.ports.git import GitChangeKind, GitObjectId
from weftmark.application.ports.runtime import (
    RuntimeAdapterError,
    RuntimeChangeWorkspace,
    RuntimeChanges,
    RuntimeChangesMode,
    RuntimeContractError,
    RuntimeErrorCode,
    RuntimeFileChange,
    RuntimeWorkerState,
    RuntimeWorkerSummary,
    RuntimeWorkspace,
)

RequestHandler = Callable[[dict[str, Any]], dict[str, Any]]
NotificationHandler = Callable[[str, dict[str, Any]], None]

_MAX_RPC_BYTES = 1_048_576
_MAX_TEXT_FILE_BYTES = 4_194_304
_MAX_TEXT_LINES = 100_000
_MAX_REQUEST_TIMEOUT_SECONDS = 3_600
_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
)


class _PendingCall:
    __slots__ = ("event", "result", "error", "transport_error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.transport_error = False


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
        if not isinstance(method, str) or not method or not isinstance(params, dict):
            raise RuntimeContractError("ACP request requires a method and object params")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout != timeout
            or timeout <= 0
            or timeout > _MAX_REQUEST_TIMEOUT_SECONDS
        ):
            raise RuntimeContractError("ACP request timeout must be between 0 and 3600 seconds")
        call_id = next(self._ids)
        pending = _PendingCall()
        with self._pending_lock:
            self._pending[call_id] = pending
        try:
            self._write(
                {"jsonrpc": "2.0", "id": call_id, "method": method, "params": params}
            )
        except RuntimeAdapterError:
            with self._pending_lock:
                self._pending.pop(call_id, None)
            raise
        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(call_id, None)
            raise RuntimeAdapterError(
                RuntimeErrorCode.TRANSPORT_FAILED, "acp", method, "timed out waiting for a response"
            )
        if pending.error is not None:
            code = (
                RuntimeErrorCode.TRANSPORT_FAILED
                if pending.transport_error
                else RuntimeErrorCode.RUNTIME_FAILED
            )
            raise RuntimeAdapterError(code, "acp", method, pending.error)
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
        self._fail_pending("connection closed")

    def _write(self, message: dict[str, Any]) -> None:
        try:
            encoded = (
                json.dumps(
                    message,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as error:
            raise RuntimeAdapterError(
                RuntimeErrorCode.TRANSPORT_FAILED,
                "acp",
                "write",
                "message is not strict JSON",
            ) from error
        if len(encoded) > _MAX_RPC_BYTES:
            raise RuntimeAdapterError(
                RuntimeErrorCode.TRANSPORT_FAILED,
                "acp",
                "write",
                "message exceeds 1 MiB",
            )
        with self._write_lock:
            if self._closed or self._process.stdin is None:
                raise RuntimeAdapterError(
                    RuntimeErrorCode.TRANSPORT_FAILED,
                    "acp",
                    "write",
                    "connection is closed",
                )
            try:
                self._process.stdin.write(encoded)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise RuntimeAdapterError(
                    RuntimeErrorCode.TRANSPORT_FAILED,
                    "acp",
                    "write",
                    "agent transport closed",
                ) from error

    def _read_loop(self) -> None:
        assert self._process.stdout is not None
        while True:
            raw_line = self._process.stdout.readline(_MAX_RPC_BYTES + 1)
            if not raw_line:
                break
            if self._closed:
                return
            if len(raw_line) > _MAX_RPC_BYTES or not raw_line.endswith(b"\n"):
                self._fail_pending("agent message is missing a newline or exceeds 1 MiB")
                return
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = _strict_json_loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            try:
                self._dispatch(message)
            except RuntimeAdapterError as error:
                self._fail_pending(error.detail)
                return
            except (KeyError, TypeError, ValueError):
                continue
        self._fail_pending("agent transport closed")

    def _dispatch(self, message: dict[str, Any]) -> None:
        if message.get("jsonrpc") != "2.0":
            self._malformed_response(message, "JSON-RPC version must be 2.0")
            return
        if "method" in message and "id" in message:
            self._handle_inbound_request(message)
        elif "method" in message:
            params = message.get("params", {})
            if isinstance(message["method"], str) and isinstance(params, dict):
                try:
                    self._on_notification(message["method"], params)
                except Exception:  # noqa: BLE001 - notification bugs cannot kill transport
                    return
        elif "id" in message:
            self._handle_response(message)

    def _handle_inbound_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if not isinstance(method, str):
            raise TypeError("JSON-RPC method must be text")
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
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise TypeError("JSON-RPC params must be an object")
            result = handler(params)
        except Exception as error:  # noqa: BLE001 - relay any handler failure to the agent
            self._write(
                {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": str(error)}}
            )
            return
        self._write({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _handle_response(self, message: dict[str, Any]) -> None:
        response_id = message["id"]
        if isinstance(response_id, str) and response_id.isascii() and response_id.isdigit():
            response_id = int(response_id)
        with self._pending_lock:
            pending = self._pending.pop(response_id, None)
        if pending is None:
            return
        if ("error" in message) == ("result" in message):
            pending.error = "ACP response must contain exactly one of result or error"
            pending.transport_error = True
        elif "error" in message:
            error = message["error"]
            pending.error = (
                str(error.get("message", "unknown ACP error"))[:4096]
                if isinstance(error, Mapping)
                else "malformed ACP error"
            )
        else:
            result = message.get("result", {})
            if not isinstance(result, dict):
                pending.error = "ACP result must be an object"
            else:
                pending.result = result
        pending.event.set()

    def _malformed_response(self, message: Mapping[str, Any], detail: str) -> None:
        response_id = message.get("id")
        if isinstance(response_id, str) and response_id.isascii() and response_id.isdigit():
            response_id = int(response_id)
        with self._pending_lock:
            pending = self._pending.pop(response_id, None)
        if pending is not None:
            pending.error = detail
            pending.transport_error = True
            pending.event.set()

    def _fail_pending(self, detail: str) -> None:
        with self._pending_lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for call in pending:
            call.error = detail
            call.transport_error = True
            call.event.set()


PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class AcpProviderSpec:
    """Launch configuration for one ACP-speaking agent binary."""

    name: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        name = self.name.strip()
        if (
            not name
            or len(name) > 128
            or not name.isascii()
            or not name[0].isalnum()
            or any(not (value.isalnum() or value in "._-") for value in name)
        ):
            raise RuntimeContractError("provider name must be a portable identifier")
        if not self.argv or any(
            not isinstance(value, str) or not value.strip() or "\x00" in value
            for value in self.argv
        ):
            raise RuntimeContractError("provider argv must contain non-empty arguments")
        object.__setattr__(self, "name", name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_worker_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError("worker input must not be empty")
    if len(value.encode("utf-8")) > _MAX_RPC_BYTES // 2:
        raise RuntimeContractError("worker input exceeds 512 KiB")
    return value


@dataclass
class _SessionState:
    process: subprocess.Popen[bytes]
    worktree_path: str
    agent_id: str
    connection: AcpConnection | None = None
    session_id: str = ""
    state: RuntimeWorkerState = RuntimeWorkerState.IDLE
    started_at: datetime | None = None
    updated_at: datetime | None = None
    exit_code: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class AcpRuntimeAdapter:
    """RuntimePort implementation driving one ACP agent process per Change Set."""

    def __init__(
        self,
        spec: AcpProviderSpec,
        *,
        launch: Callable[[Sequence[str]], subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self._spec = spec
        self._launch = launch
        self._sessions: dict[str, _SessionState] = {}
        self._sessions_lock = threading.Lock()
        self._current_worktree_hint = ""
        self._owned_worktrees: set[str] = set()

    def attach_workspace(self, repo_path: str) -> RuntimeWorkspace:
        resolved = str(Path(repo_path).resolve())
        if not Path(resolved).is_dir():
            raise RuntimeAdapterError(
                RuntimeErrorCode.WORKSPACE_NOT_FOUND,
                self._spec.name,
                "attach_workspace",
                f"repository directory not found: {resolved}",
            )
        try:
            top_level = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=resolved,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeAdapterError(
                RuntimeErrorCode.WORKSPACE_NOT_FOUND,
                self._spec.name,
                "attach_workspace",
                "workspace is not a readable Git worktree",
            ) from error
        if Path(top_level).resolve() != Path(resolved):
            raise RuntimeAdapterError(
                RuntimeErrorCode.CONFLICT,
                self._spec.name,
                "attach_workspace",
                "workspace must identify the Git worktree root",
            )
        return RuntimeWorkspace(self._spec.name, resolved, resolved)

    def ensure_change_workspace(
        self, workspace: RuntimeWorkspace, change_set_id: str, base: GitObjectId
    ) -> RuntimeChangeWorkspace:
        self._require_workspace(workspace)
        existing = self.get_change_workspace(workspace, change_set_id, base)
        if existing is not None:
            return existing
        worktree_path = self._worktree_path(workspace, change_set_id)
        try:
            subprocess.run(
                ["git", "worktree", "add", "--detach", worktree_path, base.value],
                cwd=workspace.repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            detail = getattr(error, "stderr", None) or str(error)
            raise RuntimeAdapterError(
                RuntimeErrorCode.RUNTIME_FAILED,
                self._spec.name,
                "ensure_change_workspace",
                detail,
            ) from error
        self._current_worktree_hint = worktree_path
        self._owned_worktrees.add(str(Path(worktree_path).resolve()))
        return self._change_workspace(workspace, change_set_id, base, worktree_path)

    def get_change_workspace(
        self, workspace: RuntimeWorkspace, change_set_id: str, base: GitObjectId
    ) -> RuntimeChangeWorkspace | None:
        self._require_workspace(workspace)
        worktree_path = self._worktree_path(workspace, change_set_id)
        if not Path(worktree_path).is_dir():
            return None
        try:
            ancestry = subprocess.run(
                ["git", "merge-base", "--is-ancestor", base.value, "HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            if ancestry.returncode not in {0, 1}:
                raise subprocess.CalledProcessError(
                    ancestry.returncode,
                    ancestry.args,
                    output=ancestry.stdout,
                    stderr=ancestry.stderr,
                )
            if ancestry.returncode == 1:
                raise RuntimeAdapterError(
                    RuntimeErrorCode.CONFLICT,
                    self._spec.name,
                    "get_change_workspace",
                    "existing runtime worktree no longer descends from its claimed base",
                )
            if _git_common_dir(worktree_path) != _git_common_dir(workspace.repo_path):
                raise RuntimeAdapterError(
                    RuntimeErrorCode.CONFLICT,
                    self._spec.name,
                    "get_change_workspace",
                    "existing runtime path belongs to a different Git repository",
                )
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeAdapterError(
                RuntimeErrorCode.RUNTIME_FAILED,
                self._spec.name,
                "get_change_workspace",
                "existing runtime worktree is unreadable",
            ) from error
        self._current_worktree_hint = worktree_path
        self._owned_worktrees.add(str(Path(worktree_path).resolve()))
        return self._change_workspace(workspace, change_set_id, base, worktree_path)

    def start_worker(
        self,
        change_workspace: RuntimeChangeWorkspace,
        agent_id: str,
        prompt: str,
        *,
        cols: int | None = None,
        rows: int | None = None,
    ) -> RuntimeWorkerSummary:
        del cols, rows
        self._require_change_workspace(change_workspace)
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise RuntimeContractError("worker agent_id must not be empty")
        _require_worker_text(prompt)
        key = self._key(change_workspace)
        with self._sessions_lock:
            existing = self._sessions.get(key)
        if existing is not None:
            if existing.agent_id != agent_id:
                raise RuntimeAdapterError(
                    RuntimeErrorCode.CONFLICT,
                    self._spec.name,
                    "start_worker",
                    "worker already belongs to a different agent",
                )
            return self._summary(change_workspace, existing)

        process = self._launch_process()
        session = _SessionState(
            process=process,
            worktree_path=change_workspace.worktree_path,
            agent_id=agent_id,
            state=RuntimeWorkerState.IDLE,
            started_at=_now(),
            updated_at=_now(),
        )
        connection = AcpConnection(
            process,
            request_handlers=self._request_handlers(session),
            on_notification=lambda method, params: self._on_notification(
                session, method, params
            ),
        )
        session.connection = connection
        with self._sessions_lock:
            self._sessions[key] = session
        try:
            initialized = connection.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "weftmark", "version": "0"},
                },
            )
            if initialized.get("protocolVersion") != PROTOCOL_VERSION:
                raise RuntimeAdapterError(
                    RuntimeErrorCode.TRANSPORT_FAILED,
                    self._spec.name,
                    "initialize",
                    "agent selected an unsupported ACP protocol version",
                )
            if not isinstance(initialized.get("agentCapabilities"), Mapping) or not isinstance(
                initialized.get("authMethods"), list
            ):
                raise RuntimeAdapterError(
                    RuntimeErrorCode.TRANSPORT_FAILED,
                    self._spec.name,
                    "initialize",
                    "agent returned malformed ACP capabilities",
                )
            created = connection.request(
                "session/new",
                {
                    "cwd": change_workspace.worktree_path,
                    "mcpServers": [],
                    "additionalDirectories": [],
                },
            )
            session_id = created.get("sessionId")
            if (
                not isinstance(session_id, str)
                or not session_id.strip()
                or len(session_id) > 1024
                or "\x00" in session_id
            ):
                raise RuntimeAdapterError(
                    RuntimeErrorCode.TRANSPORT_FAILED,
                    self._spec.name,
                    "session/new",
                    "agent response has an invalid sessionId",
                )
            session.session_id = session_id
            self._send_prompt(session, prompt)
        except RuntimeAdapterError as error:
            with session.lock:
                session.state = RuntimeWorkerState.FAILED
                session.updated_at = _now()
            with self._sessions_lock:
                self._sessions.pop(key, None)
            connection.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            raise
        return self._summary(change_workspace, session)

    def send_worker_input(
        self, change_workspace: RuntimeChangeWorkspace, data: str
    ) -> RuntimeWorkerSummary:
        self._require_change_workspace(change_workspace)
        session = self._require_session(change_workspace)
        self._send_prompt(session, data)
        return self._summary(change_workspace, session)

    def worker_summary(
        self, change_workspace: RuntimeChangeWorkspace
    ) -> RuntimeWorkerSummary:
        self._require_change_workspace(change_workspace)
        with self._sessions_lock:
            session = self._sessions.get(self._key(change_workspace))
        if session is None:
            return self._empty_summary(change_workspace, RuntimeWorkerState.UNKNOWN)
        if session.process.poll() is not None and session.state not in {
            RuntimeWorkerState.EXITED,
            RuntimeWorkerState.FAILED,
        }:
            with session.lock:
                session.state = RuntimeWorkerState.EXITED
                session.exit_code = session.process.returncode
                session.updated_at = _now()
        return self._summary(change_workspace, session)

    def stop_worker(
        self, change_workspace: RuntimeChangeWorkspace
    ) -> RuntimeWorkerSummary:
        self._require_change_workspace(change_workspace, require_base=False)
        key = self._key(change_workspace)
        with self._sessions_lock:
            session = self._sessions.pop(key, None)
        if session is None:
            return self._empty_summary(change_workspace, RuntimeWorkerState.EXITED)
        connection = session.connection
        if connection is not None and session.session_id:
            try:
                connection.notify("session/cancel", {"sessionId": session.session_id})
            except RuntimeAdapterError:
                pass
        if connection is not None:
            connection.close()
        try:
            session.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            session.process.kill()
            session.process.wait(timeout=5)
        with session.lock:
            session.state = RuntimeWorkerState.EXITED
            session.exit_code = session.process.returncode
            session.updated_at = _now()
        return self._summary(change_workspace, session)

    def changes(
        self,
        change_workspace: RuntimeChangeWorkspace,
        mode: RuntimeChangesMode = RuntimeChangesMode.WORKING_COPY,
    ) -> RuntimeChanges:
        self._require_change_workspace(change_workspace)
        if mode is not RuntimeChangesMode.WORKING_COPY:
            raise RuntimeContractError("only working_copy mode is supported in v0")
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z"],
                cwd=change_workspace.worktree_path,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeAdapterError(
                RuntimeErrorCode.RUNTIME_FAILED,
                self._spec.name,
                "changes",
                "cannot inspect runtime worktree",
            ) from error
        files = _parse_porcelain(result.stdout)
        return RuntimeChanges(
            provider=self._spec.name,
            change_set_id=change_workspace.change_set_id,
            mode=mode,
            files=files,
            base=change_workspace.base,
        )

    def cleanup_change_workspace(
        self, change_workspace: RuntimeChangeWorkspace
    ) -> None:
        self._require_change_workspace(
            change_workspace,
            require_exists=False,
            require_base=False,
        )
        if self._key(change_workspace) in self._sessions:
            raise RuntimeAdapterError(
                RuntimeErrorCode.CONFLICT,
                self._spec.name,
                "cleanup_change_workspace",
                "worker must be stopped before cleanup",
            )
        if not Path(change_workspace.worktree_path).exists():
            return
        resolved = str(Path(change_workspace.worktree_path).resolve())
        if resolved not in self._owned_worktrees:
            raise RuntimeAdapterError(
                RuntimeErrorCode.PERMISSION_DENIED,
                self._spec.name,
                "cleanup_change_workspace",
                "adapter does not own the requested worktree",
            )
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", change_workspace.worktree_path],
                cwd=change_workspace.workspace_id,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            detail = getattr(error, "stderr", None) or str(error)
            raise RuntimeAdapterError(
                RuntimeErrorCode.RUNTIME_FAILED,
                self._spec.name,
                "cleanup_change_workspace",
                detail,
            ) from error
        self._owned_worktrees.discard(resolved)

    def _require_workspace(self, workspace: RuntimeWorkspace) -> None:
        if workspace.provider != self._spec.name:
            raise RuntimeAdapterError(
                RuntimeErrorCode.CONFLICT,
                self._spec.name,
                "workspace",
                "workspace provider does not match adapter provider",
            )
        resolved = str(Path(workspace.repo_path).resolve())
        if workspace.workspace_id != resolved:
            raise RuntimeAdapterError(
                RuntimeErrorCode.CONFLICT,
                self._spec.name,
                "workspace",
                "workspace identity does not match its repository path",
            )

    def _require_change_workspace(
        self,
        value: RuntimeChangeWorkspace,
        *,
        require_exists: bool = True,
        require_base: bool = True,
    ) -> None:
        if value.provider != self._spec.name:
            raise RuntimeAdapterError(
                RuntimeErrorCode.CONFLICT,
                self._spec.name,
                "change_workspace",
                "change workspace provider does not match adapter provider",
            )
        workspace = RuntimeWorkspace(
            self._spec.name,
            value.workspace_id,
            value.workspace_id,
        )
        self._require_workspace(workspace)
        expected = self._worktree_path(workspace, value.change_set_id)
        if Path(value.worktree_path).resolve() != Path(expected).resolve():
            raise RuntimeAdapterError(
                RuntimeErrorCode.PERMISSION_DENIED,
                self._spec.name,
                "change_workspace",
                "change workspace path is outside its deterministic runtime location",
            )
        if not require_exists and not Path(expected).exists():
            return
        if not Path(expected).is_dir():
            raise RuntimeAdapterError(
                RuntimeErrorCode.WORKSPACE_NOT_FOUND,
                self._spec.name,
                "change_workspace",
                "runtime worktree is unavailable",
            )
        try:
            if _git_common_dir(expected) != _git_common_dir(value.workspace_id):
                raise RuntimeAdapterError(
                    RuntimeErrorCode.CONFLICT,
                    self._spec.name,
                    "change_workspace",
                    "runtime worktree belongs to a different Git repository",
                )
            if require_base:
                ancestry = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", value.base.value, "HEAD"],
                    cwd=expected,
                    capture_output=True,
                    text=True,
                )
                if ancestry.returncode != 0:
                    raise RuntimeAdapterError(
                        RuntimeErrorCode.CONFLICT,
                        self._spec.name,
                        "change_workspace",
                        "runtime worktree no longer descends from its claimed base",
                    )
        except OSError as error:
            raise RuntimeAdapterError(
                RuntimeErrorCode.RUNTIME_FAILED,
                self._spec.name,
                "change_workspace",
                "cannot validate runtime worktree",
            ) from error

    def _change_workspace(
        self,
        workspace: RuntimeWorkspace,
        change_set_id: str,
        base: GitObjectId,
        worktree_path: str,
    ) -> RuntimeChangeWorkspace:
        return RuntimeChangeWorkspace(
            provider=self._spec.name,
            workspace_id=workspace.workspace_id,
            change_set_id=change_set_id,
            task_id=change_set_id,
            base=base,
            worktree_path=worktree_path,
        )

    def _worktree_path(self, workspace: RuntimeWorkspace, change_set_id: str) -> str:
        return _runtime_worktree_path(workspace.repo_path, change_set_id)

    def _launch_process(self) -> subprocess.Popen[bytes]:
        try:
            if self._launch is not None:
                return self._launch(self._spec.argv)
            return subprocess.Popen(
                self._spec.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise RuntimeAdapterError(
                RuntimeErrorCode.AGENT_UNAVAILABLE,
                self._spec.name,
                "start_worker",
                str(error),
            ) from error

    @staticmethod
    def _key(change_workspace: RuntimeChangeWorkspace) -> str:
        return f"{change_workspace.change_set_id}:{change_workspace.task_id}"

    def _require_session(self, change_workspace: RuntimeChangeWorkspace) -> _SessionState:
        with self._sessions_lock:
            session = self._sessions.get(self._key(change_workspace))
        if session is None:
            raise RuntimeAdapterError(
                RuntimeErrorCode.WORKSPACE_NOT_FOUND,
                self._spec.name,
                "send_worker_input",
                f"no active worker for {change_workspace.change_set_id}",
            )
        return session

    def _send_prompt(self, session: _SessionState, text: str) -> None:
        _require_worker_text(text)
        with session.lock:
            if session.state is RuntimeWorkerState.RUNNING:
                raise RuntimeAdapterError(
                    RuntimeErrorCode.CONFLICT,
                    self._spec.name,
                    "session/prompt",
                    "worker turn is already running",
                )
            if session.state not in {
                RuntimeWorkerState.IDLE,
                RuntimeWorkerState.AWAITING_INPUT,
            }:
                raise RuntimeAdapterError(
                    RuntimeErrorCode.CONFLICT,
                    self._spec.name,
                    "session/prompt",
                    f"worker cannot accept input in state {session.state.value}",
                )
            session.state = RuntimeWorkerState.RUNNING
            session.updated_at = _now()

        def run() -> None:
            assert session.connection is not None
            try:
                completed = session.connection.request(
                    "session/prompt",
                    {
                        "sessionId": session.session_id,
                        "prompt": [{"type": "text", "text": text}],
                    },
                    timeout=_MAX_REQUEST_TIMEOUT_SECONDS,
                )
                if completed.get("stopReason") not in _STOP_REASONS:
                    raise RuntimeAdapterError(
                        RuntimeErrorCode.TRANSPORT_FAILED,
                        self._spec.name,
                        "session/prompt",
                        "agent returned an invalid stopReason",
                    )
            except RuntimeAdapterError:
                with session.lock:
                    if session.state is not RuntimeWorkerState.EXITED:
                        session.state = RuntimeWorkerState.FAILED
                        session.updated_at = _now()
                return
            with session.lock:
                session.state = RuntimeWorkerState.AWAITING_INPUT
                session.updated_at = _now()

        threading.Thread(target=run, daemon=True).start()

    def _summary(
        self, change_workspace: RuntimeChangeWorkspace, session: _SessionState
    ) -> RuntimeWorkerSummary:
        with session.lock:
            return RuntimeWorkerSummary(
                provider=self._spec.name,
                change_set_id=change_workspace.change_set_id,
                task_id=change_workspace.task_id,
                state=session.state,
                agent_id=session.agent_id,
                session_id=session.session_id or None,
                pid=session.process.pid,
                exit_code=session.exit_code,
                started_at=session.started_at,
                updated_at=session.updated_at,
            )

    def _empty_summary(
        self, change_workspace: RuntimeChangeWorkspace, state: RuntimeWorkerState
    ) -> RuntimeWorkerSummary:
        return RuntimeWorkerSummary(
            provider=self._spec.name,
            change_set_id=change_workspace.change_set_id,
            task_id=change_workspace.task_id,
            state=state,
        )

    def _on_notification(
        self, session: _SessionState, method: str, params: dict[str, Any]
    ) -> None:
        if method == "session/update":
            with session.lock:
                session.updated_at = _now()

    def _request_handlers(self, session: _SessionState) -> dict[str, RequestHandler]:
        self._current_worktree_hint = session.worktree_path

        def read(params: dict[str, Any]) -> dict[str, Any]:
            return self._read_text(session.worktree_path, params)

        def write(params: dict[str, Any]) -> dict[str, Any]:
            return self._write_text(session.worktree_path, params)

        def permission(params: dict[str, Any]) -> dict[str, Any]:
            return self._permission(session.worktree_path, params)

        return {
            "fs/read_text_file": read,
            "fs/write_text_file": write,
            "session/request_permission": permission,
        }

    def _handle_read_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._read_text(self._current_worktree_hint, params)

    def _handle_write_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._write_text(self._current_worktree_hint, params)

    def _handle_request_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._permission(self._current_worktree_hint, params)

    def _read_text(self, worktree_path: str, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path_in(worktree_path, str(params.get("path", "")))
        try:
            if not path.is_file() or path.stat().st_size > _MAX_TEXT_FILE_BYTES:
                raise RuntimeAdapterError(
                    RuntimeErrorCode.PERMISSION_DENIED,
                    self._spec.name,
                    "fs/read_text_file",
                    "text file must be regular and at most 4 MiB",
                )
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeAdapterError(
                RuntimeErrorCode.RUNTIME_FAILED,
                self._spec.name,
                "fs/read_text_file",
                str(error),
            ) from error
        line = params.get("line")
        limit = params.get("limit")
        if line is not None and (
            isinstance(line, bool) or not isinstance(line, int) or line < 1
        ):
            raise RuntimeAdapterError(
                RuntimeErrorCode.TRANSPORT_FAILED,
                self._spec.name,
                "fs/read_text_file",
                "line must be a positive integer",
            )
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 0
            or limit > _MAX_TEXT_LINES
        ):
            raise RuntimeAdapterError(
                RuntimeErrorCode.TRANSPORT_FAILED,
                self._spec.name,
                "fs/read_text_file",
                "limit must be between 0 and 100000",
            )
        if line is not None or limit is not None:
            lines = content.splitlines(keepends=True)
            start = (line or 1) - 1
            content = "".join(lines[start : None if limit is None else start + limit])
        return {"content": content}

    def _write_text(self, worktree_path: str, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path_in(worktree_path, str(params.get("path", "")))
        content = params.get("content")
        if not isinstance(content, str):
            raise RuntimeAdapterError(
                RuntimeErrorCode.TRANSPORT_FAILED,
                self._spec.name,
                "fs/write_text_file",
                "content must be text",
            )
        if len(content.encode("utf-8")) > _MAX_TEXT_FILE_BYTES:
            raise RuntimeAdapterError(
                RuntimeErrorCode.PERMISSION_DENIED,
                self._spec.name,
                "fs/write_text_file",
                "text content exceeds 4 MiB",
            )
        try:
            if path.exists() and not path.is_file():
                raise RuntimeAdapterError(
                    RuntimeErrorCode.PERMISSION_DENIED,
                    self._spec.name,
                    "fs/write_text_file",
                    "write target must be a regular file",
                )
            path.write_text(content, encoding="utf-8")
        except OSError as error:
            raise RuntimeAdapterError(
                RuntimeErrorCode.RUNTIME_FAILED,
                self._spec.name,
                "fs/write_text_file",
                str(error),
            ) from error
        return {}

    def _resolve_path_in(self, worktree_path: str, path: str) -> Path:
        if not worktree_path or not path:
            raise RuntimeAdapterError(
                RuntimeErrorCode.PERMISSION_DENIED,
                self._spec.name,
                "fs",
                "worktree and path are required",
            )
        root = Path(worktree_path).resolve()
        candidate = Path(path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise RuntimeAdapterError(
                RuntimeErrorCode.PERMISSION_DENIED,
                self._spec.name,
                "fs",
                f"path outside disposable worktree: {path}",
            ) from None
        return candidate

    def _permission(self, worktree_path: str, params: dict[str, Any]) -> dict[str, Any]:
        tool_call = params.get("toolCall")
        options = params.get("options")
        if not isinstance(tool_call, Mapping) or not isinstance(options, list):
            return {"outcome": {"outcome": "cancelled"}}
        locations = tool_call.get("locations")
        scoped = (
            tool_call.get("kind") in {"read", "edit"}
            and isinstance(locations, list)
            and bool(locations)
            and all(
                isinstance(location, Mapping)
                and isinstance(location.get("path"), str)
                and self._is_inside(worktree_path, location["path"])
                for location in locations
            )
        )
        allow = next(
            (
                value
                for value in options
                if isinstance(value, Mapping)
                and value.get("kind") == "allow_once"
                and isinstance(value.get("optionId"), str)
                and value["optionId"]
            ),
            None,
        )
        reject = next(
            (
                value
                for value in options
                if isinstance(value, Mapping)
                and value.get("kind") == "reject_once"
                and isinstance(value.get("optionId"), str)
                and value["optionId"]
            ),
            None,
        )
        selected = allow if scoped and allow is not None else reject
        if selected is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {
            "outcome": {"outcome": "selected", "optionId": selected["optionId"]}
        }

    @staticmethod
    def _is_inside(worktree_path: str, path: str) -> bool:
        if not worktree_path:
            return False
        try:
            Path(path).resolve().relative_to(Path(worktree_path).resolve())
            return True
        except ValueError:
            return False


def _parse_porcelain(output: bytes) -> tuple[RuntimeFileChange, ...]:
    records = output.decode("utf-8", errors="strict").split("\0")
    files: list[RuntimeFileChange] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise RuntimeContractError("git returned malformed porcelain output")
        status, path = record[:2], record[3:]
        if "R" in status or "C" in status:
            if index >= len(records) or not records[index]:
                raise RuntimeContractError("git rename lacks source path")
            old_path = records[index]
            index += 1
            kind = GitChangeKind.RENAMED if "R" in status else GitChangeKind.COPIED
            files.append(RuntimeFileChange(path=path, old_path=old_path, kind=kind))
            continue
        if "?" in status or "A" in status:
            kind = GitChangeKind.ADDED
        elif "D" in status:
            kind = GitChangeKind.DELETED
        else:
            kind = GitChangeKind.MODIFIED
        files.append(RuntimeFileChange(path=path, kind=kind))
    return tuple(sorted(files, key=lambda value: (value.path, value.old_path or "")))


def _git_common_dir(path: str) -> Path:
    try:
        raw = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeAdapterError(
            RuntimeErrorCode.RUNTIME_FAILED,
            "acp",
            "workspace",
            "cannot resolve Git common directory",
        ) from error
    return Path(raw).resolve()


class AcpRuntimeProxy:
    """Reconnectable RuntimePort proxy to a same-user local ACP control host."""

    def __init__(self, spec: AcpProviderSpec) -> None:
        self._spec = spec
        self._workspace: RuntimeWorkspace | None = None
        self._change_workspace: RuntimeChangeWorkspace | None = None
        self._address: Path | None = None

    def attach_workspace(self, repo_path: str) -> RuntimeWorkspace:
        adapter = AcpRuntimeAdapter(self._spec)
        self._workspace = adapter.attach_workspace(repo_path)
        return self._workspace

    def ensure_change_workspace(
        self, workspace: RuntimeWorkspace, change_set_id: str, base: GitObjectId
    ) -> RuntimeChangeWorkspace:
        self._bind(workspace, change_set_id, base)
        payload = self._request(
            "ensure",
            {"workspace": _workspace_payload(workspace), "change_set_id": change_set_id, "base": base.value},
            spawn=True,
        )
        self._change_workspace = _change_workspace_from_payload(payload["workspace"])
        return self._change_workspace

    def get_change_workspace(
        self, workspace: RuntimeWorkspace, change_set_id: str, base: GitObjectId
    ) -> RuntimeChangeWorkspace | None:
        self._bind(workspace, change_set_id, base)
        if not self._host_available():
            self._change_workspace = AcpRuntimeAdapter(
                self._spec
            ).get_change_workspace(workspace, change_set_id, base)
            return self._change_workspace
        payload = self._request(
            "get",
            {"workspace": _workspace_payload(workspace), "change_set_id": change_set_id, "base": base.value},
        )
        raw = payload.get("workspace")
        self._change_workspace = None if raw is None else _change_workspace_from_payload(raw)
        return self._change_workspace

    def start_worker(
        self,
        change_workspace: RuntimeChangeWorkspace,
        agent_id: str,
        prompt: str,
        *,
        cols: int | None = None,
        rows: int | None = None,
    ) -> RuntimeWorkerSummary:
        del cols, rows
        self._remember(change_workspace)
        return _summary_from_payload(
            self._request("start", {"workspace": _change_workspace_payload(change_workspace), "agent_id": agent_id, "prompt": prompt})["summary"]
        )

    def send_worker_input(
        self, change_workspace: RuntimeChangeWorkspace, data: str
    ) -> RuntimeWorkerSummary:
        self._remember(change_workspace)
        return _summary_from_payload(
            self._request("send_input", {"workspace": _change_workspace_payload(change_workspace), "data": data})["summary"]
        )

    def worker_summary(
        self, change_workspace: RuntimeChangeWorkspace
    ) -> RuntimeWorkerSummary:
        self._remember(change_workspace)
        if not self._host_available():
            return RuntimeWorkerSummary(
                self._spec.name, change_workspace.change_set_id, change_workspace.task_id, RuntimeWorkerState.UNKNOWN
            )
        return _summary_from_payload(
            self._request("status", {"workspace": _change_workspace_payload(change_workspace)})["summary"]
        )

    def stop_worker(
        self, change_workspace: RuntimeChangeWorkspace
    ) -> RuntimeWorkerSummary:
        self._remember(change_workspace)
        if not self._host_available():
            return RuntimeWorkerSummary(
                self._spec.name, change_workspace.change_set_id, change_workspace.task_id, RuntimeWorkerState.EXITED
            )
        return _summary_from_payload(
            self._request("stop", {"workspace": _change_workspace_payload(change_workspace)})["summary"]
        )

    def changes(
        self,
        change_workspace: RuntimeChangeWorkspace,
        mode: RuntimeChangesMode = RuntimeChangesMode.WORKING_COPY,
    ) -> RuntimeChanges:
        self._remember(change_workspace)
        payload = self._request(
            "changes", {"workspace": _change_workspace_payload(change_workspace), "mode": mode.value}
        )["changes"]
        return RuntimeChanges(
            provider=self._spec.name,
            change_set_id=change_workspace.change_set_id,
            mode=RuntimeChangesMode(payload["mode"]),
            files=tuple(
                RuntimeFileChange(
                    path=item["path"],
                    kind=GitChangeKind(item["kind"]),
                    old_path=item.get("old_path"),
                )
                for item in payload["files"]
            ),
            base=change_workspace.base,
        )

    def cleanup_change_workspace(self, change_workspace: RuntimeChangeWorkspace) -> None:
        self._remember(change_workspace)
        if self._host_available():
            self._request("cleanup", {"workspace": _change_workspace_payload(change_workspace)})
            return
        raise RuntimeAdapterError(
            RuntimeErrorCode.WORKSPACE_NOT_FOUND,
            self._spec.name,
            "cleanup_change_workspace",
            "runtime host is unavailable; ownership cannot be proven",
        )

    def _bind(self, workspace: RuntimeWorkspace, change_set_id: str, base: GitObjectId) -> None:
        self._workspace = workspace
        self._address = _host_address(workspace.repo_path, self._spec, change_set_id)
        self._change_workspace = RuntimeChangeWorkspace(
            self._spec.name,
            workspace.workspace_id,
            change_set_id,
            change_set_id,
            base,
            _runtime_worktree_path(workspace.repo_path, change_set_id),
        )

    def _remember(self, value: RuntimeChangeWorkspace) -> None:
        if self._workspace is None:
            self._workspace = RuntimeWorkspace(value.provider, value.workspace_id, value.workspace_id)
        if self._address is None:
            self._address = _host_address(
                self._workspace.repo_path, self._spec, value.change_set_id
            )
        self._change_workspace = value

    def _host_available(self) -> bool:
        if self._address is None or not self._address.exists():
            return False
        try:
            _socket_request(self._address, {"operation": "ping"}, timeout=0.5)
            return True
        except RuntimeAdapterError:
            return False

    def _request(
        self, operation: str, params: Mapping[str, Any], *, spawn: bool = False
    ) -> dict[str, Any]:
        if self._address is None:
            raise RuntimeAdapterError(
                RuntimeErrorCode.WORKSPACE_NOT_FOUND, self._spec.name, operation, "runtime host is not bound"
            )
        if spawn and not self._host_available():
            _spawn_host(self._address, self._spec)
        return _socket_request(
            self._address, {"operation": operation, "params": dict(params)}, timeout=35
        )


def _runtime_worktree_path(repo_path: str, change_set_id: str) -> str:
    if (
        not change_set_id
        or len(change_set_id) > 128
        or change_set_id in {".", ".."}
        or any(value in change_set_id for value in ("/", "\\", "\x00"))
    ):
        raise RuntimeContractError("change_set_id is unsafe for a worktree path")
    base = Path(repo_path).resolve()
    return str(base.parent / f".weftmark-runtime-{base.name}-{change_set_id}")


def _host_address(
    repo_path: str, spec: AcpProviderSpec, change_set_id: str
) -> Path:
    provider_identity = json.dumps(
        {"name": spec.name, "argv": spec.argv},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    identity = "\0".join(
        (str(Path(repo_path).resolve()), provider_identity, change_set_id)
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
    directory = Path("/tmp") / f"weftmark-runtime-{os.getuid()}"
    try:
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        metadata = directory.lstat()
        if (
            metadata.st_uid != os.getuid()
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
        ):
            raise RuntimeAdapterError(
                RuntimeErrorCode.PERMISSION_DENIED,
                spec.name,
                "host",
                "runtime control directory is not a private owned directory",
            )
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.chmod(directory, 0o700)
    except OSError as error:
        raise RuntimeAdapterError(
            RuntimeErrorCode.PERMISSION_DENIED,
            spec.name,
            "host",
            "runtime control directory is unavailable",
        ) from error
    return directory / f"{digest}.sock"


def _spawn_host(address: Path, spec: AcpProviderSpec) -> None:
    if address.exists() or address.is_symlink():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                probe.connect(str(address))
        except ConnectionRefusedError:
            metadata = address.lstat()
            if metadata.st_uid != os.getuid() or not stat.S_ISSOCK(metadata.st_mode):
                raise RuntimeAdapterError(
                    RuntimeErrorCode.PERMISSION_DENIED,
                    spec.name,
                    "host",
                    "stale runtime path is not an owned socket",
                ) from None
            address.unlink()
        except FileNotFoundError:
            pass
        except (OSError, TimeoutError) as error:
            raise RuntimeAdapterError(
                RuntimeErrorCode.CONFLICT,
                spec.name,
                "host",
                "existing runtime socket is not safely replaceable",
            ) from error
        else:
            raise RuntimeAdapterError(
                RuntimeErrorCode.CONFLICT,
                spec.name,
                "host",
                "runtime host is already active",
            )
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "weftmark.adapters.acp", "--host", str(address)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        assert process.stdin is not None
        config = (
            json.dumps(
                {"name": spec.name, "argv": list(spec.argv)},
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            + b"\n"
        )
        if len(config) > _MAX_RPC_BYTES:
            raise OSError("runtime provider configuration exceeds 1 MiB")
        process.stdin.write(config)
        process.stdin.close()
    except (OSError, BrokenPipeError) as error:
        raise RuntimeAdapterError(
            RuntimeErrorCode.AGENT_UNAVAILABLE, spec.name, "host", "cannot launch runtime host"
        ) from error
    ready = False
    try:
        for _ in range(100):
            if address.exists():
                try:
                    _socket_request(address, {"operation": "ping"}, timeout=0.2)
                    ready = True
                    return
                except RuntimeAdapterError:
                    pass
            if process.poll() is not None:
                break
            threading.Event().wait(0.02)
        detail = "runtime host did not become ready"
        if process.poll() is not None and process.stderr is not None:
            diagnostic = process.stderr.read(4096).decode(
                "utf-8", errors="replace"
            ).strip()
            if diagnostic:
                detail += ": " + diagnostic
        raise RuntimeAdapterError(
            RuntimeErrorCode.AGENT_UNAVAILABLE, spec.name, "host", detail
        )
    finally:
        if process.poll() is not None:
            process.wait(timeout=1)
        elif not ready:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


def _socket_request(address: Path, request: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
    encoded = json.dumps(request, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(encoded) > 1_048_576:
        raise RuntimeAdapterError(
            RuntimeErrorCode.TRANSPORT_FAILED, "acp", "host", "control request exceeds 1 MiB"
        )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(address))
            client.sendall(encoded)
            response = _recv_line(client)
    except (OSError, TimeoutError) as error:
        raise RuntimeAdapterError(
            RuntimeErrorCode.TRANSPORT_FAILED, "acp", "host", "runtime host is unavailable"
        ) from error
    try:
        payload = _strict_json_loads(response)
        if not isinstance(payload, dict):
            raise ValueError("response is not an object")
        if payload.get("ok") is not True:
            code = RuntimeErrorCode(str(payload.get("code", RuntimeErrorCode.RUNTIME_FAILED.value)))
            raise RuntimeAdapterError(code, "acp", "host", str(payload.get("error", "host refused request")))
        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise ValueError("result is not an object")
        return result
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, RuntimeAdapterError):
            raise
        raise RuntimeAdapterError(
            RuntimeErrorCode.TRANSPORT_FAILED, "acp", "host", "runtime host returned malformed JSON"
        ) from error


def _recv_line(connection: socket.socket) -> bytes:
    chunks = bytearray()
    while len(chunks) <= 1_048_576:
        chunk = connection.recv(65536)
        if not chunk:
            break
        chunks.extend(chunk)
        if b"\n" in chunk:
            return bytes(chunks).split(b"\n", 1)[0]
    raise OSError("control response is missing or too large")


def _run_host(address: Path) -> int:
    try:
        raw = sys.stdin.buffer.readline(_MAX_RPC_BYTES + 1)
        if len(raw) > _MAX_RPC_BYTES or not raw.endswith(b"\n"):
            return 2
        config = _strict_json_loads(raw)
        if not isinstance(config, Mapping) or set(config) != {"name", "argv"}:
            return 2
        if not isinstance(config["name"], str) or not isinstance(config["argv"], list):
            return 2
        spec = AcpProviderSpec(config["name"], tuple(config["argv"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 2
    adapter = AcpRuntimeAdapter(spec)
    should_exit = False
    stopped_deadline: float | None = None
    try:
        if address.exists():
            return 3
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(address))
            os.chmod(address, 0o600)
            server.listen(8)
            server.settimeout(0.5)
            while not should_exit:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    if stopped_deadline is not None and time.monotonic() >= stopped_deadline:
                        break
                    continue
                with connection:
                    if not _same_uid(connection):
                        _host_reply(connection, error="peer uid is not authorized", code=RuntimeErrorCode.PERMISSION_DENIED)
                        continue
                    try:
                        request = _strict_json_loads(_recv_line(connection))
                        result, should_exit = _dispatch_host(adapter, request)
                        _host_reply(connection, result=result)
                        if request.get("operation") == "stop":
                            stopped_deadline = time.monotonic() + 5
                    except RuntimeAdapterError as error:
                        _host_reply(connection, error=error.detail, code=error.code)
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        _host_reply(connection, error="malformed control request", code=RuntimeErrorCode.TRANSPORT_FAILED)
    finally:
        try:
            address.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def _same_uid(connection: socket.socket) -> bool:
    if not hasattr(socket, "SO_PEERCRED"):
        return True
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _, uid, _ = struct.unpack("3i", credentials)
    return uid == os.getuid()


def _strict_json_loads(value: str | bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = item
        return result

    return json.loads(value, object_pairs_hook=reject_duplicates)


def _dispatch_host(
    adapter: AcpRuntimeAdapter, request: Mapping[str, Any]
) -> tuple[dict[str, Any], bool]:
    operation = str(request.get("operation", ""))
    if operation == "ping":
        return {"alive": True}, False
    params = request.get("params")
    if not isinstance(params, Mapping):
        raise ValueError("params must be an object")
    if operation in {"ensure", "get"}:
        workspace = _workspace_from_payload(params["workspace"])
        base = GitObjectId(str(params["base"]))
        method = adapter.ensure_change_workspace if operation == "ensure" else adapter.get_change_workspace
        change = method(workspace, str(params["change_set_id"]), base)
        return {"workspace": None if change is None else _change_workspace_payload(change)}, False
    change = _change_workspace_from_payload(params["workspace"])
    if operation == "start":
        summary = adapter.start_worker(change, str(params["agent_id"]), str(params["prompt"]))
        return {"summary": _summary_payload(summary)}, False
    if operation == "send_input":
        summary = adapter.send_worker_input(change, str(params["data"]))
        return {"summary": _summary_payload(summary)}, False
    if operation == "status":
        return {"summary": _summary_payload(adapter.worker_summary(change))}, False
    if operation == "stop":
        return {"summary": _summary_payload(adapter.stop_worker(change))}, False
    if operation == "changes":
        changes = adapter.changes(change, RuntimeChangesMode(str(params["mode"])))
        return {"changes": {"mode": changes.mode.value, "files": [
            {"path": item.path, "old_path": item.old_path, "kind": item.kind.value} for item in changes.files
        ]}}, False
    if operation == "cleanup":
        adapter.cleanup_change_workspace(change)
        return {}, True
    raise ValueError("unknown operation")


def _host_reply(
    connection: socket.socket,
    *,
    result: Mapping[str, Any] | None = None,
    error: str | None = None,
    code: RuntimeErrorCode = RuntimeErrorCode.RUNTIME_FAILED,
) -> None:
    payload = {"ok": error is None, "result": dict(result or {})}
    if error is not None:
        payload.update({"error": error, "code": code.value})
    connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")


def _workspace_payload(value: RuntimeWorkspace) -> dict[str, Any]:
    return {"provider": value.provider, "workspace_id": value.workspace_id, "repo_path": value.repo_path}


def _workspace_from_payload(value: Mapping[str, Any]) -> RuntimeWorkspace:
    return RuntimeWorkspace(str(value["provider"]), str(value["workspace_id"]), str(value["repo_path"]))


def _change_workspace_payload(value: RuntimeChangeWorkspace) -> dict[str, Any]:
    return {"provider": value.provider, "workspace_id": value.workspace_id, "change_set_id": value.change_set_id,
            "task_id": value.task_id, "base": value.base.value, "worktree_path": value.worktree_path}


def _change_workspace_from_payload(value: Mapping[str, Any]) -> RuntimeChangeWorkspace:
    return RuntimeChangeWorkspace(str(value["provider"]), str(value["workspace_id"]), str(value["change_set_id"]),
                                  str(value["task_id"]), GitObjectId(str(value["base"])), str(value["worktree_path"]))


def _summary_payload(value: RuntimeWorkerSummary) -> dict[str, Any]:
    return {"provider": value.provider, "change_set_id": value.change_set_id, "task_id": value.task_id,
            "state": value.state.value, "agent_id": value.agent_id, "session_id": value.session_id,
            "pid": value.pid, "exit_code": value.exit_code,
            "started_at": None if value.started_at is None else value.started_at.isoformat(),
            "updated_at": None if value.updated_at is None else value.updated_at.isoformat()}


def _summary_from_payload(value: Mapping[str, Any]) -> RuntimeWorkerSummary:
    return RuntimeWorkerSummary(
        str(value["provider"]), str(value["change_set_id"]), str(value["task_id"]), RuntimeWorkerState(str(value["state"])),
        agent_id=None if value.get("agent_id") is None else str(value["agent_id"]),
        session_id=None if value.get("session_id") is None else str(value["session_id"]),
        pid=None if value.get("pid") is None else int(value["pid"]),
        exit_code=None if value.get("exit_code") is None else int(value["exit_code"]),
        started_at=None if value.get("started_at") is None else datetime.fromisoformat(str(value["started_at"])),
        updated_at=None if value.get("updated_at") is None else datetime.fromisoformat(str(value["updated_at"])),
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--host":
        raise SystemExit(_run_host(Path(sys.argv[2])))
    raise SystemExit(2)
