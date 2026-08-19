from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.claims import ClaimService
from weftmark.application.control import ControlConflict, ControlService
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.task_claims import TaskClaimService
from weftmark.application.task_planning import TaskPlanningService
from weftmark.application.tasks import TaskService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceProducer, ProducerKind
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskIntent, TaskPriority, TaskState


NOW = datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def services(
    tmp_path: Path,
) -> tuple[ControlService, TaskService, ClaimService, LedgerService]:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / ".state" / "ledger.jsonl"))
    tasks = TaskService(ledger)
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    claims = ClaimService(workspace, ledger)
    task_claims = TaskClaimService(
        TaskPlanningService(tasks), tasks, workspace, claims, ledger
    )
    workflow = LocalWorkflowService(
        workspace,
        ledger,
        EvidenceProducer(ProducerKind.WORKER, "control-tests"),
    )
    return ControlService(task_claims, claims, workflow, ledger), tasks, claims, ledger


def create_task(tasks: TaskService, id: str = "task-a") -> None:
    tasks.create(
        TaskIntent.create(
            id=id,
            title=f"Task {id}",
            why="Exercise board control safely.",
            what="Claim and hand off through application services.",
            roi_note=None,
            priority=TaskPriority.P0,
            state=TaskState.TODO,
            scopes=(Scope.file("src/**"), Scope.contract("control-v0")),
            created_at=NOW,
        )
    )


def claim(control: ControlService, *, key: str = "claim-request-1"):
    return control.claim_task(
        "task-a",
        idempotency_key=key,
        change_set_id="chg-a",
        claim_id="claim-a",
        base_revision="HEAD",
        agent_id="worker-a",
        session_id="session-a",
        lease_seconds=600,
        requested_at=NOW,
    )


def test_claim_task_replays_same_idempotency_result_without_duplicate_writes(
    tmp_path: Path,
) -> None:
    control, tasks, _, ledger = services(tmp_path)
    create_task(tasks)

    first = claim(control)
    count = len(ledger.snapshot())
    second = claim(control)

    assert first.replayed is False
    assert second.replayed is True
    assert second.payload == first.payload
    assert len(ledger.snapshot()) == count
    assert first.payload["change_set"]["id"] == "chg-a"
    assert first.payload["claim"]["id"] == "claim-a"
    assert tasks.require("task-a").state is TaskState.IN_PROGRESS


def test_idempotency_key_cannot_be_reused_for_different_intent(tmp_path: Path) -> None:
    control, tasks, _, _ = services(tmp_path)
    create_task(tasks)
    claim(control)

    with pytest.raises(ControlConflict, match="different request"):
        control.claim_task(
            "task-a",
            idempotency_key="claim-request-1",
            change_set_id="chg-a",
            claim_id="claim-a",
            base_revision="HEAD",
            agent_id="worker-a",
            session_id="different-session",
            lease_seconds=600,
            requested_at=NOW + timedelta(seconds=1),
        )


def test_release_is_idempotent_and_recovers_if_release_preceded_control_record(
    tmp_path: Path,
) -> None:
    control, tasks, claims, _ = services(tmp_path)
    create_task(tasks)
    claim(control)

    # Simulate a process crash after the domain release but before a remote
    # idempotency record was written.
    claims.release(
        "claim-a",
        agent_id="worker-a",
        session_id="session-a",
        released_at=NOW + timedelta(seconds=1),
        reason="handoff",
    )
    recovered = control.release_claim(
        "claim-a",
        idempotency_key="release-request-1",
        agent_id="worker-a",
        session_id="session-a",
        reason="handoff",
        requested_at=NOW + timedelta(seconds=2),
    )
    replayed = control.release_claim(
        "claim-a",
        idempotency_key="release-request-1",
        agent_id="worker-a",
        session_id="session-a",
        reason="handoff",
        requested_at=NOW + timedelta(seconds=3),
    )

    assert recovered.replayed is False
    assert recovered.payload["effective_state"] == "released"
    assert replayed.replayed is True
    assert replayed.payload == recovered.payload


def test_handoff_creation_is_retry_safe_and_does_not_need_chat_history(
    tmp_path: Path,
) -> None:
    control, tasks, _, _ = services(tmp_path)
    create_task(tasks)
    claim(control)

    first = control.create_handoff(
        "chg-a",
        idempotency_key="handoff-request-1",
        handoff_id="handoff-a",
        task_id="task-a",
        next_action="Continue the HTTP control bridge",
        created_by="worker-a",
        requested_at=NOW + timedelta(seconds=1),
        intended_receiver_id="worker-b",
        known_failures=("No runtime provider is attached through HTTP yet",),
    )
    replayed = control.create_handoff(
        "chg-a",
        idempotency_key="handoff-request-1",
        handoff_id="handoff-a",
        task_id="task-a",
        next_action="Continue the HTTP control bridge",
        created_by="worker-a",
        requested_at=NOW + timedelta(seconds=2),
        intended_receiver_id="worker-b",
        known_failures=("No runtime provider is attached through HTTP yet",),
    )

    assert first.replayed is False
    assert replayed.replayed is True
    assert replayed.payload == first.payload
    assert first.payload["change_set_id"] == "chg-a"
    assert first.payload["head_sha"]
    assert "transcript" not in first.payload


def test_existing_handoff_id_with_different_intent_fails_closed(tmp_path: Path) -> None:
    control, tasks, _, _ = services(tmp_path)
    create_task(tasks)
    claim(control)
    control.create_handoff(
        "chg-a",
        idempotency_key="handoff-request-1",
        handoff_id="handoff-a",
        task_id="task-a",
        next_action="Continue safely",
        created_by="worker-a",
        requested_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ControlConflict, match="different intent"):
        control.create_handoff(
            "chg-a",
            idempotency_key="handoff-request-2",
            handoff_id="handoff-a",
            task_id="task-a",
            next_action="Do something unrelated",
            created_by="worker-a",
            requested_at=NOW + timedelta(seconds=2),
        )
