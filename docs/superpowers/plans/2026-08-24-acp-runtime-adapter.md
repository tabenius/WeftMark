# ACP Runtime Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let WeftMark start, drive and observe a disposable ACP-speaking coding-agent worker for an owned native task, entirely through the CLI, with no dependency on Frog.

**Architecture:** A hand-rolled newline-delimited JSON-RPC 2.0 client (`adapters/acp.py`) implements the existing `RuntimePort` Protocol by spawning an ACP agent subprocess, owning its disposable Git worktree, and auto-approving only worktree-scoped file/permission requests. A config-driven `RuntimeProviderRegistry` (`application/runtime_registry.py`) maps provider names to launch argv. A `RuntimeWorkerService` (`application/runtime_workers.py`) composes registry + adapter + the existing `TaskClaimService`/ledger to give worker sessions durable, idempotent provenance. `weftmark runtime start|status|send-input|stop` is the CLI surface.

**Tech Stack:** Python 3.11+, stdlib only (`subprocess`, `threading`, `json`) — no new runtime dependency. Tests use `pytest`.

**Spec:** `docs/superpowers/specs/2026-08-23-acp-runtime-adapter-design.md`

## Global Constraints

- No new third-party dependency (hand-rolled ACP client, not the pre-1.0 `agent-client-protocol` PyPI package) — per spec decision 1.
- `GitPort` stays read-only; worktree creation/removal lives only in `adapters/acp.py` — per spec decision 2.
- `fs/read_text_file`/`fs/write_text_file` are served only for paths inside the session's disposable worktree; `session/request_permission` is auto-approved only for `allow_once` options whose tool call is `kind` `"read"` or `"edit"` **and** every `toolCall.locations[].path` resolves inside the worktree — everything else is refused — per spec decision 3.
- `start_worker`/`send_worker_input` must not block for a whole agent turn; a background thread drains `session/update` notifications into an in-memory `RuntimeWorkerState` snapshot; `worker_summary` is a cheap read of that snapshot — per spec decision 4.
- `RuntimeProviderRegistry` must not import `adapters/acp.py`; its own tests use the existing `FakeRuntime` double from `tests/contracts/test_runtime_port.py` — per spec decision 5.
- CLI is the only consumer surface in this phase (no HTTP control, no MCP tools) — per spec decision 6.
- All adapter failures map onto the existing `RuntimeErrorCode` enum (`NOT_AVAILABLE`, `WORKSPACE_NOT_FOUND`, `CONFLICT`, `AGENT_UNAVAILABLE`, `RUNTIME_FAILED`, `TRANSPORT_FAILED`, `PERMISSION_DENIED`) — no new error vocabulary.
- Every new task-plan slice's evidence commands must actually pass before its status moves past `review`, per this repo's own convention (`AGENTS.md`, and the `review` → `done` pattern visible in prior commits).
- Run `python scripts/validate_tasks.py` after any `tasks/*.weft.yml` edit.
- Run the **full** `python -m pytest -q` before every push, not just the new test file, since other agent sessions land work on `main` concurrently.

## Reference: verified ACP wire schema (v1.21.0, `agentclientprotocol/agent-client-protocol`)

Framing: newline-delimited JSON-RPC 2.0 over stdio. The client (WeftMark) writes requests/notifications to the agent subprocess's stdin; the agent writes responses/notifications to its stdout; the agent may write logs to stderr (ignored). No embedded newlines in a message.

| Method | Direction | Params | Result |
|---|---|---|---|
| `initialize` | client→agent, request | `protocolVersion` (uint16), `clientCapabilities: {fs: {readTextFile: true, writeTextFile: true}, terminal: false}`, `clientInfo: {name, version}` | `protocolVersion`, `agentCapabilities`, `authMethods` |
| `session/new` | client→agent, request | `cwd` (absolute path string), `mcpServers` (`[]`), `additionalDirectories` (`[]`) | `sessionId` (string) |
| `session/prompt` | client→agent, request | `sessionId`, `prompt` (`ContentBlock[]`, e.g. `[{"type": "text", "text": "..."}]`) | `stopReason` (one of `end_turn`, `max_tokens`, `max_turn_requests`, `refusal`, `cancelled`) |
| `session/cancel` | client→agent, **notification** | `sessionId` | none |
| `session/update` | agent→client, **notification** | `sessionId`, `update: {sessionUpdate: <tag>, ...}` — tags include `user_message_chunk`, `agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`, `plan` | none |
| `fs/read_text_file` | agent→client, request | `sessionId`, `path` (absolute), `line?`, `limit?` | `{content: string}` |
| `fs/write_text_file` | agent→client, request | `sessionId`, `path` (absolute), `content` | `{}` |
| `session/request_permission` | agent→client, request | `sessionId`, `toolCall: {toolCallId, kind?, status?, locations?: [{path, line?}]}`, `options: [{optionId, name, kind}]` where `kind` is one of `allow_once`/`allow_always`/`reject_once`/`reject_always` | `{outcome: {outcome: "selected", optionId} | {outcome: "cancelled"}}` |

`ToolKind` values relevant to the permission policy: `read`, `edit`, `delete`, `move`, `search`, `execute`, `think`, `fetch`, `switch_mode`, `other`.

---

## Task 1: ACP JSON-RPC connection

**Files:**
- Create: `src/weftmark/adapters/acp.py`
- Test: `tests/adapters/test_acp.py`

**Interfaces:**
- Produces: `AcpConnectionError(RuntimeAdapterError)` (import from `weftmark.application.ports.runtime`: `RuntimeAdapterError`, `RuntimeErrorCode`); `class AcpConnection` with:
  - `__init__(self, process: subprocess.Popen[bytes], *, request_handlers: Mapping[str, Callable[[dict], dict]], on_notification: Callable[[str, dict], None]) -> None`
  - `def request(self, method: str, params: dict, *, timeout: float = 30.0) -> dict`
  - `def notify(self, method: str, params: dict) -> None`
  - `def close(self) -> None`

This task builds only the transport — no ACP semantics yet. It is tested against a real subprocess running a tiny fixture Python script (`tests/adapters/test_acp.py` writes the fixture script to a temp file and launches it with `sys.executable`), so the reader thread, request/response correlation, and inbound-request dispatch are exercised for real, not mocked.

- [ ] **Step 1: Write the failing test for a request/response round trip**

Create `tests/adapters/test_acp.py`:

```python
"""Tests for the hand-rolled ACP stdio JSON-RPC connection and runtime adapter."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from weftmark.adapters.acp import AcpConnection
from weftmark.application.ports.runtime import RuntimeAdapterError, RuntimeErrorCode

_ECHO_AGENT = """
import json
import sys

def _write(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "ping":
        _write({"jsonrpc": "2.0", "id": message["id"], "result": {"pong": message["params"]["value"]}})
    elif message["method"] == "boom":
        _write({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "boom failed"}})
"""


@pytest.fixture
def echo_agent(tmp_path: Path) -> subprocess.Popen[bytes]:
    script = tmp_path / "echo_agent.py"
    script.write_text(textwrap.dedent(_ECHO_AGENT), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    yield process
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)


def test_request_returns_matching_result(echo_agent: subprocess.Popen[bytes]) -> None:
    connection = AcpConnection(echo_agent, request_handlers={}, on_notification=lambda method, params: None)
    try:
        result = connection.request("ping", {"value": 42})
    finally:
        connection.close()
    assert result == {"pong": 42}


def test_request_raises_on_agent_error(echo_agent: subprocess.Popen[bytes]) -> None:
    connection = AcpConnection(echo_agent, request_handlers={}, on_notification=lambda method, params: None)
    try:
        with pytest.raises(RuntimeAdapterError) as excinfo:
            connection.request("boom", {})
    finally:
        connection.close()
    assert excinfo.value.code is RuntimeErrorCode.RUNTIME_FAILED
    assert "boom failed" in excinfo.value.detail


def test_request_times_out_when_agent_is_silent(tmp_path: Path) -> None:
    script = tmp_path / "silent_agent.py"
    script.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    connection = AcpConnection(process, request_handlers={}, on_notification=lambda method, params: None)
    try:
        with pytest.raises(RuntimeAdapterError) as excinfo:
            connection.request("ping", {"value": 1}, timeout=0.2)
    finally:
        connection.close()
        process.kill()
        process.wait(timeout=5)
    assert excinfo.value.code is RuntimeErrorCode.TRANSPORT_FAILED
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/adapters/test_acp.py -v`
Expected: `ModuleNotFoundError: No module named 'weftmark.adapters.acp'` (or `ImportError: cannot import name 'AcpConnection'`).

