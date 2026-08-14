from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.frog_promotions import (
    FrogPromotionError,
    FrogPromotionService,
)
from weftmark.application.frog_receipts import FrogReceiptService
from weftmark.application.ledger import LedgerService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 14, 13, 0, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository(path: Path) -> Path:
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.name", "WeftMark Tests")
    git(path, "config", "user.email", "weftmark@example.invalid")
    git(path, "commit", "--allow-empty", "-m", "base")
    return path


def snapshot() -> dict[str, object]:
    contents = {
        "source_kind": "frog-agents-db",
        "source_label": "workspace-main",
        "source_schema": {"migrations": ["001_initial.sql"]},
        "records": {
            "repos": [],
            "tasks": [
                {
                    "slug": "external-done",
                    "repo_path": "/source/project",
                    "workflow_status": "done",
                    "assigned_agent": "someone-else",
                    "title": "Continue external intent locally",
                }
            ],
            "task_dependencies": [],
            "task_conflicts": [],
            "task_tags": [],
            "task_assignments": [],
            "agents": [],
            "files": [],
            "task_files": [],
            "locks": [
                {
                    "id": 1,
                    "scope_key": "task:external-done",
                    "status": "active",
                    "agent_name": "someone-else",
                }
            ],
        },
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


def services(
    repo: Path,
) -> tuple[FrogPromotionService, FrogReceiptService, WorkspaceService, LedgerService]:
    ledger = LedgerService(JsonlLedger(repo / ".state" / "ledger.jsonl"))
    receipts = FrogReceiptService(ledger)
    workspace = WorkspaceService(LocalGit(repo), ledger)
    return FrogPromotionService(receipts, workspace, ledger), receipts, workspace, ledger


def test_promotion_is_retry_safe_and_does_not_import_runtime_authority(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    promotions, receipts, workspace, ledger = services(repo)
    source = snapshot()
    digest = str(source["digest"])
    receipts.record(source, imported_at=NOW)
    scopes = (Scope.file("src/**"), Scope.contract("migration-v1"))

    first = promotions.promote(
        digest,
        "external-done",
        change_set_id="local-1",
        base_revision="HEAD",
        scopes=scopes,
        promoted_at=NOW,
    )
    entry_count = len(ledger.snapshot())
    repeated = promotions.promote(
        digest,
        "external-done",
        change_set_id="local-1",
        base_revision="HEAD",
        scopes=scopes,
        promoted_at=NOW,
    )

    assert first.promoted is True
    assert repeated.promoted is False
    assert repeated.promotion == first.promotion
    assert len(ledger.snapshot()) == entry_count
    binding = workspace.require_change_set("local-1")
    assert binding.change_set.goal == "Continue external intent locally"
    assert binding.change_set.state.value == "active"
    assert [entry.kind for entry in ledger.snapshot()].count("changeset") == 1
    assert not any(entry.kind == "claim" for entry in ledger.snapshot())
    assert first.promotion.source_repo_path == "/source/project"
    assert first.promotion.completed is True


def test_promotion_refuses_conflicting_retry_and_missing_sources(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    promotions, receipts, _, ledger = services(repo)
    source = snapshot()
    digest = str(source["digest"])
    receipts.record(source, imported_at=NOW)
    promotions.promote(
        digest,
        "external-done",
        change_set_id="local-1",
        base_revision="HEAD",
        scopes=(Scope.file("src/**"),),
        promoted_at=NOW,
    )
    entry_count = len(ledger.snapshot())

    with pytest.raises(FrogPromotionError, match="different local intent"):
        promotions.promote(
            digest,
            "external-done",
            change_set_id="local-2",
            base_revision="HEAD",
            scopes=(Scope.file("other/**"),),
            promoted_at=NOW,
        )
    with pytest.raises(FrogPromotionError, match="snapshot not found"):
        promotions.promote(
            "sha256:" + "0" * 64,
            "external-done",
            change_set_id=None,
            base_revision="HEAD",
            scopes=(Scope.file("src/**"),),
            promoted_at=NOW,
        )
    with pytest.raises(FrogPromotionError, match="task not found"):
        promotions.promote(
            digest,
            "missing",
            change_set_id=None,
            base_revision="HEAD",
            scopes=(Scope.file("src/**"),),
            promoted_at=NOW,
        )
    assert len(ledger.snapshot()) == entry_count


def test_reserved_promotion_recovers_after_interrupted_changeset_creation(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    promotions, receipts, workspace, ledger = services(repo)
    source = snapshot()
    digest = str(source["digest"])
    receipts.record(source, imported_at=NOW)

    class InterruptedWorkspace:
        def get_change_set(self, id: str):
            return None

        def create_change_set(self, **kwargs):
            raise RuntimeError("simulated interruption")

    interrupted = FrogPromotionService(receipts, InterruptedWorkspace(), ledger)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="simulated interruption"):
        interrupted.promote(
            digest,
            "external-done",
            change_set_id="local-recovered",
            base_revision="HEAD",
            scopes=(Scope.file("src/**"),),
            promoted_at=NOW,
        )

    reserved = promotions.get(digest, "external-done")
    assert reserved is not None and reserved.completed is False
    recovered = promotions.promote(
        digest,
        "external-done",
        change_set_id="local-recovered",
        base_revision="HEAD",
        scopes=(Scope.file("src/**"),),
        promoted_at=NOW,
    )
    assert recovered.promoted is False
    assert recovered.promotion.completed is True
    assert workspace.require_change_set("local-recovered").change_set.state.value == "active"
