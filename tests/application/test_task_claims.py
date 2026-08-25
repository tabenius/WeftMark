from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.claims import ClaimConflict, ClaimService
from weftmark.application.ledger import LedgerService
from weftmark.application.task_claims import TaskClaimError, TaskClaimService
from weftmark.application.task_planning import TaskPlanningService
from weftmark.application.tasks import TaskService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.lock import LockState
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskIntent, TaskPriority, TaskState


NOW = datetime(2026, 8, 14, 19, 0, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def intent(id: str, *scopes: Scope) -> TaskIntent:
    return TaskIntent.create(
        id=id,
        title=f"Task {id}",
        why="Own native work safely.",
        what="Create a Change Set and claim.",
        roi_note=None,
        priority=TaskPriority.P0,
        state=TaskState.TODO,
        scopes=tuple(scopes),
        created_at=NOW,
    )


def services(
    tmp_path: Path,
) -> tuple[
    TaskClaimService,
    TaskService,
    WorkspaceService,
    ClaimService,
    LedgerService,
]:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / ".state" / "ledger.jsonl"))
    tasks = TaskService(ledger)
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    claims = ClaimService(workspace, ledger)
    service = TaskClaimService(
        TaskPlanningService(tasks), tasks, workspace, claims, ledger
    )
    return service, tasks, workspace, claims, ledger


def test_native_task_claim_is_retry_safe_and_transitions_task(tmp_path: Path) -> None:
    service, tasks, workspace, claims, ledger = services(tmp_path)
    tasks.create(intent("task-a", Scope.file("src/**"), Scope.contract("api-v1")))

    first = service.claim(
        "task-a",
        change_set_id=None,
        claim_id=None,
        base_revision="HEAD",
        agent_id="worker-1",
        session_id="session-1",
        claimed_at=NOW,
        lease_seconds=300,
    )
    count = len(ledger.snapshot())
    repeated = service.claim(
        "task-a",
        change_set_id=None,
        claim_id=None,
        base_revision="HEAD",
        agent_id="worker-1",
        session_id="session-1",
        claimed_at=NOW + timedelta(seconds=1),
        lease_seconds=300,
    )

    assert first.claimed is True
    assert first.binding.completed is True
    assert repeated.claimed is False
    assert repeated.binding == first.binding
    assert repeated.claim == first.claim
    assert len(ledger.snapshot()) == count
    assert tasks.require("task-a").state is TaskState.IN_PROGRESS
    assert workspace.require_change_set(first.binding.change_set_id).change_set.goal == (
        "Task task-a"
    )
    assert claims.get(first.binding.claim_id) == first.claim

    with pytest.raises(TaskClaimError, match="different work binding"):
        service.claim(
            "task-a",
            change_set_id=None,
            claim_id=None,
            base_revision="HEAD",
            agent_id="different-worker",
            session_id="session-1",
            claimed_at=NOW + timedelta(seconds=2),
            lease_seconds=300,
        )


def test_native_task_claim_recovers_its_expired_bound_claim(tmp_path: Path) -> None:
    service, tasks, _, _, ledger = services(tmp_path)
    tasks.create(intent("task-a", Scope.contract("api-v1")))
    first = service.claim(
        "task-a",
        change_set_id="task-a-work",
        claim_id="task-a-claim",
        base_revision="HEAD",
        agent_id="worker-1",
        session_id="session-1",
        claimed_at=NOW,
        lease_seconds=10,
    )
    before = len(ledger.snapshot())

    recovered = service.claim(
        "task-a",
        change_set_id="task-a-work",
        claim_id="task-a-claim",
        base_revision="HEAD",
        agent_id="worker-1",
        session_id="session-1",
        claimed_at=NOW + timedelta(seconds=11),
        lease_seconds=300,
    )

    assert recovered.claimed is True
    assert recovered.binding == first.binding
    assert recovered.claim.locks[0].events[-1].kind.value == "reacquired"
    assert recovered.claim.state_at(NOW + timedelta(seconds=11)) is LockState.ACTIVE
    assert len(ledger.snapshot()) == before + 1


