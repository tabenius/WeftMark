"""Tests for the hand-rolled ACP stdio JSON-RPC connection and runtime adapter."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from weftmark.adapters.acp import (
    AcpConnection,
    AcpProviderSpec,
    AcpRuntimeAdapter,
    AcpRuntimeProxy,
)
from weftmark.application.ports.git import GitObjectId
from weftmark.application.ports.runtime import (
    RuntimeAdapterError,
    RuntimeChangeWorkspace,
    RuntimeErrorCode,
    RuntimeWorkerState,
)

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


_STUB_AGENT = """
import json
import sys
import time

def write(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}
    elif method == "session/new":
        result = {"sessionId": "sess-1"}
    elif method == "session/prompt":
        write({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "sess-1", "update": {"sessionUpdate": "agent_message_chunk"}}})
        time.sleep(0.05)
        result = {"stopReason": "end_turn"}
    else:
        result = {}
    write({"jsonrpc": "2.0", "id": message["id"], "result": result})
"""


def _spawn_stub_adapter(tmp_path: Path) -> AcpRuntimeAdapter:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "stub_agent.py"
    script.write_text(textwrap.dedent(_STUB_AGENT), encoding="utf-8")
    return AcpRuntimeAdapter(
        AcpProviderSpec("stub-acp", (sys.executable, str(script)))
    )


def _runtime_workspace(tmp_path: Path) -> RuntimeChangeWorkspace:
    return RuntimeChangeWorkspace(
        provider="stub-acp",
        workspace_id="ws-1",
        change_set_id="chg-1",
        task_id="task-1",
        base=GitObjectId("0" * 40),
        worktree_path=str(tmp_path),
    )


def test_start_worker_is_non_blocking_and_reaches_awaiting_input(tmp_path: Path) -> None:
    adapter = _spawn_stub_adapter(tmp_path / "stub")
    workspace = _runtime_workspace(tmp_path)
    started = time.monotonic()
    try:
        summary = adapter.start_worker(workspace, "agent-1", "do the thing")
        assert time.monotonic() - started < 0.5
        assert summary.state is RuntimeWorkerState.RUNNING
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            summary = adapter.worker_summary(workspace)
            if summary.state is RuntimeWorkerState.AWAITING_INPUT:
                break
            time.sleep(0.01)
        assert summary.state is RuntimeWorkerState.AWAITING_INPUT
        assert summary.agent_id == "agent-1"
        assert summary.session_id == "sess-1"
    finally:
        adapter.stop_worker(workspace)


def test_stop_worker_marks_exited(tmp_path: Path) -> None:
    adapter = _spawn_stub_adapter(tmp_path / "stub")
    workspace = _runtime_workspace(tmp_path)
    adapter.start_worker(workspace, "agent-1", "do the thing")
    assert adapter.stop_worker(workspace).state is RuntimeWorkerState.EXITED


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head


def test_worktree_lifecycle_changes_and_scoped_fs(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    adapter = _spawn_stub_adapter(tmp_path / "stub")
    workspace = adapter.attach_workspace(str(repo))
    change = adapter.ensure_change_workspace(workspace, "chg-2", GitObjectId(head))
    try:
        assert change == adapter.ensure_change_workspace(workspace, "chg-2", GitObjectId(head))
        inside = Path(change.worktree_path) / "README.md"
        assert adapter._handle_read_text_file({"path": str(inside)}) == {"content": "hello\n"}
        adapter._handle_write_text_file({"path": str(inside), "content": "changed\n"})
        assert [item.path for item in adapter.changes(change).files] == ["README.md"]
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with pytest.raises(RuntimeAdapterError) as excinfo:
            adapter._handle_read_text_file({"path": str(outside)})
        assert excinfo.value.code is RuntimeErrorCode.PERMISSION_DENIED
    finally:
        adapter.cleanup_change_workspace(change)
    assert not Path(change.worktree_path).exists()


def test_permission_policy_requires_scoped_read_or_edit(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    adapter = _spawn_stub_adapter(tmp_path / "stub")
    change = adapter.ensure_change_workspace(
        adapter.attach_workspace(str(repo)), "chg-3", GitObjectId(head)
    )
    options = [
        {"optionId": "allow", "kind": "allow_once"},
        {"optionId": "reject", "kind": "reject_once"},
    ]
    try:
        inside = str(Path(change.worktree_path) / "README.md")
        approved = adapter._handle_request_permission({
            "toolCall": {"kind": "edit", "locations": [{"path": inside}]},
            "options": options,
        })
        assert approved["outcome"]["optionId"] == "allow"
        for tool_call in (
            {"kind": "execute", "locations": [{"path": inside}]},
            {"kind": "read", "locations": []},
            {"kind": "read", "locations": [{"path": str(tmp_path)}]},
        ):
            denied = adapter._handle_request_permission({"toolCall": tool_call, "options": options})
            assert denied["outcome"]["optionId"] == "reject"
    finally:
        adapter.cleanup_change_workspace(change)


def test_proxy_reconnects_across_instances_without_leaking_worker(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    script_dir = tmp_path / "stub"
    script_dir.mkdir()
    script = script_dir / "stub_agent.py"
    script.write_text(textwrap.dedent(_STUB_AGENT), encoding="utf-8")
    spec = AcpProviderSpec("reconnect-acp", (sys.executable, str(script)))

    first = AcpRuntimeProxy(spec)
    workspace = first.attach_workspace(str(repo))
    change = first.ensure_change_workspace(workspace, "chg-reconnect", GitObjectId(head))
    change = RuntimeChangeWorkspace(
        change.provider, change.workspace_id, change.change_set_id, "task-reconnect",
        change.base, change.worktree_path,
    )
    started = first.start_worker(change, "agent-1", "first turn")
    assert started.state is RuntimeWorkerState.RUNNING

    second = AcpRuntimeProxy(spec)
    workspace2 = second.attach_workspace(str(repo))
    found = second.get_change_workspace(workspace2, "chg-reconnect", GitObjectId(head))
    assert found is not None
    found = RuntimeChangeWorkspace(
        found.provider, found.workspace_id, found.change_set_id, "task-reconnect",
        found.base, found.worktree_path,
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        summary = second.worker_summary(found)
        if summary.state is RuntimeWorkerState.AWAITING_INPUT:
            break
        time.sleep(0.01)
    assert summary.state is RuntimeWorkerState.AWAITING_INPUT
    assert second.stop_worker(found).state is RuntimeWorkerState.EXITED
    second.cleanup_change_workspace(found)
