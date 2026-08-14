from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.bundle import BundleError, BundleService, verify_bundle
from weftmark.application.claims import ClaimService
from weftmark.application.evidence_runner import CommandEvidenceRequest
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceKind, EvidenceProducer, ProducerKind
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(tmp_path: Path) -> BundleService:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / ".git" / "weftmark" / "bundle.jsonl"))
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    workspace.create_change_set(
        id="chg-1",
        goal="Export portable proof",
        base_revision="HEAD",
        scopes=(Scope.file("**"), Scope.contract("bundle-v1")),
        created_at=NOW,
    )
    claims = ClaimService(workspace, ledger)
    claims.acquire(
        "chg-1",
        id="claim-1",
        agent_id="worker",
        session_id="session",
        acquired_at=NOW + timedelta(seconds=1),
        lease_seconds=300,
    )
    workflow = LocalWorkflowService(
        workspace,
        ledger,
        EvidenceProducer(ProducerKind.WORKER, "test-worker"),
    )
    workflow.run_evidence(
        "chg-1",
        CommandEvidenceRequest(
            id="ev-1",
            kind=EvidenceKind.TEST,
            argv=(
                sys.executable,
                "-c",
                "import os; print(os.environ['TEST_OUTPUT'])",
            ),
            cwd=str(tmp_path),
            environment=(("TEST_OUTPUT", "not exported raw"),),
        ),
        observed_at=NOW + timedelta(seconds=2),
    )
    workflow.review(
        "chg-1",
        decision_id="review-1",
        author_id="reviewer",
        required_kinds=(EvidenceKind.TEST,),
        semantic_changes=(Scope.contract("bundle-v1"),),
        decided_at=NOW + timedelta(seconds=3),
    )
    workflow.create_handoff(
        "chg-1",
        id="handoff-1",
        task_id="work-bundle",
        next_action="Transfer bundle",
        created_by="worker",
        created_at=NOW + timedelta(seconds=4),
    )
    return BundleService(workspace, claims, workflow)


def test_export_selects_related_records_and_strips_local_locations(
    tmp_path: Path,
) -> None:
    bundle = setup(tmp_path).export(
        "chg-1", exported_at=NOW + timedelta(seconds=5)
    )
    encoded = json.dumps(bundle, sort_keys=True)
    contents = bundle["contents"]

    assert str(tmp_path) not in encoded
    assert "not exported raw" not in encoded
    assert contents["change_set"]["repository_fingerprint"]
    assert contents["evidence"][0]["command"]["cwd"] == "."
    assert contents["evidence"][0]["command"]["argv"][0] == Path(
        sys.executable
    ).name
    assert "description" not in contents["evidence"][0]["environment"]
    verification = verify_bundle(bundle)
    assert verification.change_set_id == "chg-1"
    assert verification.claim_count == 1
    assert verification.evidence_count == 1
    assert verification.review_count == 1
    assert verification.handoff_count == 1


def test_bundle_digest_and_privacy_contract_fail_closed(tmp_path: Path) -> None:
    bundle = setup(tmp_path).export(
        "chg-1", exported_at=NOW + timedelta(seconds=5)
    )
    tampered = json.loads(json.dumps(bundle))
    tampered["contents"]["change_set"]["goal"] = "tampered"
    with pytest.raises(BundleError, match="digest"):
        verify_bundle(tampered)

    unsafe = json.loads(json.dumps(bundle))
    unsafe["contents"]["change_set"]["worktree"] = "/private/source"
    with pytest.raises(BundleError, match="local or raw-output"):
        verify_bundle(unsafe)