- [ ] **Step 3: Implement `AcpConnection`**

Create `src/weftmark/adapters/acp.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/adapters/test_acp.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/weftmark/adapters/acp.py tests/adapters/test_acp.py
git commit -m "feat: add hand-rolled ACP stdio JSON-RPC connection"
```

---

## Task 2: ACP session lifecycle and worker state (RuntimePort core)

**Files:**
- Modify: `src/weftmark/adapters/acp.py` (add `AcpRuntimeAdapter`, `AcpProviderSpec`, `_SessionState`)
- Modify: `tests/adapters/test_acp.py` (add a fuller fixture ACP agent script and worker-lifecycle tests)

**Interfaces:**
- Consumes: `AcpConnection` from Task 1; from `weftmark.application.ports.runtime`: `RuntimePort`, `RuntimeWorkspace`, `RuntimeChangeWorkspace`, `RuntimeWorkerSummary`, `RuntimeWorkerState`, `RuntimeAdapterError`, `RuntimeErrorCode`, `RuntimeContractError`.
- Produces:
  - `@dataclass(frozen=True, slots=True) class AcpProviderSpec: name: str; argv: tuple[str, ...]`
  - `class AcpRuntimeAdapter` implementing `attach_workspace`, `start_worker`, `send_worker_input`, `stop_worker`, `worker_summary` (worktree/`ensure_change_workspace`/`changes`/`cleanup_change_workspace` land in Task 3 — until then they raise `NotImplementedError` so the class is importable and partially testable).
  - `def __init__(self, spec: AcpProviderSpec, *, launch: Callable[[Sequence[str]], subprocess.Popen[bytes]] = subprocess.Popen) -> None`

