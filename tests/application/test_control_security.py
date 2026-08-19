from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.claims import ClaimService
from weftmark.application.control import ControlService, ControlServiceError
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.task_claims import TaskClaimService
from weftmark.application.task_planning import TaskPlanningService
from weftmark.application.tasks import TaskService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceProducer, ProducerKind
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskIntent, TaskPriority, TaskState


NOW = datetime(2026, 8, 19, 16, 45, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def fixture(tmp_path: Path) -> tuple[ControlService, TaskService, LedgerService]:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / ".git" / "weftmark" / "ledger.jsonl"))
    tasks = TaskService(ledger)
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    claims = ClaimService(workspace, ledger)
    task_claims = TaskClaimService(
        TaskPlanningService(tasks), tasks, workspace, claims, ledger
    )
    workflow = LocalWorkflowService(
        workspace,
        ledger,
        EvidenceProducer(ProducerKind.WORKER, "control-security-tests"),
    )
    return ControlService(task_claims, claims, workflow, ledger), tasks, ledger


def create_task(tasks: TaskService) -> None:
    tasks.create(
        TaskIntent.create(
            id="task-a",
            title="Secure control",
            why="Verify idempotency identifiers are not persisted verbatim.",
            what="Claim through the control boundary.",
            roi_note=None,
            priority=TaskPriority.P0,
            state=TaskState.TODO,
            scopes=(Scope.file("src/**"),),
            created_at=NOW,
        )
    )


def test_raw_idempotency_key_is_not_persisted_as_ledger_identity(tmp_path: Path) -> None:
    control, tasks, ledger = fixture(tmp_path)
    create_task(tasks)
    key = "mobile-request-20260819-0001"

    control.claim_task(
        "task-a",
        idempotency_key=key,
        change_set_id="chg-a",
        claim_id="claim-a",
        base_revision="HEAD",
        agent_id="worker-a",
        session_id="session-a",
        lease_seconds=300,
        requested_at=NOW,
    )

    records = tuple(
        entry
        for entry in ledger.snapshot()
        if entry.kind == "control_idempotency_v0"
    )
    assert len(records) == 1
    assert records[0].entity_id.startswith("idem-")
    assert key not in records[0].entity_id
    assert key not in records[0].draft.payload_json


@pytest.mark.parametrize(
    "key",
    (
        "github_pat_1234567890abcdef",
        "ghp_1234567890abcdef",
        "sk-example12345678",
        "token=example123456",
    ),
)
def test_credential_like_idempotency_keys_fail_before_mutation(
    tmp_path: Path, key: str
) -> None:
    control, tasks, ledger = fixture(tmp_path)
    create_task(tasks)
    before = len(ledger.snapshot())

    with pytest.raises(ControlServiceError, match="idempotency_key"):
        control.claim_task(
            "task-a",
            idempotency_key=key,
            change_set_id="chg-a",
            claim_id="claim-a",
            base_revision="HEAD",
            agent_id="worker-a",
            session_id="session-a",
            lease_seconds=300,
            requested_at=NOW,
        )

    assert len(ledger.snapshot()) == before
