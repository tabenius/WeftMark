from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.evidence_runner import CommandEvidenceRequest
from weftmark.application.ledger import LedgerService
from weftmark.application.lifecycle import LifecyclePolicyError, LifecycleService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.changeset import ChangeSetState, InvalidTransition
from weftmark.domain.evidence import EvidenceKind, EvidenceProducer, ProducerKind
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 14, 11, 30, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(
    tmp_path: Path,
) -> tuple[WorkspaceService, LocalWorkflowService, LifecycleService]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / ".git" / "weftmark" / "lifecycle.jsonl"))
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    workspace.create_change_set(
        id="chg-1",
        goal="Complete lifecycle",
        base_revision="HEAD",
        scopes=(Scope.file("**"),),
        created_at=NOW,
    )
    workflow = LocalWorkflowService(
        workspace,
        ledger,
        EvidenceProducer(ProducerKind.WORKER, "test-worker"),
    )
    return workspace, workflow, LifecycleService(workspace, workflow)


def prove_ready(
    tmp_path: Path,
    workflow: LocalWorkflowService,
    *,
    handoff: bool = True,
) -> None:
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
    if handoff:
        workflow.create_handoff(
            "chg-1",
            id="handoff-1",
            task_id="work-lifecycle",
            next_action="Close",
            created_by="worker",
            created_at=NOW + timedelta(seconds=3),
        )


def test_ready_review_and_current_handoff_gate_terminal_success(
    tmp_path: Path,
) -> None:
    workspace, workflow, lifecycle = setup(tmp_path)
    prove_ready(tmp_path, workflow)
    review = lifecycle.transition(
        "chg-1",
        state=ChangeSetState.REVIEW,
        transitioned_at=NOW + timedelta(seconds=4),
    )
    merged = lifecycle.transition(
        "chg-1",
        state=ChangeSetState.MERGED,
        transitioned_at=NOW + timedelta(seconds=5),
    )
    closed = lifecycle.transition(
        "chg-1",
        state=ChangeSetState.CLOSED,
        transitioned_at=NOW + timedelta(seconds=6),
    )
    assert review.change_set.state is ChangeSetState.REVIEW
    assert merged.change_set.state is ChangeSetState.MERGED
    assert closed.change_set.state is ChangeSetState.CLOSED
    assert workspace.require_change_set("chg-1") == closed


def test_merged_refuses_missing_or_obsolete_review(tmp_path: Path) -> None:
    workspace, workflow, lifecycle = setup(tmp_path)
    workflow.review(
        "chg-1",
        decision_id="review-incomplete",
        author_id="reviewer",
        required_kinds=(EvidenceKind.TEST,),
        decided_at=NOW + timedelta(seconds=1),
    )
    lifecycle.transition(
        "chg-1",
        state=ChangeSetState.REVIEW,
        transitioned_at=NOW + timedelta(seconds=2),
    )
    with pytest.raises(LifecyclePolicyError, match="current releasable"):
        lifecycle.transition(
            "chg-1",
            state=ChangeSetState.MERGED,
            transitioned_at=NOW + timedelta(seconds=3),
        )

    workspace, workflow, lifecycle = setup(tmp_path / "stale")
    prove_ready(tmp_path / "stale", workflow)
    (tmp_path / "stale" / "next.txt").write_text("next\n", encoding="utf-8")
    git(tmp_path / "stale", "add", "next.txt")
    git(tmp_path / "stale", "commit", "-m", "next")
    workspace.refresh_change_set(
        "chg-1", observed_at=NOW + timedelta(seconds=4)
    )
    workflow.review(
        "chg-1",
        decision_id="review-stale",
        author_id="reviewer",
        required_kinds=(EvidenceKind.TEST,),
        decided_at=NOW + timedelta(seconds=5),
    )
    lifecycle.transition(
        "chg-1",
        state=ChangeSetState.REVIEW,
        transitioned_at=NOW + timedelta(seconds=6),
    )
    with pytest.raises(LifecyclePolicyError, match="current releasable"):
        lifecycle.transition(
            "chg-1",
            state=ChangeSetState.MERGED,
            transitioned_at=NOW + timedelta(seconds=7),
        )


def test_review_transition_requires_a_current_decision(tmp_path: Path) -> None:
    _, _, lifecycle = setup(tmp_path)
    with pytest.raises(LifecyclePolicyError, match="current review decision"):
        lifecycle.transition(
            "chg-1",
            state=ChangeSetState.REVIEW,
            transitioned_at=NOW + timedelta(seconds=1),
        )


def test_closed_refuses_missing_current_handoff(tmp_path: Path) -> None:
    _, workflow, lifecycle = setup(tmp_path)
    prove_ready(tmp_path, workflow, handoff=False)
    lifecycle.transition(
        "chg-1",
        state=ChangeSetState.REVIEW,
        transitioned_at=NOW + timedelta(seconds=3),
    )
    lifecycle.transition(
        "chg-1",
        state=ChangeSetState.MERGED,
        transitioned_at=NOW + timedelta(seconds=4),
    )
    with pytest.raises(LifecyclePolicyError, match="current clean-head handoff"):
        lifecycle.transition(
            "chg-1",
            state=ChangeSetState.CLOSED,
            transitioned_at=NOW + timedelta(seconds=5),
        )


def test_invalid_domain_transition_is_not_hidden_by_policy_gate(tmp_path: Path) -> None:
    _, _, lifecycle = setup(tmp_path)
    with pytest.raises(InvalidTransition, match="active to closed"):
        lifecycle.transition(
            "chg-1",
            state=ChangeSetState.CLOSED,
            transitioned_at=NOW + timedelta(seconds=1),
        )