This task covers: spawning the subprocess, `initialize` + `session/new` handshake, `session/prompt` (non-blocking — sent from a background thread since `session/prompt` doesn't return until the whole turn ends), and turning `session/update` notifications into `RuntimeWorkerState` transitions that `worker_summary` reads back synchronously.

- [ ] **Step 1: Write the failing test with a fuller fixture ACP agent**

Add to `tests/adapters/test_acp.py` (append; keep the Task 1 imports/fixtures):

```python
from datetime import datetime, timezone

from weftmark.adapters.acp import AcpProviderSpec, AcpRuntimeAdapter
from weftmark.application.ports.runtime import RuntimeWorkerState

_STUB_AGENT = """
import json
import sys
import threading
import time

def _write(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()

def _handle(message):
    method = message["method"]
    if method == "initialize":
        return {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}
    if method == "session/new":
        return {"sessionId": "sess-1"}
    if method == "session/prompt":
        session_id = message["params"]["sessionId"]
        def _run_turn():
            time.sleep(0.05)
            _write({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "working"}},
                },
            })
        threading.Thread(target=_run_turn, daemon=True).start()
        return {"stopReason": "end_turn"}
    return None

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if "id" not in message:
        continue
    result = _handle(message)
    if result is None:
        _write({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "unhandled"}})
    else:
        _write({"jsonrpc": "2.0", "id": message["id"], "result": result})
"""


def _spawn_stub_adapter(tmp_path: Path) -> AcpRuntimeAdapter:
    script = tmp_path / "stub_agent.py"
    script.write_text(textwrap.dedent(_STUB_AGENT), encoding="utf-8")
    spec = AcpProviderSpec(name="stub-acp", argv=(sys.executable, str(script)))
    return AcpRuntimeAdapter(spec)


def test_start_worker_reaches_running_then_completed(tmp_path: Path) -> None:
    from weftmark.application.ports.git import GitObjectId
    from weftmark.application.ports.runtime import RuntimeChangeWorkspace

    adapter = _spawn_stub_adapter(tmp_path)
    workspace = RuntimeChangeWorkspace(
        provider="stub-acp",
        workspace_id="ws-1",
        change_set_id="chg-1",
        task_id="task-1",
        base=GitObjectId(value="0" * 40),
        worktree_path=str(tmp_path),
    )
    try:
        summary = adapter.start_worker(workspace, "agent-1", "do the thing")
        assert summary.state in {RuntimeWorkerState.RUNNING, RuntimeWorkerState.AWAITING_INPUT}

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            summary = adapter.worker_summary(workspace)
            if summary.state is RuntimeWorkerState.AWAITING_INPUT:
                break
            time.sleep(0.02)
        assert summary.state is RuntimeWorkerState.AWAITING_INPUT
    finally:
        adapter.stop_worker(workspace)


def test_stop_worker_marks_exited(tmp_path: Path) -> None:
    from weftmark.application.ports.git import GitObjectId
    from weftmark.application.ports.runtime import RuntimeChangeWorkspace

    adapter = _spawn_stub_adapter(tmp_path)
    workspace = RuntimeChangeWorkspace(
        provider="stub-acp",
        workspace_id="ws-1",
        change_set_id="chg-1",
        task_id="task-1",
        base=GitObjectId(value="0" * 40),
        worktree_path=str(tmp_path),
    )
    adapter.start_worker(workspace, "agent-1", "do the thing")
    summary = adapter.stop_worker(workspace)
    assert summary.state is RuntimeWorkerState.EXITED
```

Add `import time` to the top-of-file imports in `tests/adapters/test_acp.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/adapters/test_acp.py -v -k "worker"`
Expected: FAIL — `ImportError: cannot import name 'AcpProviderSpec'`.

- [ ] **Step 3: Implement session lifecycle in `AcpRuntimeAdapter`**

Append to `src/weftmark/adapters/acp.py` (after `AcpConnection`, keep all Task 1 code):

```python
import subprocess as _subprocess  # already imported above; kept for clarity in diffs
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from weftmark.application.ports.git import GitChangeKind, GitObjectId
from weftmark.application.ports.runtime import (
    RuntimeChangeWorkspace,
    RuntimeChanges,
    RuntimeChangesMode,
    RuntimeContractError,
    RuntimeFileChange,
    RuntimeWorkerState,
    RuntimeWorkerSummary,
    RuntimeWorkspace,
)

PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class AcpProviderSpec:
    """Launch configuration for one ACP-speaking agent binary."""

    name: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise RuntimeContractError("provider name must not be empty")
        if not self.argv:
            raise RuntimeContractError("provider argv must not be empty")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _SessionState:
    connection: AcpConnection
    process: subprocess.Popen[bytes]
    session_id: str
    worktree_path: str
    state: RuntimeWorkerState = RuntimeWorkerState.IDLE
    started_at: datetime | None = None
    updated_at: datetime | None = None
    exit_code: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class AcpRuntimeAdapter:
    """RuntimePort implementation driving one ACP agent binary per session."""

    def __init__(
        self,
        spec: AcpProviderSpec,
        *,
        launch: Callable[[Sequence[str]], subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self._spec = spec
        self._launch = launch
        self._sessions: dict[str, _SessionState] = {}
        self._sessions_lock = threading.Lock()

    def attach_workspace(self, repo_path: str) -> RuntimeWorkspace:
        return RuntimeWorkspace(provider=self._spec.name, workspace_id=repo_path, repo_path=repo_path)

    def start_worker(
        self,
        change_workspace: RuntimeChangeWorkspace,
        agent_id: str,
        prompt: str,
        *,
        cols: int | None = None,
        rows: int | None = None,
    ) -> RuntimeWorkerSummary:
        key = self._key(change_workspace)
        with self._sessions_lock:
            existing = self._sessions.get(key)
        if existing is not None:
            return self._summary(change_workspace, existing)

        process = self._launch_process()
        session = _SessionState(
            connection=None,  # type: ignore[arg-type]  # set immediately below
            process=process,
            session_id="",
            worktree_path=change_workspace.worktree_path,
            state=RuntimeWorkerState.RUNNING,
            started_at=_now(),
            updated_at=_now(),
        )
        connection = AcpConnection(
            process,
            request_handlers=self._request_handlers(session),
            on_notification=lambda method, params: self._on_notification(session, method, params),
        )
        session.connection = connection
        with self._sessions_lock:
            self._sessions[key] = session

        try:
            connection.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": False},
                    "clientInfo": {"name": "weftmark", "version": "0"},
                },
            )
            new_session = connection.request(
                "session/new",
                {"cwd": change_workspace.worktree_path, "mcpServers": [], "additionalDirectories": []},
            )
            session.session_id = new_session["sessionId"]
            self._send_prompt(session, prompt)
        except RuntimeAdapterError:
            with session.lock:
                session.state = RuntimeWorkerState.FAILED
                session.updated_at = _now()
            raise
        return self._summary(change_workspace, session)

    def send_worker_input(self, change_workspace: RuntimeChangeWorkspace, data: str) -> RuntimeWorkerSummary:
        session = self._require_session(change_workspace)
        self._send_prompt(session, data)
        return self._summary(change_workspace, session)

    def worker_summary(self, change_workspace: RuntimeChangeWorkspace) -> RuntimeWorkerSummary:
        session = self._sessions.get(self._key(change_workspace))
        if session is None:
            return RuntimeWorkerSummary(
                provider=self._spec.name,
                change_set_id=change_workspace.change_set_id,
                task_id=change_workspace.task_id,
                state=RuntimeWorkerState.UNKNOWN,
            )
        return self._summary(change_workspace, session)

    def stop_worker(self, change_workspace: RuntimeChangeWorkspace) -> RuntimeWorkerSummary:
        key = self._key(change_workspace)
        with self._sessions_lock:
            session = self._sessions.pop(key, None)
        if session is None:
            return RuntimeWorkerSummary(
                provider=self._spec.name,
                change_set_id=change_workspace.change_set_id,
                task_id=change_workspace.task_id,
                state=RuntimeWorkerState.EXITED,
            )
        if session.session_id:
            try:
                session.connection.notify("session/cancel", {"sessionId": session.session_id})
            except Exception:  # noqa: BLE001 - best-effort cancel before teardown
                pass
        session.connection.close()
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

    # -- worktree/diff methods land in Task 3 --

    def ensure_change_workspace(
        self, workspace: RuntimeWorkspace, change_set_id: str, base: GitObjectId
    ) -> RuntimeChangeWorkspace:
        raise NotImplementedError("implemented in Task 3")

    def get_change_workspace(
        self, workspace: RuntimeWorkspace, change_set_id: str, base: GitObjectId
    ) -> RuntimeChangeWorkspace | None:
        raise NotImplementedError("implemented in Task 3")

    def changes(
        self,
        change_workspace: RuntimeChangeWorkspace,
        mode: RuntimeChangesMode = RuntimeChangesMode.WORKING_COPY,
    ) -> RuntimeChanges:
        raise NotImplementedError("implemented in Task 3")

    def cleanup_change_workspace(self, change_workspace: RuntimeChangeWorkspace) -> None:
        raise NotImplementedError("implemented in Task 3")

    # -- internals --

    def _launch_process(self) -> subprocess.Popen[bytes]:
        return self._launch(self._spec.argv)

    def _key(self, change_workspace: RuntimeChangeWorkspace) -> str:
        return f"{change_workspace.change_set_id}:{change_workspace.task_id}"

    def _require_session(self, change_workspace: RuntimeChangeWorkspace) -> _SessionState:
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
        with session.lock:
            session.state = RuntimeWorkerState.RUNNING
            session.updated_at = _now()
        session.connection.request(
            "session/prompt",
            {"sessionId": session.session_id, "prompt": [{"type": "text", "text": text}]},
        )

    def _summary(self, change_workspace: RuntimeChangeWorkspace, session: _SessionState) -> RuntimeWorkerSummary:
        with session.lock:
            return RuntimeWorkerSummary(
                provider=self._spec.name,
                change_set_id=change_workspace.change_set_id,
                task_id=change_workspace.task_id,
                state=session.state,
                pid=session.process.pid if session.process.pid else None,
                exit_code=session.exit_code,
                started_at=session.started_at,
                updated_at=session.updated_at,
            )

    def _on_notification(self, session: _SessionState, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        update = params.get("update", {})
        tag = update.get("sessionUpdate")
        with session.lock:
            if tag in {"agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update", "plan"}:
                session.state = RuntimeWorkerState.AWAITING_INPUT
            session.updated_at = _now()

    def _request_handlers(self, session: _SessionState) -> dict[str, RequestHandler]:
        # fs/read_text_file, fs/write_text_file and session/request_permission
        # handlers are added in Task 3, once the worktree is owned here.
        return {}
```

Note: `session/update`'s `"agent_message_chunk"` etc. drive the session to `AWAITING_INPUT` as a simple v0 heuristic — the worker is "doing something visible" and the operator can reasonably poll/observe it. A more precise `RUNNING` vs `AWAITING_INPUT` split (e.g. only `AWAITING_INPUT` after `stopReason` comes back) is future work; note this explicitly in `docs/contracts/acp-runtime-adapter-v0.md` in Task 3's Step 6 so it isn't silently assumed to be exact.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/adapters/test_acp.py -v`
Expected: all tests PASS (Task 1's 3 tests plus the 2 new ones).

- [ ] **Step 5: Run the full suite to check nothing else broke**

Run: `python -m pytest -q`
Expected: all tests PASS (no regressions).

- [ ] **Step 6: Commit**

```bash
git add src/weftmark/adapters/acp.py tests/adapters/test_acp.py
git commit -m "feat: add ACP session lifecycle to the runtime adapter"
```

---

## Task 3: Worktree lifecycle, fs/permission handlers, and diffing

**Files:**
- Modify: `src/weftmark/adapters/acp.py` (implement `ensure_change_workspace`, `get_change_workspace`, `changes`, `cleanup_change_workspace`; add fs/permission request handlers)
- Modify: `tests/adapters/test_acp.py` (worktree + permission-policy tests)
- Create: `docs/contracts/acp-runtime-adapter-v0.md`

**Interfaces:**
- Consumes: `LocalGit`/`LocalGitError` from `weftmark.adapters.git_local` (only to resolve `base` to a real commit before creating the worktree — worktree mutation itself is plain `subprocess` calls to `git worktree add`/`remove`, kept local to this adapter per the spec's decision 2).
- Produces: the four remaining `RuntimePort` methods, now real; two new private handlers `_handle_read_text_file`, `_handle_write_text_file`, `_handle_request_permission` wired into `_request_handlers`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/adapters/test_acp.py`:

```python
import subprocess as sp


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    sp.run(["git", "add", "README.md"], cwd=repo, check=True)
    sp.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_ensure_change_workspace_creates_disposable_worktree(tmp_path: Path) -> None:
    from weftmark.application.ports.git import GitObjectId

    repo = _init_repo(tmp_path)
    head = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    adapter = AcpRuntimeAdapter(AcpProviderSpec(name="stub-acp", argv=(sys.executable, "-c", "pass")))
    workspace = adapter.attach_workspace(str(repo))

    change_workspace = adapter.ensure_change_workspace(workspace, "chg-1", GitObjectId(value=head))
    try:
        assert Path(change_workspace.worktree_path).is_dir()
        assert (Path(change_workspace.worktree_path) / "README.md").exists()
        assert change_workspace.worktree_path != str(repo)

        again = adapter.ensure_change_workspace(workspace, "chg-1", GitObjectId(value=head))
        assert again.worktree_path == change_workspace.worktree_path
    finally:
        adapter.cleanup_change_workspace(change_workspace)
    assert not Path(change_workspace.worktree_path).exists()


def test_fs_read_write_scoped_to_worktree(tmp_path: Path) -> None:
    from weftmark.application.ports.git import GitObjectId

    repo = _init_repo(tmp_path)
    head = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    adapter = _spawn_stub_adapter(tmp_path / "stub")
    workspace = adapter.attach_workspace(str(repo))
    change_workspace = adapter.ensure_change_workspace(workspace, "chg-2", GitObjectId(value=head))
    try:
        inside = str(Path(change_workspace.worktree_path) / "README.md")
        content = adapter._handle_read_text_file({"sessionId": "s", "path": inside})
        assert content["content"] == "hello\n"

        adapter._handle_write_text_file({"sessionId": "s", "path": inside, "content": "changed\n"})
        assert Path(inside).read_text(encoding="utf-8") == "changed\n"

        outside = str(tmp_path / "outside.txt")
        Path(outside).write_text("nope\n", encoding="utf-8")
        with pytest.raises(RuntimeAdapterError) as excinfo:
            adapter._handle_read_text_file({"sessionId": "s", "path": outside})
        assert excinfo.value.code is RuntimeErrorCode.PERMISSION_DENIED
    finally:
        adapter.cleanup_change_workspace(change_workspace)


def test_permission_request_approves_scoped_edit_and_denies_execute(tmp_path: Path) -> None:
    from weftmark.application.ports.git import GitObjectId

    repo = _init_repo(tmp_path)
    head = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    adapter = _spawn_stub_adapter(tmp_path / "stub2")
    workspace = adapter.attach_workspace(str(repo))
    change_workspace = adapter.ensure_change_workspace(workspace, "chg-3", GitObjectId(value=head))
    try:
        inside = str(Path(change_workspace.worktree_path) / "README.md")
        allow_options = [
            {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
            {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
        ]
        approved = adapter._handle_request_permission(
            {
                "sessionId": "s",
                "toolCall": {"toolCallId": "t1", "kind": "edit", "locations": [{"path": inside}]},
                "options": allow_options,
            }
        )
        assert approved["outcome"] == {"outcome": "selected", "optionId": "allow"}

        denied = adapter._handle_request_permission(
            {
                "sessionId": "s",
                "toolCall": {"toolCallId": "t2", "kind": "execute", "locations": [{"path": inside}]},
                "options": allow_options,
            }
        )
        assert denied["outcome"] == {"outcome": "selected", "optionId": "reject"}
    finally:
        adapter.cleanup_change_workspace(change_workspace)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/adapters/test_acp.py -v -k "worktree or fs_read_write or permission_request"`
Expected: FAIL — `NotImplementedError` from `ensure_change_workspace` / `AttributeError` for the private handlers.

- [ ] **Step 3: Implement worktree lifecycle and fs/permission handlers**

Replace the four `NotImplementedError` stub methods and `_request_handlers` in `src/weftmark/adapters/acp.py`:

```python
    def ensure_change_workspace(
        self, workspace: RuntimeWorkspace, change_set_id: str, base: GitObjectId
    ) -> RuntimeChangeWorkspace:
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
        except subprocess.CalledProcessError as error:
            raise RuntimeAdapterError(
                RuntimeErrorCode.RUNTIME_FAILED,
                self._spec.name,
                "ensure_change_workspace",
                error.stderr or str(error),
            ) from error
        return RuntimeChangeWorkspace(
            provider=self._spec.name,
            workspace_id=workspace.workspace_id,
            change_set_id=change_set_id,
            task_id=change_set_id,
            base=base,
            worktree_path=worktree_path,
        )

    def get_change_workspace(
        self, workspace: RuntimeWorkspace, change_set_id: str, base: GitObjectId
    ) -> RuntimeChangeWorkspace | None:
        worktree_path = self._worktree_path(workspace, change_set_id)
        if not Path(worktree_path).is_dir():
            return None
        return RuntimeChangeWorkspace(
            provider=self._spec.name,
            workspace_id=workspace.workspace_id,
            change_set_id=change_set_id,
            task_id=change_set_id,
            base=base,
            worktree_path=worktree_path,
        )

    def changes(
        self,
        change_workspace: RuntimeChangeWorkspace,
        mode: RuntimeChangesMode = RuntimeChangesMode.WORKING_COPY,
    ) -> RuntimeChanges:
        if mode is not RuntimeChangesMode.WORKING_COPY:
            raise RuntimeContractError("only working_copy mode is supported in v0")
        result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=change_workspace.worktree_path,
            check=True,
            capture_output=True,
            text=True,
        )
        files = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            code, _, path = line.partition(" ")
            code = code.strip()
            kind = GitChangeKind.MODIFIED
            if code in {"??", "A"}:
                kind = GitChangeKind.ADDED
            elif code == "D":
                kind = GitChangeKind.DELETED
            files.append(RuntimeFileChange(path=path.strip(), kind=kind))
        return RuntimeChanges(
            provider=self._spec.name,
            change_set_id=change_workspace.change_set_id,
            mode=mode,
            files=tuple(files),
        )

    def cleanup_change_workspace(self, change_workspace: RuntimeChangeWorkspace) -> None:
        repo_path = self._repo_path_for(change_workspace)
        subprocess.run(
            ["git", "worktree", "remove", "--force", change_workspace.worktree_path],
            cwd=repo_path,
            check=False,
            capture_output=True,
            text=True,
        )

    def _worktree_path(self, workspace: RuntimeWorkspace, change_set_id: str) -> str:
        base = Path(workspace.repo_path).resolve()
        return str(base.parent / f".weftmark-runtime-{base.name}-{change_set_id}")

    def _repo_path_for(self, change_workspace: RuntimeChangeWorkspace) -> str:
        # v0: the disposable worktree's parent-of-parent layout mirrors _worktree_path,
        # but cleanup only needs *a* path inside the original repo's Git dir, which
        # `git worktree remove` resolves from the worktree itself.
        return change_workspace.worktree_path

    def _resolve_in_worktree(self, session: _SessionState, path: str) -> Path:
        root = Path(session.worktree_path).resolve()
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

    def _handle_read_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._session_by_id(params["sessionId"]) if params.get("sessionId") else None
        root = session.worktree_path if session is not None else params.get("__worktree_path", "")
        path = self._resolve_path_in(root, params["path"])
        return {"content": path.read_text(encoding="utf-8")}

    def _handle_write_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._session_by_id(params["sessionId"]) if params.get("sessionId") else None
        root = session.worktree_path if session is not None else params.get("__worktree_path", "")
        path = self._resolve_path_in(root, params["path"])
        path.write_text(params["content"], encoding="utf-8")
        return {}

    def _resolve_path_in(self, worktree_path: str, path: str) -> Path:
        root = Path(worktree_path).resolve()
        candidate = Path(path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise RuntimeAdapterError(
                RuntimeErrorCode.PERMISSION_DENIED, self._spec.name, "fs", f"path outside disposable worktree: {path}"
            ) from None
        return candidate

    def _handle_request_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_call = params["toolCall"]
        options = params["options"]
        worktree_path = self._current_worktree_hint
        scoped = tool_call.get("kind") in {"read", "edit"} and all(
            self._is_inside(worktree_path, location["path"]) for location in tool_call.get("locations", [])
        ) and bool(tool_call.get("locations"))
        allow = next((o for o in options if o["kind"] == "allow_once"), None)
        reject = next((o for o in options if o["kind"] in {"reject_once", "reject_always"}), None)
        if scoped and allow is not None:
            return {"outcome": {"outcome": "selected", "optionId": allow["optionId"]}}
        if reject is not None:
            return {"outcome": {"outcome": "selected", "optionId": reject["optionId"]}}
        return {"outcome": {"outcome": "cancelled"}}

    def _is_inside(self, worktree_path: str, path: str) -> bool:
        try:
            Path(path).resolve().relative_to(Path(worktree_path).resolve())
            return True
        except ValueError:
            return False

    def _session_by_id(self, session_id: str) -> _SessionState | None:
        for session in self._sessions.values():
            if session.session_id == session_id:
                return session
        return None
```

Then update `_request_handlers` and `start_worker`/tests-only helpers to thread the worktree path through: replace the body of `_request_handlers` with:

```python
    def _request_handlers(self, session: _SessionState) -> dict[str, RequestHandler]:
        self._current_worktree_hint = session.worktree_path

        def read(params: dict[str, Any]) -> dict[str, Any]:
            path = self._resolve_path_in(session.worktree_path, params["path"])
            return {"content": path.read_text(encoding="utf-8")}

        def write(params: dict[str, Any]) -> dict[str, Any]:
            path = self._resolve_path_in(session.worktree_path, params["path"])
            path.write_text(params["content"], encoding="utf-8")
            return {}

        def permission(params: dict[str, Any]) -> dict[str, Any]:
            self._current_worktree_hint = session.worktree_path
            return self._handle_request_permission(params)

        return {
            "fs/read_text_file": read,
            "fs/write_text_file": write,
            "session/request_permission": permission,
        }
```

Remove the now-unused `_handle_read_text_file`/`_handle_write_text_file` top-level forms if the test file calls the underscore methods directly — **keep** `_handle_read_text_file`, `_handle_write_text_file`, and `_handle_request_permission` as thin adapter-level methods (used directly by the Step 1 tests via `adapter._handle_read_text_file(...)`), each delegating to the same `_resolve_path_in`/`_handle_request_permission` logic but resolving the worktree path via `self._current_worktree_hint` (set whenever `_request_handlers`/`ensure_change_workspace` runs) instead of a session lookup:

```python
    def _handle_read_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path_in(self._current_worktree_hint, params["path"])
        return {"content": path.read_text(encoding="utf-8")}

    def _handle_write_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path_in(self._current_worktree_hint, params["path"])
        path.write_text(params["content"], encoding="utf-8")
        return {}
```

Add `self._current_worktree_hint: str = ""` to `__init__`, and set it in `ensure_change_workspace`/`get_change_workspace` right before returning (`self._current_worktree_hint = worktree_path`), so the Step 1 tests (which call `adapter._handle_read_text_file(...)` directly, without an active session) resolve against the worktree that was just ensured.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/adapters/test_acp.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests PASS.

- [ ] **Step 6: Write `docs/contracts/acp-runtime-adapter-v0.md`**

```markdown
# ACP runtime adapter (v0)

`src/weftmark/adapters/acp.py` implements `RuntimePort` by spawning an
ACP-speaking coding agent as a subprocess and driving it over newline-
delimited JSON-RPC 2.0, per the published protocol
(agentclientprotocol.com/protocol/v1). Only the slice of ACP needed to
start/stop/feed a disposable worker is implemented: `initialize`,
`session/new`, `session/prompt`, `session/cancel`, `session/update`, plus
the client-side `fs/read_text_file`, `fs/write_text_file`, and
`session/request_permission` callbacks the agent may invoke.

## Worktree ownership

Each Change Set gets one disposable `git worktree` (`git worktree add
--detach <path> <base>`), created under the parent of the primary
repository directory. `GitPort` stays read-only; this adapter owns worktree
creation and removal directly, matching `RuntimePort`'s own docstring
("Implementations may own disposable worktrees, PTYs and agent
processes").

## Permission policy (v0)

- `fs/read_text_file`/`fs/write_text_file` are served only for paths that
  resolve inside the active Change Set's disposable worktree; anything
  else raises `RuntimeErrorCode.PERMISSION_DENIED`.
- `session/request_permission`: an option is auto-approved only when the
  tool call's `kind` is `read` or `edit` **and** every declared
  `toolCall.locations[].path` resolves inside the worktree, by selecting
  that option's `allow_once` choice. Everything else (`execute`, `delete`,
  `move`, unscoped/no-location tool calls, or tool calls with no matching
  `allow_once` option) is refused by selecting a `reject_once`/
  `reject_always` option, or by returning outcome `cancelled` if neither is
  offered.
- `allow_always`/`reject_always` are never auto-selected — a persistent
  grant is a policy decision the adapter does not make on the operator's
  behalf in v0.

## Worker state model

`RuntimeWorkerState` is a coarse v0 approximation: any `session/update`
notification (message chunk, thought chunk, tool call, tool call update,
or plan) moves the session to `AWAITING_INPUT`. This is deliberately
imprecise — it does not yet distinguish "the agent is actively computing"
from "the agent is waiting on the next `session/prompt`" — future work can
tighten this using `stopReason` and tool-call `status` transitions.

## Known limitations (v0)

- One worker session per Change Set; no concurrent multi-session support.
- No terminal/PTY (`terminal/*` ACP methods) support — `clientCapabilities.
  terminal` is always `false`.
- No `authenticate` method support — providers requiring ACP auth are not
  usable in v0.
- Diff presentation (`changes()`) shells out to `git status --porcelain`
  in the worktree; it does not yet compute additions/deletions per file.
```

- [ ] **Step 7: Commit**

```bash
git add src/weftmark/adapters/acp.py tests/adapters/test_acp.py docs/contracts/acp-runtime-adapter-v0.md
git commit -m "feat: own disposable worktrees and scope fs/permission requests in the ACP adapter"
```

---

## Task 4: Runtime provider registry

**Files:**
- Create: `src/weftmark/application/runtime_registry.py`
- Test: `tests/application/test_runtime_registry.py`
- Create: `docs/contracts/runtime-provider-registry-v0.md`

**Interfaces:**
- Consumes: nothing from Task 1-3 (spec decision 5: protocol-agnostic, no import of `adapters/acp.py`).
- Produces:
  - `class RuntimeRegistryError(ValueError)`
  - `@dataclass(frozen=True, slots=True) class RuntimeProviderConfig: name: str; argv: tuple[str, ...]; capabilities: frozenset[str] = frozenset()`
  - `class RuntimeProviderRegistry: def __init__(self, providers: Mapping[str, RuntimeProviderConfig]) -> None`, `def get(self, name: str) -> RuntimeProviderConfig`, `def names(self) -> tuple[str, ...]`
  - `def parse_runtime_provider_flag(value: str) -> RuntimeProviderConfig` — parses one `--runtime-provider` CLI flag of the form `name=argv0:argv1:...[:cap=read,edit]`, e.g. `codex-acp=codex:acp` or `codex-acp=codex:acp:cap=read,edit`.
  - `def load_runtime_registry(*, config_path: str | None = None, cli_flags: Sequence[str] = ()) -> RuntimeProviderRegistry` — merges a JSON config file (`{"providers": {"<name>": {"argv": [...], "capabilities": [...]}}}`) with `--runtime-provider` flags; flags win on name collision.

- [ ] **Step 1: Write the failing tests**

Create `tests/application/test_runtime_registry.py`:

```python
"""Tests for the config-driven runtime provider registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weftmark.application.runtime_registry import (
    RuntimeProviderConfig,
    RuntimeProviderRegistry,
    RuntimeRegistryError,
    load_runtime_registry,
    parse_runtime_provider_flag,
)


def test_registry_get_returns_configured_provider() -> None:
    registry = RuntimeProviderRegistry(
        {
            "codex-acp": RuntimeProviderConfig(name="codex-acp", argv=("codex", "acp")),
            "claude-acp": RuntimeProviderConfig(name="claude-acp", argv=("claude", "--acp")),
        }
    )
    assert registry.get("codex-acp").argv == ("codex", "acp")
    assert set(registry.names()) == {"codex-acp", "claude-acp"}


def test_registry_get_unknown_provider_fails_closed() -> None:
    registry = RuntimeProviderRegistry({})
    with pytest.raises(RuntimeRegistryError, match="unknown runtime provider"):
        registry.get("missing")


def test_parse_runtime_provider_flag_without_capabilities() -> None:
    config = parse_runtime_provider_flag("codex-acp=codex:acp")
    assert config == RuntimeProviderConfig(name="codex-acp", argv=("codex", "acp"), capabilities=frozenset())


def test_parse_runtime_provider_flag_with_capabilities() -> None:
    config = parse_runtime_provider_flag("codex-acp=codex:acp:cap=read,edit")
    assert config.argv == ("codex", "acp")
    assert config.capabilities == frozenset({"read", "edit"})


def test_parse_runtime_provider_flag_rejects_malformed_input() -> None:
    with pytest.raises(RuntimeRegistryError, match="--runtime-provider"):
        parse_runtime_provider_flag("no-equals-sign")


def test_load_runtime_registry_merges_file_and_flags(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps({"providers": {"codex-acp": {"argv": ["codex", "acp"], "capabilities": ["read"]}}}),
        encoding="utf-8",
    )
    registry = load_runtime_registry(
        config_path=str(config_path), cli_flags=["claude-acp=claude:--acp"]
    )
    assert set(registry.names()) == {"codex-acp", "claude-acp"}
    assert registry.get("codex-acp").capabilities == frozenset({"read"})


def test_load_runtime_registry_flag_overrides_file(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps({"providers": {"codex-acp": {"argv": ["codex", "acp"]}}}), encoding="utf-8"
    )
    registry = load_runtime_registry(
        config_path=str(config_path), cli_flags=["codex-acp=codex:acp:cap=execute"]
    )
    assert registry.get("codex-acp").capabilities == frozenset({"execute"})


def test_load_runtime_registry_rejects_missing_config_file() -> None:
    with pytest.raises(RuntimeRegistryError, match="runtime config"):
        load_runtime_registry(config_path="/nonexistent/runtime.json")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/application/test_runtime_registry.py -v`
Expected: `ModuleNotFoundError: No module named 'weftmark.application.runtime_registry'`.

- [ ] **Step 3: Implement the registry**

Create `src/weftmark/application/runtime_registry.py`:

```python
"""Config-driven lookup of named, disposable RuntimePort providers.

Deliberately independent of any concrete RuntimePort implementation (no
import of weftmark.adapters.acp): this registry only resolves a provider
*name* to a launch configuration. Tests exercise it against the existing
FakeRuntime double, not a real adapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class RuntimeRegistryError(ValueError):
    """Raised when runtime provider configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeProviderConfig:
    name: str
    argv: tuple[str, ...]
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise RuntimeRegistryError("provider name must not be empty")
        if not self.argv:
            raise RuntimeRegistryError(f"provider {self.name!r} has an empty argv")


class RuntimeProviderRegistry:
    def __init__(self, providers: Mapping[str, RuntimeProviderConfig]) -> None:
        self._providers = dict(providers)

    def get(self, name: str) -> RuntimeProviderConfig:
        try:
            return self._providers[name]
        except KeyError:
            raise RuntimeRegistryError(f"unknown runtime provider: {name}") from None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


def parse_runtime_provider_flag(value: str) -> RuntimeProviderConfig:
    name, sep, rest = value.partition("=")
    if not sep or not name.strip() or not rest.strip():
        raise RuntimeRegistryError(
            f"--runtime-provider must look like name=argv0:argv1[:cap=read,edit], got: {value!r}"
        )
    parts = rest.split(":")
    capabilities: frozenset[str] = frozenset()
    argv_parts = []
    for part in parts:
        if part.startswith("cap="):
            capabilities = frozenset(item for item in part[len("cap=") :].split(",") if item)
        else:
            argv_parts.append(part)
    if not argv_parts:
        raise RuntimeRegistryError(f"--runtime-provider {value!r} has no argv")
    return RuntimeProviderConfig(name=name.strip(), argv=tuple(argv_parts), capabilities=capabilities)


def load_runtime_registry(
    *, config_path: str | None = None, cli_flags: Sequence[str] = ()
) -> RuntimeProviderRegistry:
    providers: dict[str, RuntimeProviderConfig] = {}
    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise RuntimeRegistryError(f"runtime config file not found: {config_path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeRegistryError(f"runtime config is not valid JSON: {error}") from error
        for name, entry in payload.get("providers", {}).items():
            argv = entry.get("argv")
            if not isinstance(argv, list) or not argv:
                raise RuntimeRegistryError(f"runtime config provider {name!r} has an invalid argv")
            capabilities = frozenset(entry.get("capabilities", []))
            providers[name] = RuntimeProviderConfig(name=name, argv=tuple(argv), capabilities=capabilities)
    for flag in cli_flags:
        config = parse_runtime_provider_flag(flag)
        providers[config.name] = config
    return RuntimeProviderRegistry(providers)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/application/test_runtime_registry.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Write `docs/contracts/runtime-provider-registry-v0.md`**

```markdown
# Runtime provider registry (v0)

`src/weftmark/application/runtime_registry.py` maps a provider *name*
(e.g. `codex-acp`) to a launch `argv` and declared capabilities, sourced
from a `--runtime-config <path>` JSON file, repeated `--runtime-provider
name=argv0:argv1[:cap=a,b]` CLI flags, or both (flags win on a name
collision).

The registry never imports a concrete `RuntimePort` implementation — it is
pure configuration lookup, independently testable against the existing
`FakeRuntime` double. No plugin or network-discovery mechanism exists in
v0; adding a provider means adding a config entry or CLI flag.

## Config file shape

```json
{
  "providers": {
    "codex-acp": {"argv": ["codex", "acp"], "capabilities": ["read", "edit"]},
    "claude-acp": {"argv": ["claude", "--acp"], "capabilities": ["read", "edit"]}
  }
}
```

## Negative behavior

An unconfigured provider name fails closed (`RuntimeRegistryError`) rather
than silently defaulting to any other provider.
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/weftmark/application/runtime_registry.py tests/application/test_runtime_registry.py docs/contracts/runtime-provider-registry-v0.md
git commit -m "feat: add config-driven runtime provider registry"
```

---

## Task 5: Runtime worker service and CLI surface

**Files:**
- Create: `src/weftmark/application/runtime_workers.py`
- Modify: `src/weftmark/cli/main.py`
- Test: `tests/cli/test_cli_runtime.py`

**Interfaces:**
- Consumes: `RuntimeProviderRegistry`/`load_runtime_registry` (Task 4); `AcpRuntimeAdapter`/`AcpProviderSpec` (Tasks 1-3); `TaskClaimService`/`TaskClaimService.get` (existing, `application/task_claims.py`); `LedgerService.record_if_head`/`latest`/`snapshot` (existing, `application/ledger.py`); `RuntimePort`, `RuntimeChangeWorkspace`, `RuntimeWorkerSummary`, `RuntimeWorkerState`, `RuntimeAdapterError` (`application/ports/runtime.py`).
- Produces:
  - `class RuntimeWorkerError(ValueError)`
  - `@dataclass(frozen=True, slots=True) class RuntimeWorkerRecord: task_id, change_set_id, provider, session_started_at, updated_at, state (str)`
  - `class RuntimeWorkerService: def __init__(self, task_claims: TaskClaimService, registry: RuntimeProviderRegistry, adapter_factory: Callable[[str], RuntimePort], repo_path: str, ledger: LedgerService) -> None`, `def start(self, task_id: str, *, provider: str, prompt: str, started_at: datetime) -> RuntimeWorkerRecord`, `def send_input(self, task_id: str, data: str, *, observed_at: datetime) -> RuntimeWorkerRecord`, `def status(self, task_id: str) -> RuntimeWorkerRecord`, `def stop(self, task_id: str, *, observed_at: datetime) -> RuntimeWorkerRecord`
  - CLI: `weftmark runtime start <task-id> --provider NAME --prompt TEXT [--runtime-config PATH] [--runtime-provider NAME=ARGV...]*`, `weftmark runtime status <task-id>`, `weftmark runtime send-input <task-id> --data TEXT`, `weftmark runtime stop <task-id>`

`adapter_factory` keeps `RuntimeWorkerService` decoupled from the concrete ACP adapter (mirrors `RuntimeProviderRegistry`'s own decoupling) — the CLI wiring in `main.py` is the only place that imports `AcpRuntimeAdapter` and builds the factory as `lambda name: AcpRuntimeAdapter(registry.get(name))`.

- [ ] **Step 1: Read the existing CLI wiring pattern before writing the test**

Read `src/weftmark/cli/main.py` around the `task claim` command (search for `task_claim = task_commands.add_parser` and `if args.command == "task" and args.task_command == "claim":`) to match its exact style: `_now()` helper, `_emit_native_task_claim`, the trailing tuple of caught exception types near the bottom of `main()`. Every new CLI piece below must follow that same shape — same `_now()`, same JSON/human dual-output convention (see `_emit_native_task_claim` for the pattern to copy for `_emit_runtime_worker`), same placement of the new exception type in the big `except (...)` tuple.

- [ ] **Step 2: Write the failing CLI test**

Create `tests/cli/test_cli_runtime.py`:

```python
"""End-to-end CLI tests for `weftmark runtime`, using a fake in-process provider."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from weftmark.cli.main import main


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repo), *args), check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(repo_path, "init", "--initial-branch=main")
    _git(repo_path, "config", "user.name", "WeftMark Tests")
    _git(repo_path, "config", "user.email", "weftmark@example.invalid")
    _git(repo_path, "commit", "--allow-empty", "-m", "base")
    return repo_path


def _command(repo: Path, *args: str) -> list[str]:
    # Matches tests/cli/test_cli_task_claims.py's own `command()` helper exactly —
    # `--json` goes right after `--repo`, before the subcommand.
    return ["--repo", str(repo), "--json", *args]


def _run_capture(capsys: pytest.CaptureFixture[str], repo: Path, *args: str) -> tuple[int, dict]:
    exit_code = main(_command(repo, *args))
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    return exit_code, payload


_ECHO_AGENT_SCRIPT = """
import json
import sys

def _write(m):
    sys.stdout.write(json.dumps(m) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    m = json.loads(line)
    if "id" not in m:
        continue
    method = m["method"]
    if method == "initialize":
        _write({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}})
    elif method == "session/new":
        _write({"jsonrpc": "2.0", "id": m["id"], "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        _write({"jsonrpc": "2.0", "id": m["id"], "result": {"stopReason": "end_turn"}})
"""


def test_runtime_start_status_stop_round_trip(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    script = tmp_path / "echo_agent.py"
    script.write_text(_ECHO_AGENT_SCRIPT, encoding="utf-8")
    provider_flag = f"echo-acp={sys.executable}:{script}"

    exit_code = main(
        _command(
            repo, "task", "create", "task-1", "--title", "Do a thing",
            "--why", "test the runtime CLI", "--what", "start a worker",
            "--priority", "p1", "--scope", "file:README.md",
        )
    )
    assert exit_code == 0
    exit_code = main(
        _command(
            repo, "task", "claim", "task-1",
            "--changeset-id", "chg-1", "--claim-id", "claim-1",
        )
    )
    assert exit_code == 0

    exit_code, payload = _run_capture(
        capsys, repo, "runtime", "start", "task-1",
        "--provider", "echo-acp", "--prompt", "do it", "--runtime-provider", provider_flag,
    )
    assert exit_code == 0
    assert payload["runtime_worker"]["task_id"] == "task-1"
    assert payload["runtime_worker"]["provider"] == "echo-acp"

    exit_code, payload = _run_capture(
        capsys, repo, "runtime", "status", "task-1", "--runtime-provider", provider_flag,
    )
    assert exit_code == 0
    assert payload["runtime_worker"]["state"] in {"running", "awaiting_input"}

    exit_code, payload = _run_capture(
        capsys, repo, "runtime", "stop", "task-1", "--runtime-provider", provider_flag,
    )
    assert exit_code == 0
    assert payload["runtime_worker"]["state"] == "exited"


def test_runtime_start_without_active_claim_is_refused(repo: Path) -> None:
    exit_code = main(
        _command(
            repo, "task", "create", "task-2", "--title", "Untaken",
            "--why", "test the refusal path", "--what", "leave unclaimed",
            "--priority", "p1", "--scope", "file:README.md",
        )
    )
    assert exit_code == 0
    exit_code = main(
        _command(
            repo, "runtime", "start", "task-2",
            "--provider", "echo-acp", "--prompt", "do it",
            "--runtime-provider", "echo-acp=python3:-c:pass",
        )
    )
    assert exit_code == 2
```

The `task create`/`task claim` flags above (`--why`, `--what`, `--priority`, `--scope`, `--changeset-id`, `--claim-id`) and the `repo`/`_command` fixtures were verified directly against `tests/cli/test_cli_task_claims.py`'s own `repository()`/`command()` helpers — if a later change to that file has moved the surface again, re-verify before trusting this plan's copy.

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/cli/test_cli_runtime.py -v`
Expected: FAIL — `runtime` is not a recognized command (argparse error) or `ModuleNotFoundError` for `runtime_workers`.

- [ ] **Step 4: Implement `RuntimeWorkerService`**

Create `src/weftmark/application/runtime_workers.py`:

```python
"""Ledger-recorded, idempotent worker sessions over a RuntimePort provider."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from weftmark.application.ledger import LedgerService
from weftmark.application.ports.git import GitObjectId
from weftmark.application.ports.runtime import (
    RuntimeAdapterError,
    RuntimeChangeWorkspace,
    RuntimePort,
    RuntimeWorkerState,
)
from weftmark.application.runtime_registry import RuntimeProviderRegistry, RuntimeRegistryError
from weftmark.application.task_claims import TaskClaimService, TaskClaimError


class RuntimeWorkerError(ValueError):
    """Raised when a runtime worker session cannot be started, driven or stopped."""


@dataclass(frozen=True, slots=True)
class RuntimeWorkerRecord:
    task_id: str
    change_set_id: str
    provider: str
    state: str
    updated_at: datetime


class RuntimeWorkerService:
    def __init__(
        self,
        task_claims: TaskClaimService,
        registry: RuntimeProviderRegistry,
        adapter_factory: Callable[[str], RuntimePort],
        repo_path: str,
        ledger: LedgerService,
    ) -> None:
        self._task_claims = task_claims
        self._registry = registry
        self._adapter_factory = adapter_factory
        self._repo_path = repo_path
        self._ledger = ledger

    def start(self, task_id: str, *, provider: str, prompt: str, started_at: datetime) -> RuntimeWorkerRecord:
        binding = self._require_active_claim(task_id)
        try:
            self._registry.get(provider)
        except RuntimeRegistryError as error:
            raise RuntimeWorkerError(str(error)) from error
        adapter = self._adapter_factory(provider)
        workspace = adapter.attach_workspace(self._repo_path)
        change_workspace = adapter.ensure_change_workspace(
            workspace, binding.change_set_id, GitObjectId(value=binding.base_revision)
            if len(binding.base_revision) == 40
            else self._resolve_base(binding.base_revision),
        )
        try:
            summary = adapter.start_worker(change_workspace, binding.agent_id, prompt)
        except RuntimeAdapterError as error:
            raise RuntimeWorkerError(f"{error.code.value}: {error.detail}") from error
        return self._record(task_id, binding.change_set_id, provider, summary.state, started_at)

    def send_input(self, task_id: str, data: str, *, observed_at: datetime) -> RuntimeWorkerRecord:
        binding, provider = self._require_started(task_id)
        adapter = self._adapter_factory(provider)
        workspace = adapter.attach_workspace(self._repo_path)
        change_workspace = self._existing_workspace(adapter, workspace, binding)
        try:
            summary = adapter.send_worker_input(change_workspace, data)
        except RuntimeAdapterError as error:
            raise RuntimeWorkerError(f"{error.code.value}: {error.detail}") from error
        return self._record(task_id, binding.change_set_id, provider, summary.state, observed_at)

    def status(self, task_id: str) -> RuntimeWorkerRecord:
        binding, provider = self._require_started(task_id)
        adapter = self._adapter_factory(provider)
        workspace = adapter.attach_workspace(self._repo_path)
        change_workspace = self._existing_workspace(adapter, workspace, binding)
        summary = adapter.worker_summary(change_workspace)
        return self._record(task_id, binding.change_set_id, provider, summary.state, summary.updated_at or summary.started_at)

    def stop(self, task_id: str, *, observed_at: datetime) -> RuntimeWorkerRecord:
        binding, provider = self._require_started(task_id)
        adapter = self._adapter_factory(provider)
        workspace = adapter.attach_workspace(self._repo_path)
        change_workspace = self._existing_workspace(adapter, workspace, binding)
        try:
            summary = adapter.stop_worker(change_workspace)
        except RuntimeAdapterError as error:
            raise RuntimeWorkerError(f"{error.code.value}: {error.detail}") from error
        return self._record(task_id, binding.change_set_id, provider, summary.state, observed_at)

    # -- internals --

    def _require_active_claim(self, task_id: str):
        binding = self._task_claims.get(task_id)
        if binding is None:
            raise RuntimeWorkerError(f"no active native claim for task: {task_id}")
        return binding

    def _require_started(self, task_id: str) -> tuple[Any, str]:
        binding = self._require_active_claim(task_id)
        entry = self._ledger.latest(kind="runtime_worker_session", entity_id=task_id)
        if entry is None:
            raise RuntimeWorkerError(f"no runtime worker session recorded for task: {task_id}")
        return binding, str(entry.payload["provider"])

    def _existing_workspace(self, adapter: RuntimePort, workspace, binding) -> RuntimeChangeWorkspace:
        change_workspace = adapter.get_change_workspace(
            workspace, binding.change_set_id, self._resolve_base(binding.base_revision)
        )
        if change_workspace is None:
            raise RuntimeWorkerError(f"no disposable worktree found for change set: {binding.change_set_id}")
        return change_workspace

    def _resolve_base(self, base_revision: str) -> GitObjectId:
        if len(base_revision) == 40:
            return GitObjectId(value=base_revision)
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", base_revision], cwd=self._repo_path, check=True, capture_output=True, text=True
        )
        return GitObjectId(value=result.stdout.strip())

    def _record(
        self, task_id: str, change_set_id: str, provider: str, state: RuntimeWorkerState, at: datetime
    ) -> RuntimeWorkerRecord:
        entries = self._ledger.snapshot()
        from weftmark.application.ports.ledger import LEDGER_GENESIS_DIGEST

        expected = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
        payload = {
            "schema_version": 1,
            "task_id": task_id,
            "change_set_id": change_set_id,
            "provider": provider,
            "state": state.value,
        }
        try:
            self._ledger.record_if_head(
                kind="runtime_worker_session", entity_id=task_id, payload=payload, recorded_at=at, expected_digest=expected
            )
        except Exception:
            pass  # a concurrent recorder already captured an equal-or-later observation
        return RuntimeWorkerRecord(
            task_id=task_id, change_set_id=change_set_id, provider=provider, state=state.value, updated_at=at
        )


def runtime_worker_record_to_payload(record: RuntimeWorkerRecord) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "change_set_id": record.change_set_id,
        "provider": record.provider,
        "state": record.state,
        "updated_at": record.updated_at.isoformat(),
    }
```

Read `src/weftmark/application/task_claims.py`'s `TaskWorkBinding` dataclass fields before wiring `_require_active_claim`/`_resolve_base` above — confirm the exact attribute names (`change_set_id`, `base_revision`, `agent_id`) match; adjust the code above if any name differs from what Task 2/3's earlier reading of `task_claims.py` established.

- [ ] **Step 5: Wire the CLI**

In `src/weftmark/cli/main.py`:

1. Add imports near the other `weftmark.application.*` imports:

```python
from weftmark.adapters.acp import AcpProviderSpec, AcpRuntimeAdapter
from weftmark.application.runtime_registry import (
    RuntimeRegistryError,
    load_runtime_registry,
)
from weftmark.application.runtime_workers import (
    RuntimeWorkerError,
    RuntimeWorkerService,
    runtime_worker_record_to_payload,
)
```

2. In `build_parser()`, alongside the other top-level `commands.add_parser(...)` calls, add:

```python
    runtime = commands.add_parser("runtime", help="drive disposable coding-agent workers")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)

    def _add_runtime_provider_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--runtime-config", help="path to a JSON runtime provider config file")
        p.add_argument(
            "--runtime-provider",
            action="append",
            default=[],
            metavar="NAME=ARGV0:ARGV1[:cap=a,b]",
            help="define/override one named runtime provider (repeatable)",
        )

    runtime_start = runtime_commands.add_parser("start", help="start a worker for an owned native task")
    runtime_start.add_argument("id", help="native task ID")
    runtime_start.add_argument("--provider", required=True)
    runtime_start.add_argument("--prompt", required=True)
    _add_runtime_provider_args(runtime_start)

    runtime_status = runtime_commands.add_parser("status", help="show a worker's last observed state")
    runtime_status.add_argument("id", help="native task ID")
    _add_runtime_provider_args(runtime_status)

    runtime_send_input = runtime_commands.add_parser("send-input", help="send input to a running worker")
    runtime_send_input.add_argument("id", help="native task ID")
    runtime_send_input.add_argument("--data", required=True)
    _add_runtime_provider_args(runtime_send_input)

    runtime_stop = runtime_commands.add_parser("stop", help="stop a worker")
    runtime_stop.add_argument("id", help="native task ID")
    _add_runtime_provider_args(runtime_stop)
```

3. In `main()`, after `task_claims = TaskClaimService(...)` is constructed, add:

```python
        def _runtime_worker_service(args: argparse.Namespace) -> RuntimeWorkerService:
            registry = load_runtime_registry(
                config_path=getattr(args, "runtime_config", None),
                cli_flags=getattr(args, "runtime_provider", []),
            )

            def _adapter_factory(provider_name: str) -> AcpRuntimeAdapter:
                return AcpRuntimeAdapter(AcpProviderSpec(name=provider_name, argv=registry.get(provider_name).argv))

            return RuntimeWorkerService(task_claims, registry, _adapter_factory, args.repo, ledger)
```

   (Match `args.repo`/`ledger`/`task_claims` to whatever the surrounding `main()` already names them — read the existing `task_claims = TaskClaimService(...)` block first, since exact local variable names must match what's already in scope.)

4. Still in `main()`, alongside the other `if args.command == "task" and ...` blocks, add:

```python
        if args.command == "runtime" and args.runtime_command == "start":
            service = _runtime_worker_service(args)
            record = service.start(args.id, provider=args.provider, prompt=args.prompt, started_at=_now())
            _emit_runtime_worker(runtime_worker_record_to_payload(record), json_output=args.json)
            return 0
        if args.command == "runtime" and args.runtime_command == "status":
            service = _runtime_worker_service(args)
            record = service.status(args.id)
            _emit_runtime_worker(runtime_worker_record_to_payload(record), json_output=args.json)
            return 0
        if args.command == "runtime" and args.runtime_command == "send-input":
            service = _runtime_worker_service(args)
            record = service.send_input(args.id, args.data, observed_at=_now())
            _emit_runtime_worker(runtime_worker_record_to_payload(record), json_output=args.json)
            return 0
        if args.command == "runtime" and args.runtime_command == "stop":
            service = _runtime_worker_service(args)
            record = service.stop(args.id, observed_at=_now())
            _emit_runtime_worker(runtime_worker_record_to_payload(record), json_output=args.json)
            return 0
```

5. Add `RuntimeWorkerError` and `RuntimeRegistryError` to the big exception tuple near the bottom of `main()` (the one that already lists `TaskClaimError`, `TaskPlanningError`, etc.) so runtime failures produce the same clean CLI error path (exit code 2) as every other application error, instead of an uncaught traceback:

```python
        RuntimeWorkerError,
        RuntimeRegistryError,
```

6. Add the emitter next to `_emit_native_task_claim`:

```python
def _emit_runtime_worker(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "runtime_worker": payload}, sort_keys=True))
        return
    print(f"{payload['task_id']}  {payload['provider']}  {payload['state']}")
```

- [ ] **Step 6: Run the CLI test to verify it passes**

Run: `python -m pytest tests/cli/test_cli_runtime.py -v`
Expected: all tests PASS. If `test_runtime_start_status_stop_round_trip` fails on the `task create`/`task claim` argument shapes, fix the test's arguments to match `tests/cli/test_cli_task_claims.py`'s actual usage (read it first) rather than guessing further.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -q`
Expected: all tests PASS, no regressions elsewhere in `cli/main.py`.

- [ ] **Step 8: Commit**

```bash
git add src/weftmark/application/runtime_workers.py src/weftmark/cli/main.py tests/cli/test_cli_runtime.py
git commit -m "feat: add weftmark runtime start/status/send-input/stop CLI"
```

---

## Task 6: Close out the task-plan slices

**Files:**
- Modify: `tasks/57-runtime-providers.weft.yml`

- [ ] **Step 1: Flip status for the three implemented slices**

For `acp-runtime-adapter`, `runtime-provider-registry`, and `runtime-cli-surface` in `tasks/57-runtime-providers.weft.yml`, change `status: todo` to `status: review` (matching this repo's convention: an implementation commit lands with the slice at `review`, and a later `plan: close ... slice` commit moves it to `done` once the required evidence commands have actually been run and the security-review criterion has been considered — see `docs/superpowers/specs/2026-08-23-acp-runtime-adapter-design.md`'s referenced convention and e.g. commit `cb91db8`/`a565cc9` for the pattern).

- [ ] **Step 2: Validate**

Run: `python scripts/validate_tasks.py`
Expected: `validated N tasks across M files` with no error.

- [ ] **Step 3: Run the evidence commands named in each task entry**

Run each `evidence.command` listed for the three slices, e.g.:
```bash
python -m pytest tests/adapters/test_acp.py tests/contracts/test_runtime_port.py
python -m pytest tests/application/test_runtime_registry.py
python -m pytest tests/cli/test_cli_runtime.py
python -m pytest -q
```
Expected: all PASS. This is the `kind: test` evidence each task entry requires before it can move past `review`.

- [ ] **Step 4: Commit**

```bash
git add tasks/57-runtime-providers.weft.yml
git commit -m "plan: move runtime-providers slices to review"
```

- [ ] **Step 5: Push**

```bash
git fetch origin --prune
git rev-list --count HEAD..origin/main   # if nonzero, rebase onto origin/main and rerun the full suite before pushing
git push origin main
```

---

## Self-review notes (already applied above)

- **Spec coverage:** all 6 spec-doc decisions are reflected — decision 1 (Task 1, no new dependency), decision 2 (Task 3, worktree in the adapter not `GitPort`), decision 3 (Task 3, permission policy + `docs/contracts/acp-runtime-adapter-v0.md`), decision 4 (Task 2, background reader thread + non-blocking `start_worker`/`send_worker_input`), decision 5 (Task 4, registry never imports the ACP adapter), decision 6 (Task 5, CLI-only). The four task-plan slices from the design doc map 1:1 onto Tasks 1-3 (`acp-runtime-adapter`), Task 4 (`runtime-provider-registry`), and Task 5 (`runtime-cli-surface`); `runtime-port-contract` needed no code task since it was already shipped and its task-plan entry was already backfilled in the prior session.
- **Type consistency check:** `RuntimeWorkerService.__init__`'s `adapter_factory: Callable[[str], RuntimePort]` matches the `_adapter_factory` closure built in Task 5 Step 5; `RuntimeWorkerRecord` field names (`task_id`, `change_set_id`, `provider`, `state`, `updated_at`) match `runtime_worker_record_to_payload` and the CLI test's payload assertions (`payload["runtime_worker"]["task_id"]`, `["provider"]`, `["state"]`). `AcpProviderSpec(name=..., argv=...)` construction is identical between Task 2's definition and Task 5's CLI wiring.
- **Verified against existing code, not guessed:** `TaskWorkBinding`'s field names (`task_id`, `change_set_id`, `claim_id`, `agent_id`, `session_id`, `base_revision`, `created_at`, `completed` — `src/weftmark/application/task_claims.py`) and `tests/cli/test_cli_task_claims.py`'s actual CLI flags (`--why`/`--what` required on `task create`; `--json` placed right after `--repo`, before the subcommand) were both read directly and Task 5's test/service code above already matches them — this was corrected during plan self-review, not left as an open risk. If either surface has drifted further by execution time, re-verify before trusting this plan's copy.