def test_native_task_claim_does_not_recover_after_task_is_blocked(
    tmp_path: Path,
) -> None:
    service, tasks, _, claims, ledger = services(tmp_path)
    tasks.create(intent("task-a", Scope.contract("api-v1")))
    service.claim(
        "task-a",
        change_set_id="task-a-work",
        claim_id="task-a-claim",
        base_revision="HEAD",
        agent_id="worker-1",
        session_id="session-1",
        claimed_at=NOW,
        lease_seconds=10,
    )
    tasks.transition(
        "task-a",
        state=TaskState.BLOCKED,
        actor_id="planner",
        rationale="dependency became unavailable",
        occurred_at=NOW + timedelta(seconds=11),
    )
    before = ledger.snapshot()

    with pytest.raises(TaskClaimError, match="incompatible state: blocked"):
        service.claim(
            "task-a",
            change_set_id="task-a-work",
            claim_id="task-a-claim",
            base_revision="HEAD",
            agent_id="worker-1",
            session_id="session-1",
            claimed_at=NOW + timedelta(seconds=12),
            lease_seconds=300,
        )

    assert ledger.snapshot() == before
    assert claims.get("task-a-claim").state_at(
        NOW + timedelta(seconds=12)
    ) is LockState.EXPIRED


def test_native_task_claim_refuses_ineligible_scopeless_and_invalid_requests(
    tmp_path: Path,
) -> None:
    service, tasks, _, _, ledger = services(tmp_path)
    tasks.create(intent("blocked", Scope.contract("blocked-v1")))
    tasks.transition(
        "blocked",
        state=TaskState.BLOCKED,
        actor_id="planner",
        rationale="dependency missing",
        occurred_at=NOW,
    )
    tasks.create(intent("scopeless"))
    before = ledger.snapshot()

    with pytest.raises(TaskClaimError, match="not eligible"):
        service.claim(
            "blocked",
            change_set_id="blocked-work",
            claim_id="blocked-claim",
            base_revision="HEAD",
            agent_id="worker",
            session_id="session",
            claimed_at=NOW,
            lease_seconds=60,
        )
    with pytest.raises(TaskClaimError, match="declared scopes"):
        service.claim(
            "scopeless",
            change_set_id="scopeless-work",
            claim_id="scopeless-claim",
            base_revision="HEAD",
            agent_id="worker",
            session_id="session",
            claimed_at=NOW,
            lease_seconds=60,
        )
    with pytest.raises(TaskClaimError, match="lease duration"):
        service.claim(
            "scopeless",
            change_set_id=None,
            claim_id=None,
            base_revision="HEAD",
            agent_id="worker",
            session_id="session",
            claimed_at=NOW,
            lease_seconds=0,
        )
    assert service.get("blocked") is None
    assert service.get("scopeless") is None
    assert ledger.snapshot() == before


def test_reserved_binding_recovers_after_native_scope_conflict(tmp_path: Path) -> None:
    service, tasks, workspace, claims, _ = services(tmp_path)
    tasks.create(intent("task-a", Scope.contract("shared-v1")))
    workspace.create_change_set(
        id="existing-work",
        goal="Existing owner",
        base_revision="HEAD",
        scopes=(Scope.contract("shared-v1"),),
        created_at=NOW,
    )
    claims.acquire(
        "existing-work",
        id="existing-claim",
        agent_id="other",
        session_id="other-session",
        acquired_at=NOW,
        lease_seconds=300,
    )

    with pytest.raises(ClaimConflict, match="shared-v1"):
        service.claim(
            "task-a",
            change_set_id="task-a-work",
            claim_id="task-a-claim",
            base_revision="HEAD",
            agent_id="worker",
            session_id="session",
            claimed_at=NOW + timedelta(seconds=1),
            lease_seconds=300,
        )
    reserved = service.get("task-a")
    assert reserved is not None and reserved.completed is False
    assert tasks.require("task-a").state is TaskState.TODO
    assert workspace.get_change_set("task-a-work") is not None
    assert claims.get("task-a-claim") is None

    claims.release(
        "existing-claim",
        agent_id="other",
        session_id="other-session",
        released_at=NOW + timedelta(seconds=2),
        reason="handoff",
    )
    recovered = service.claim(
        "task-a",
        change_set_id="task-a-work",
        claim_id="task-a-claim",
        base_revision="HEAD",
        agent_id="worker",
        session_id="session",
        claimed_at=NOW + timedelta(seconds=3),
        lease_seconds=300,
    )
    assert recovered.claimed is True
    assert recovered.binding.completed is True
    assert tasks.require("task-a").state is TaskState.IN_PROGRESS
