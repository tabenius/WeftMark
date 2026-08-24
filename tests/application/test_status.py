from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.claims import ClaimService
from weftmark.application.evidence_runner import CommandEvidenceRequest
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.task_claims import TaskClaimService
from weftmark.application.task_planning import TaskPlanningService
from weftmark.application.tasks import TaskService
from weftmark.application.status import StatusService, status_to_payload
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceKind, EvidenceProducer, ProducerKind
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskIntent, TaskPriority, TaskState


NOW = datetime(2026, 8, 14, 11, 0, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(
    tmp_path: Path,
) -> tuple[WorkspaceService, ClaimService, LocalWorkflowService]:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / ".git" / "weftmark" / "status.jsonl"))
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    workspace.create_change_set(
        id="chg-1",
        goal="Summarize the workspace",
        base_revision="HEAD",
        scopes=(Scope.file("src/**"), Scope.contract("status-v0")),
        created_at=NOW,
    )
    claims = ClaimService(workspace, ledger)
    workflow = LocalWorkflowService(
        workspace,
        ledger,
        EvidenceProducer(ProducerKind.WORKER, "test-worker"),
    )
    return workspace, claims, workflow


def test_status_composes_current_claim_evidence_review_and_handoff(
    tmp_path: Path,
) -> None:
    workspace, claims, workflow = setup(tmp_path)
    claims.acquire(
        "chg-1",
        id="claim-1",
        agent_id="worker-1",
        session_id="session-1",
        acquired_at=NOW + timedelta(seconds=1),
        lease_seconds=300,
    )
    workflow.run_evidence(
        "chg-1",
        CommandEvidenceRequest(
            id="ev-1",
            kind=EvidenceKind.TEST,
            argv=(sys.executable, "-c", "pass"),
            cwd=str(tmp_path),
        ),
        observed_at=NOW + timedelta(seconds=2),
    )
    workflow.review(
        "chg-1",
        decision_id="review-1",
        author_id="reviewer",
        required_kinds=(EvidenceKind.TEST,),
        semantic_changes=(Scope.contract("status-v0"),),
        decided_at=NOW + timedelta(seconds=3),
    )
    workflow.create_handoff(
        "chg-1",
        id="handoff-1",
        task_id="work-status",
        next_action="Publish",
        created_by="worker-1",
        created_at=NOW + timedelta(seconds=4),
    )

    payload = status_to_payload(
        StatusService(workspace, claims, workflow).summarize(
            observed_at=NOW + timedelta(seconds=5)
        )
    )
    value = payload["change_sets"][0]
    assert payload["counts"] == {
        "change_sets": 1,
        "active_claims": 1,
        "expired_claims": 0,
        "released_claims": 0,
    }
    assert value["readiness"] == "ready"
    assert value["active_claim_ids"] == ["claim-1"]
    assert value["scope_collisions"] == []
    assert value["evidence"] == {
        "total": 1,
        "current": 1,
        "obsolete": 0,
        "failed": 0,
        "unavailable": 0,
    }
    assert value["latest_review"]["is_current"]
    assert value["latest_handoff"]["is_current"]


def test_status_marks_old_proof_and_decisions_stale_after_observed_head_moves(
    tmp_path: Path,
) -> None:
    workspace, claims, workflow = setup(tmp_path)
    workflow.run_evidence(
        "chg-1",
        CommandEvidenceRequest(
            id="ev-1",
            kind=EvidenceKind.TEST,
            argv=(sys.executable, "-c", "pass"),
            cwd=str(tmp_path),
        ),
        observed_at=NOW + timedelta(seconds=1),
    )
    workflow.review(
        "chg-1",
        decision_id="review-1",
        author_id="reviewer",
        required_kinds=(EvidenceKind.TEST,),
        decided_at=NOW + timedelta(seconds=2),
    )
    workflow.create_handoff(
        "chg-1",
        id="handoff-1",
        task_id="work-status",
        next_action="Continue",
        created_by="worker",
        created_at=NOW + timedelta(seconds=3),
    )
    (tmp_path / "next.txt").write_text("next\n", encoding="utf-8")
    git(tmp_path, "add", "next.txt")
    git(tmp_path, "commit", "-m", "next")
    workspace.refresh_change_set(
        "chg-1", observed_at=NOW + timedelta(seconds=4)
    )

    payload = status_to_payload(
        StatusService(workspace, claims, workflow).summarize(
            observed_at=NOW + timedelta(seconds=5)
        )
    )["change_sets"][0]
    assert payload["readiness"] == "stale"
    assert payload["evidence"]["current"] == 0
    assert payload["evidence"]["obsolete"] == 1
    assert not payload["latest_review"]["is_current"]
    assert not payload["latest_handoff"]["is_current"]


