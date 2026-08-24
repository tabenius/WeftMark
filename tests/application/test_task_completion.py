from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.claims import ClaimService
from weftmark.application.evidence_runner import CommandEvidenceRequest
from weftmark.application.ledger import LedgerService
from weftmark.application.lifecycle import LifecycleService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.task_claims import TaskClaimService
from weftmark.application.task_completion import (
    TaskCompletionError,
    TaskCompletionService,
)
from weftmark.application.task_planning import TaskPlanningService
from weftmark.application.tasks import TaskService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.changeset import ChangeSetState
from weftmark.domain.evidence import EvidenceKind, EvidenceProducer, ProducerKind
from weftmark.domain.lock import LockState
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskIntent, TaskPriority, TaskState


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(tmp_path: Path):
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / ".git" / "weftmark" / "ledger.jsonl"))
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    claims = ClaimService(workspace, ledger)
    tasks = TaskService(ledger)
    task_claims = TaskClaimService(
        TaskPlanningService(tasks), tasks, workspace, claims, ledger
    )
    workflow = LocalWorkflowService(
        workspace,
        ledger,
        EvidenceProducer(ProducerKind.WORKER, "tests"),
    )
    service = TaskCompletionService(
        task_claims, claims, ledger
    )
    tasks.create(
        TaskIntent.create(
            id="native-work",
            title="Native work",
            why="prove completion",
            what="finish merged work",
            roi_note=None,
            priority=TaskPriority.P0,
            state=TaskState.TODO,
            scopes=(Scope.file("**"),),
            created_at=NOW,
        )
    )
    task_claims.claim(
        "native-work",
        change_set_id="native-work-cs",
        claim_id="native-work-claim",
        base_revision="HEAD",
        agent_id="worker-1",
        session_id="session-1",
        claimed_at=NOW + timedelta(seconds=1),
        lease_seconds=3600,
    )
    return tasks, workspace, claims, workflow, task_claims, ledger, service


def prove_merged(repo: Path, workspace, workflow) -> None:
    workflow.run_evidence(
        "native-work-cs",
        CommandEvidenceRequest(
            id="ev-ready",
            kind=EvidenceKind.TEST,
            argv=(sys.executable, "-c", "pass"),
            cwd=str(repo),
        ),
        observed_at=NOW + timedelta(seconds=2),
    )
    workflow.review(
        "native-work-cs",
        decision_id="review-ready",
        author_id="reviewer",
        required_kinds=(EvidenceKind.TEST,),
        decided_at=NOW + timedelta(seconds=3),
    )
    lifecycle = LifecycleService(workspace, workflow)
    lifecycle.transition(
        "native-work-cs",
        state=ChangeSetState.REVIEW,
        transitioned_at=NOW + timedelta(seconds=4),
    )
    lifecycle.transition(
        "native-work-cs",
        state=ChangeSetState.MERGED,
        transitioned_at=NOW + timedelta(seconds=5),
    )


def test_completion_releases_claim_and_identical_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    tasks, workspace, claims, workflow, _, _, service = setup(tmp_path)
    prove_merged(tmp_path, workspace, workflow)
    at = NOW + timedelta(seconds=6)

    first = service.complete(
        "native-work", actor_id="worker-1", reason="merged and verified", completed_at=at
    )
    repeated = service.complete(
        "native-work",
        actor_id="worker-1",
        reason="merged and verified",
        completed_at=at + timedelta(seconds=1),
    )

    assert first.completed is True
    assert first.claim_released is True
    assert repeated.completed is False
    assert repeated.claim_released is False
    assert tasks.require("native-work").state is TaskState.DONE
    assert len(tasks.require("native-work").state_events) == 2
    assert claims.get("native-work-claim").state_at(at) is LockState.RELEASED


def test_completion_refuses_unmerged_work_without_releasing_claim(
    tmp_path: Path,
) -> None:
    _, _, claims, _, _, _, service = setup(tmp_path)

    with pytest.raises(TaskCompletionError, match="Change Set to be merged"):
        service.complete(
            "native-work",
            actor_id="worker-1",
            reason="premature",
            completed_at=NOW + timedelta(seconds=2),
        )

    assert claims.get("native-work-claim").state_at(
        NOW + timedelta(seconds=2)
    ) is LockState.ACTIVE


