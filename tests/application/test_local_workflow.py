from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.evidence_runner import CommandEvidenceRequest, EvidenceRunnerError
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceKind, EvidenceProducer, ProducerKind
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 14, 1, 40, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "WeftMark Tests")
    git(repo, "config", "user.email", "weftmark@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / "state" / "ledger.jsonl"))
    workspace = WorkspaceService(LocalGit(repo), ledger)
    workspace.create_change_set(
        id="chg-1",
        goal="Exercise local workflow",
        base_revision="HEAD",
        scopes=(Scope.file("src/**"), Scope.contract("api-v1")),
        created_at=NOW,
    )
    flow = LocalWorkflowService(
        workspace,
        ledger,
        EvidenceProducer(ProducerKind.WORKER, "cli"),
    )
    return repo, ledger, flow


def test_scope_audit_refreshes_git_and_persists_findings(tmp_path: Path) -> None:
    repo, ledger, flow = setup(tmp_path)
    (repo / "outside.txt").write_text("outside\n", encoding="utf-8")
    git(repo, "add", "outside.txt")
    git(repo, "commit", "-m", "outside")

    result = flow.audit_scope(
        "chg-1",
        semantic_changes=(Scope.schema("new-ledger"),),
        audited_at=NOW + timedelta(minutes=1),
    )
    assert result.uncovered_paths == ("outside.txt",)
    assert result.undeclared_semantic_scopes == (Scope.schema("new-ledger"),)
    stored = ledger.latest(kind="scope_audit", entity_id=result.observation_id)
    assert stored is not None
    assert len(stored.payload["findings"]) == 2


def test_command_evidence_refreshes_runs_persists_and_round_trips(tmp_path: Path) -> None:
    repo, _, flow = setup(tmp_path)
    request = CommandEvidenceRequest(
        id="ev-1",
        kind=EvidenceKind.TEST,
        argv=(sys.executable, "-c", "print('ok')"),
        cwd=str(repo),
    )

    result = flow.run_evidence(
        "chg-1", request, observed_at=NOW + timedelta(minutes=1)
    )
    assert result.passed
    assert flow.get_evidence("ev-1") == result
    assert flow.list_evidence(change_set_id="chg-1") == (result,)


def test_command_evidence_refuses_dirty_tree_before_execution(tmp_path: Path) -> None:
    repo, _, flow = setup(tmp_path)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    marker = repo / "must-not-exist"
    request = CommandEvidenceRequest(
        id="ev-dirty",
        kind=EvidenceKind.TEST,
        argv=(sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"),
        cwd=str(repo),
    )

    with pytest.raises(EvidenceRunnerError, match="clean worktree"):
        flow.run_evidence(
            "chg-1", request, observed_at=NOW + timedelta(minutes=1)
        )
    assert not marker.exists()
