from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.frog_receipts import (
    FrogReceiptError,
    FrogReceiptService,
)
from weftmark.application.ledger import LedgerService


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def snapshot() -> dict[str, object]:
    contents = {
        "source_kind": "frog-agents-db",
        "source_label": "workspace-main",
        "source_schema": {"migrations": ["001_initial.sql"]},
        "records": {
            "repos": [],
            "tasks": [
                {
                    "slug": "task-1",
                    "repo_path": "/workspace/one",
                    "workflow_status": "in_progress",
                    "title": "External task",
                },
                {
                    "slug": "task-2",
                    "repo_path": "/workspace/two",
                    "workflow_status": "done",
                    "title": "Other task",
                },
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
                    "scope_key": "task:task-1",
                    "status": "active",
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


def service(tmp_path: Path) -> tuple[FrogReceiptService, LedgerService]:
    ledger = LedgerService(JsonlLedger(tmp_path / "ledger.jsonl"))
    return FrogReceiptService(ledger), ledger


def test_receipt_is_idempotent_queryable_and_non_authoritative(
    tmp_path: Path,
) -> None:
    receipts, ledger = service(tmp_path)
    source = snapshot()

    first = receipts.record(source, imported_at=NOW)
    repeated = receipts.record(source, imported_at=NOW)

    assert first.imported is True
    assert repeated.imported is False
    assert first.sequence == repeated.sequence == 1
    assert first.receipt.counts["tasks"] == 2
    assert first.receipt.counts["locks"] == 1
    assert receipts.get(first.receipt.digest) == first.receipt
    assert receipts.list() == (first.receipt,)
    assert [task["slug"] for task in receipts.tasks(
        first.receipt.digest,
        repo_path="/workspace/one",
        workflow_status="in_progress",
    ) or ()] == ["task-1"]

    entries = ledger.snapshot()
    assert len(entries) == 1
    assert entries[0].kind == "frog_snapshot"
    assert entries[0].kind != "changeset"
    assert entries[0].kind != "claim"


def test_receipt_rejects_tampering_without_appending(tmp_path: Path) -> None:
    receipts, ledger = service(tmp_path)
    tampered = snapshot()
    tampered["records"]["tasks"][0]["title"] = "changed"  # type: ignore[index]

    with pytest.raises(FrogReceiptError, match="malformed or corrupt"):
        receipts.record(tampered, imported_at=NOW)
    with pytest.raises(FrogReceiptError, match="timezone"):
        receipts.record(snapshot(), imported_at=NOW.replace(tzinfo=None))

    assert ledger.snapshot() == ()