def test_completion_refuses_expired_bound_claim(tmp_path: Path) -> None:
    tasks, workspace, _, workflow, _, _, service = setup(tmp_path)
    prove_merged(tmp_path, workspace, workflow)

    with pytest.raises(TaskCompletionError, match="expired bound claim"):
        service.complete(
            "native-work",
            actor_id="worker-1",
            reason="too late",
            completed_at=NOW + timedelta(hours=2),
        )
    assert tasks.require("native-work").state is TaskState.IN_PROGRESS


def test_completion_accepts_matching_released_claim_and_rejects_changed_retry(
    tmp_path: Path,
) -> None:
    _, workspace, claims, workflow, _, _, service = setup(tmp_path)
    prove_merged(tmp_path, workspace, workflow)
    claims.release(
        "native-work-claim",
        agent_id="worker-1",
        session_id="session-1",
        released_at=NOW + timedelta(seconds=6),
        reason="merge complete",
    )
    service.complete(
        "native-work",
        actor_id="worker-1",
        reason="merged and verified",
        completed_at=NOW + timedelta(seconds=7),
    )

    with pytest.raises(TaskCompletionError, match="different completion intent"):
        service.complete(
            "native-work",
            actor_id="other-worker",
            reason="rewrite history",
            completed_at=NOW + timedelta(seconds=8),
        )

    workflow.review(
        "native-work-cs",
        decision_id="later-incomplete-review",
        author_id="late-reviewer",
        required_kinds=(EvidenceKind.CI,),
        decided_at=NOW + timedelta(seconds=9),
    )
    repeated = service.complete(
        "native-work",
        actor_id="worker-1",
        reason="merged and verified",
        completed_at=NOW + timedelta(seconds=10),
    )
    assert repeated.completed is False
    assert repeated.review_id == "review-ready"

    workflow.create_handoff(
        "native-work-cs",
        id="post-completion-handoff",
        task_id="native-work",
        next_action="close completed work",
        created_by="worker-1",
        created_at=NOW + timedelta(seconds=11),
    )
    LifecycleService(workspace, workflow).transition(
        "native-work-cs",
        state=ChangeSetState.CLOSED,
        transitioned_at=NOW + timedelta(seconds=12),
    )
    after_close = service.complete(
        "native-work",
        actor_id="worker-1",
        reason="merged and verified",
        completed_at=NOW + timedelta(seconds=13),
    )
    assert after_close.completed is False
    assert after_close.review_id == "review-ready"


def test_newer_incomplete_review_during_release_prevents_task_transition(
    tmp_path: Path,
) -> None:
    tasks, workspace, _, workflow, task_claims, ledger, _ = setup(tmp_path)
    prove_merged(tmp_path, workspace, workflow)

    class ReviewInjectingClaimService(ClaimService):
        def release(self, *args, **kwargs):
            released = super().release(*args, **kwargs)
            workflow.review(
                "native-work-cs",
                decision_id="racing-incomplete-review",
                author_id="racing-reviewer",
                required_kinds=(EvidenceKind.CI,),
                decided_at=kwargs["released_at"],
            )
            return released

    racing_claims = ReviewInjectingClaimService(workspace, ledger)
    guarded = TaskCompletionService(task_claims, racing_claims, ledger)

    with pytest.raises(TaskCompletionError, match="current required evidence"):
        guarded.complete(
            "native-work",
            actor_id="worker-1",
            reason="merged and verified",
            completed_at=NOW + timedelta(seconds=6),
        )
    assert tasks.require("native-work").state is TaskState.IN_PROGRESS

    workflow.review(
        "native-work-cs",
        decision_id="recovered-ready-review",
        author_id="reviewer",
        required_kinds=(EvidenceKind.TEST,),
        decided_at=NOW + timedelta(seconds=7),
    )
    recovered = guarded.complete(
        "native-work",
        actor_id="worker-1",
        reason="merged and verified",
        completed_at=NOW + timedelta(seconds=8),
    )
    assert recovered.completed is True
    assert recovered.claim_released is False
