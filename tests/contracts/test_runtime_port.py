from __future__ import annotations

from datetime import UTC, datetime

import pytest

from weftmark.application.ports.git import GitChangeKind, GitObjectId
from weftmark.application.ports.runtime import (
    RuntimeAdapterError,
    RuntimeChangeWorkspace,
    RuntimeChanges,
    RuntimeChangesMode,
    RuntimeContractError,
    RuntimeErrorCode,
    RuntimeFileChange,
    RuntimePort,
    RuntimeWorkerState,
    RuntimeWorkerSummary,
    RuntimeWorkspace,
)


SHA = GitObjectId("a" * 40)


def test_workspace_and_change_workspace_validate_identity() -> None:
    workspace = RuntimeWorkspace(provider="cline-kanban", workspace_id="demo", repo_path="/tmp/demo")
    change_workspace = RuntimeChangeWorkspace(
        provider=workspace.provider,
        workspace_id=workspace.workspace_id,
        change_set_id="chg-17",
        task_id="chg-17",
        base=SHA,
        worktree_path="/tmp/.cline/worktrees/chg-17/demo",
    )

    assert change_workspace.change_set_id == "chg-17"
    assert change_workspace.task_id == "chg-17"
    assert str(change_workspace.base) == "a" * 40


@pytest.mark.parametrize("field", ["provider", "workspace_id", "repo_path"])
def test_workspace_rejects_blank_fields(field: str) -> None:
    values = {"provider": "kanban", "workspace_id": "demo", "repo_path": "/tmp/demo"}
    values[field] = "  "
    with pytest.raises(RuntimeContractError):
        RuntimeWorkspace(**values)


def test_worker_summary_is_operational_telemetry() -> None:
    summary = RuntimeWorkerSummary(
        provider="cline-kanban",
        change_set_id="chg-17",
        task_id="chg-17",
        state=RuntimeWorkerState.AWAITING_INPUT,
        agent_id="codex",
        session_id="session-1",
        pid=123,
        started_at=datetime(2026, 8, 19, 13, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 19, 13, 1, tzinfo=UTC),
    )

    assert summary.state is RuntimeWorkerState.AWAITING_INPUT
    assert not hasattr(summary, "review_verdict")
    assert not hasattr(summary, "readiness")
    assert not hasattr(summary, "board_lane")


def test_worker_summary_rejects_naive_time_and_nonpositive_pid() -> None:
    with pytest.raises(RuntimeContractError, match="timezone"):
        RuntimeWorkerSummary(
            provider="kanban",
            change_set_id="chg",
            task_id="chg",
            state=RuntimeWorkerState.RUNNING,
            updated_at=datetime(2026, 8, 19, 13, 0),
        )

    with pytest.raises(RuntimeContractError, match="pid"):
        RuntimeWorkerSummary(
            provider="kanban",
            change_set_id="chg",
            task_id="chg",
            state=RuntimeWorkerState.RUNNING,
            pid=0,
        )


def test_runtime_changes_validate_paths_and_duplicates() -> None:
    changed = RuntimeFileChange(
        path="src/new.py",
        old_path="src/old.py",
        kind=GitChangeKind.RENAMED,
        additions=3,
        deletions=1,
    )
    changes = RuntimeChanges(
        provider="cline-kanban",
        change_set_id="chg-17",
        mode=RuntimeChangesMode.WORKING_COPY,
        files=(changed,),
        base=SHA,
    )

    assert changes.files[0].path == "src/new.py"

    with pytest.raises(RuntimeContractError, match="duplicate"):
        RuntimeChanges(
            provider="cline-kanban",
            change_set_id="chg-17",
            mode=RuntimeChangesMode.WORKING_COPY,
            files=(changed, changed),
        )


def test_runtime_file_change_rejects_unsafe_or_invalid_paths() -> None:
    with pytest.raises(RuntimeContractError):
        RuntimeFileChange(path="../secret", kind=GitChangeKind.MODIFIED)
    with pytest.raises(RuntimeContractError, match="old_path"):
        RuntimeFileChange(path="new.py", kind=GitChangeKind.RENAMED)
    with pytest.raises(RuntimeContractError, match="negative"):
        RuntimeFileChange(path="file.py", kind=GitChangeKind.MODIFIED, additions=-1)


def test_adapter_errors_use_small_normalized_vocabulary() -> None:
    error = RuntimeAdapterError(
        RuntimeErrorCode.TRANSPORT_FAILED,
        provider="cline-kanban",
        operation="ensure_change_workspace",
        detail="runtime refused connection",
    )
    assert error.code is RuntimeErrorCode.TRANSPORT_FAILED
    assert "transport_failed" in str(error)


def test_runtime_port_is_runtime_checkable_without_kanban_dependency() -> None:
    class FakeRuntime:
        def attach_workspace(self, repo_path):
            raise NotImplementedError

        def ensure_change_workspace(self, workspace, change_set_id, base):
            raise NotImplementedError

        def get_change_workspace(self, workspace, change_set_id, base):
            raise NotImplementedError

        def start_worker(self, change_workspace, agent_id, prompt, *, cols=None, rows=None):
            raise NotImplementedError

        def stop_worker(self, change_workspace):
            raise NotImplementedError

        def send_worker_input(self, change_workspace, data):
            raise NotImplementedError

        def worker_summary(self, change_workspace):
            raise NotImplementedError

        def changes(self, change_workspace, mode=RuntimeChangesMode.WORKING_COPY):
            raise NotImplementedError

        def cleanup_change_workspace(self, change_workspace):
            raise NotImplementedError

    assert isinstance(FakeRuntime(), RuntimePort)