def test_status_exposes_cross_file_contract_blocker_until_claim_expires(
    tmp_path: Path,
) -> None:
    workspace, claims, workflow = setup(tmp_path)
    workspace.create_change_set(
        id="chg-2",
        goal="Change documentation-side authentication behavior",
        base_revision="HEAD",
        scopes=(Scope.file("docs/**"), Scope.contract("status-v0")),
        created_at=NOW + timedelta(milliseconds=500),
    )
    claims.acquire(
        "chg-1",
        id="claim-1",
        agent_id="worker-1",
        session_id="session-1",
        acquired_at=NOW + timedelta(seconds=1),
        lease_seconds=10,
    )

    active = status_to_payload(
        StatusService(workspace, claims, workflow).summarize(
            observed_at=NOW + timedelta(seconds=2)
        )
    )
    by_id = {value["id"]: value for value in active["change_sets"]}

    assert by_id["chg-1"]["scope_collisions"] == []
    assert by_id["chg-2"]["scope_collisions"] == [
        {
            "claim_id": "claim-1",
            "competing_change_set_id": "chg-1",
            "requested_scope": {"kind": "contract", "key": "status-v0"},
            "owned_scope": {"kind": "contract", "key": "status-v0"},
        }
    ]

    expired = status_to_payload(
        StatusService(workspace, claims, workflow).summarize(
            observed_at=NOW + timedelta(seconds=12)
        )
    )
    by_id = {value["id"]: value for value in expired["change_sets"]}
    assert by_id["chg-2"]["scope_collisions"] == []


def test_status_includes_native_task_sources_relations_and_work_binding(
    tmp_path: Path,
) -> None:
    workspace, claims, workflow = setup(tmp_path)
    ledger = LedgerService(JsonlLedger(tmp_path / ".git" / "weftmark" / "status.jsonl"))
    tasks = TaskService(ledger)
    tasks.create(
        TaskIntent.create(
            id="plan-work",
            title="Plan work",
            why="show intent",
            what="project task",
            roi_note=None,
            priority=TaskPriority.P0,
            state=TaskState.TODO,
            scopes=(Scope.file("docs/**"),),
            created_at=NOW + timedelta(seconds=1),
        )
    )
    task_claims = TaskClaimService(
        TaskPlanningService(tasks), tasks, workspace, claims, ledger
    )
    task_claims.claim(
        "plan-work",
        change_set_id="plan-work-cs",
        claim_id="plan-work-claim",
        base_revision="HEAD",
        agent_id="worker",
        session_id="session",
        claimed_at=NOW + timedelta(seconds=2),
        lease_seconds=300,
    )
    ledger.record(
        kind="source_plan_import",
        entity_id="weftmark/tasks",
        payload={
            "source_label": "weftmark/tasks",
            "source_digest": "sha256:" + "a" * 64,
            "native_task_ids": ["plan-work"],
        },
        recorded_at=NOW + timedelta(seconds=3),
    )
    ledger.record(
        kind="frog_native_task_import",
        entity_id="frog/workspace",
        payload={
            "source_label": "frog/workspace",
            "source_snapshot_digest": "sha256:" + "b" * 64,
            "native_tasks": {"plan-work": {}},
        },
        recorded_at=NOW + timedelta(seconds=3, microseconds=1),
    )

    payload = status_to_payload(
        StatusService(
            workspace, claims, workflow, tasks=tasks, ledger=ledger
        ).summarize(observed_at=NOW + timedelta(seconds=4))
    )

    assert payload["counts"]["tasks"] == 1
    assert payload["tasks"][0]["state"] == "in_progress"
    assert payload["tasks"][0]["sources"] == [
        {
            "kind": "frog_snapshot",
            "label": "frog/workspace",
            "digest": "sha256:" + "b" * 64,
        },
        {
            "kind": "source_plan",
            "label": "weftmark/tasks",
            "digest": "sha256:" + "a" * 64,
        }
    ]
    assert payload["task_change_set_links"] == [
        {
            "task_id": "plan-work",
            "change_set_id": "plan-work-cs",
            "claim_id": "plan-work-claim",
            "binding_state": "completed",
        }
    ]
