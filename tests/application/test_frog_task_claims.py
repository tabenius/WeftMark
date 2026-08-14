from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.claims import ClaimConflict, ClaimService
from weftmark.application.frog_planning import FrogPlanningService
from weftmark.application.frog_promotions import FrogPromotionService
from weftmark.application.frog_receipts import FrogReceiptService
from weftmark.application.frog_task_claims import (
    FrogTaskClaimError,
    FrogTaskClaimService,
)
from weftmark.application.ledger import LedgerService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def snapshot() -> dict[str, object]:
    records = {
        "repos": [],
        "tasks": [
            task("eligible", "idea", "p0"),
            task("eligible-two", "idea", "p1"),
            task("blocked", "blocked", "p0"),
        ],
        "task_dependencies": [],
        "task_conflicts": [],
        "task_tags": [],
        "task_assignments": [
            {"id": 1, "task_slug": "eligible", "agent_name": "source-owner"}
        ],
        "agents": [],
        "files": [],
        "task_files": [],
        "locks": [
            {"id": 1, "scope_key": "task:eligible", "status": "active"}
        ],
    }
    contents = {
        "source_kind": "frog-agents-db",
        "source_label": "workspace-main",
        "source_schema": {"migrations": ["001_initial.sql"]},
        "records": records,
    }
    canonical = json.dumps(
        contents, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return {
        "schema_version": 1,
        **contents,
        "captured_at": NOW.isoformat(),
        "digest": f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
    }


def task(slug: str, status: str, priority: str) -> dict[str, object]:
    return {
        "slug": slug,
        "repo_path": "/source/project",
        "title": slug.replace("-", " ").title(),
        "workflow_status": status,
        "git_status": "not_started",
        "priority": priority,
        "created_at": NOW.isoformat(),
    }


def services(
    tmp_path: Path,
) -> tuple[
    FrogTaskClaimService,
    FrogPromotionService,
    ClaimService,
    WorkspaceService,
    LedgerService,
    str,
]:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / ".state" / "ledger.jsonl"))
    receipts = FrogReceiptService(ledger)
    source = snapshot()
    receipts.record(source, imported_at=NOW)
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    claims = ClaimService(workspace, ledger)
    promotions = FrogPromotionService(receipts, workspace, ledger)
    workflow = FrogTaskClaimService(
        FrogPlanningService(receipts), promotions, claims
    )
    return workflow, promotions, claims, workspace, ledger, str(source["digest"])


def test_claim_composes_eligibility_promotion_and_native_authority_idempotently(
    tmp_path: Path,
) -> None:
    workflow, _, claims, _, ledger, digest = services(tmp_path)
    scopes = (Scope.contract("local-api"), Scope.file("src/**"))

    first = workflow.claim(
        digest,
        "eligible",
        change_set_id=None,
        claim_id=None,
        base_revision="HEAD",
        scopes=scopes,
        agent_id="local-worker",
        session_id="session-1",
        claimed_at=NOW,
        lease_seconds=300,
    )
    count = len(ledger.snapshot())
    repeated = workflow.claim(
        digest,
        "eligible",
        change_set_id=None,
        claim_id=None,
        base_revision="HEAD",
        scopes=scopes,
        agent_id="local-worker",
        session_id="session-1",
        claimed_at=NOW + timedelta(seconds=1),
        lease_seconds=300,
    )

    assert first.claimed is True
    assert repeated.claimed is False
    assert repeated.claim.id == first.claim.id
    assert repeated.promotion.promotion.change_set_id == (
        first.promotion.promotion.change_set_id
    )
    assert len(ledger.snapshot()) == count
    assert claims.list(
        change_set_id=first.promotion.promotion.change_set_id
    ) == (first.claim,)

    with pytest.raises(FrogTaskClaimError, match="different intent"):
        workflow.claim(
            digest,
            "eligible",
            change_set_id=None,
            claim_id=first.claim.id,
            base_revision="HEAD",
            scopes=scopes,
            agent_id="different-worker",
            session_id="session-1",
            claimed_at=NOW + timedelta(seconds=2),
            lease_seconds=300,
        )


def test_claim_refuses_ineligible_source_and_native_scope_conflict(
    tmp_path: Path,
) -> None:
    workflow, promotions, claims, workspace, ledger, digest = services(tmp_path)
    with pytest.raises(FrogTaskClaimError, match="lease duration"):
        workflow.claim(
            digest,
            "eligible",
            change_set_id="invalid-local",
            claim_id="invalid-claim",
            base_revision="HEAD",
            scopes=(Scope.contract("invalid-api"),),
            agent_id="worker",
            session_id="session",
            claimed_at=NOW,
            lease_seconds=0,
        )
    assert promotions.get(digest, "eligible") is None

    with pytest.raises(FrogTaskClaimError, match="not eligible.*blocked"):
        workflow.claim(
            digest,
            "blocked",
            change_set_id="blocked-local",
            claim_id="blocked-claim",
            base_revision="HEAD",
            scopes=(Scope.contract("blocked-api"),),
            agent_id="worker",
            session_id="session",
            claimed_at=NOW,
            lease_seconds=60,
        )
    assert promotions.get(digest, "blocked") is None

    workspace.create_change_set(
        id="existing-local",
        goal="Own shared scope",
        base_revision="HEAD",
        scopes=(Scope.contract("shared-api"),),
        created_at=NOW,
    )
    claims.acquire(
        "existing-local",
        id="existing-claim",
        agent_id="other-worker",
        session_id="other-session",
        acquired_at=NOW,
        lease_seconds=300,
    )
    before_claims = tuple(entry for entry in ledger.snapshot() if entry.kind == "claim")
    with pytest.raises(ClaimConflict, match="shared-api"):
        workflow.claim(
            digest,
            "eligible-two",
            change_set_id="conflicting-local",
            claim_id="conflicting-claim",
            base_revision="HEAD",
            scopes=(Scope.contract("shared-api"),),
            agent_id="worker",
            session_id="session",
            claimed_at=NOW + timedelta(seconds=1),
            lease_seconds=60,
        )
    after_claims = tuple(entry for entry in ledger.snapshot() if entry.kind == "claim")
    assert after_claims == before_claims
    assert promotions.get(digest, "eligible-two") is not None
