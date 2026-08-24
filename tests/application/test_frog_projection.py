from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.frog_projection import (
    FROG_TRANSITION_PROJECTION_SCHEMA,
    FrogProjectionError,
    FrogTransitionProjectionService,
    frog_transition_projection_to_payload,
)
from weftmark.application.frog_receipts import FrogReceiptService
from weftmark.application.ledger import LedgerService


CAPTURED = datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)
IMPORTED = CAPTURED + timedelta(minutes=1)


def _task(
    slug: str,
    status: str,
    priority: str,
    created_at: str,
    *,
    repo_path: str = "/project",
    assigned_agent: str | None = None,
) -> dict[str, object]:
    return {
        "slug": slug,
        "repo_path": repo_path,
        "title": slug.replace("-", " ").title(),
        "workflow_status": status,
        "git_status": "in_progress",
        "priority": priority,
        "created_at": created_at,
        "assigned_agent": assigned_agent,
    }


def _snapshot() -> dict[str, object]:
    records = {
        "repos": [],
        "tasks": [
            _task("done-dep", "done", "p0", "1"),
            _task("active", "in_progress", "p1", "2"),
            _task("reviewing", "review", "p0", "3"),
            _task("unknown", "future-state", "p0", "4"),
            _task("blocked", "blocked", "p0", "5"),
            _task("eligible", "todo", "p0", "6", assigned_agent="task-owner"),
            _task("unmet", "todo", "p0", "7"),
            _task("conflicted", "todo", "p1", "8"),
            _task("other-repo", "todo", "p0", "9", repo_path="/other"),
        ],
        "task_dependencies": [
            {"task_slug": "eligible", "depends_on_slug": "done-dep", "relation": "depends_on"},
            {"task_slug": "unmet", "depends_on_slug": "active", "relation": "depends_on"},
        ],
        "task_conflicts": [
            {"task_slug": "conflicted", "conflicts_with_slug": "active", "reason": "contract"}
        ],
        "task_tags": [],
        "task_assignments": [
            {"id": 1, "task_slug": "eligible", "agent_name": "assignment-worker"},
            {"id": 2, "task_slug": "eligible"},
        ],
        "agents": [],
        "files": [],
        "task_files": [],
        "locks": [
            {"id": 7, "scope_key": "task:eligible", "status": "active"},
            {"id": 8, "scope_key": "file:README.md", "status": "active"},
            {"id": 9, "scope_key": "task:eligible"},
        ],
    }
    contents = {
        "source_kind": "frog-agents-db",
        "source_label": "workspace-main",
        "source_schema": {"migrations": ["001_initial.sql"]},
        "records": records,
    }
    canonical = json.dumps(contents, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "schema_version": 1,
        **contents,
        "captured_at": CAPTURED.isoformat(),
        "digest": f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
    }


def _service(tmp_path: Path) -> tuple[FrogTransitionProjectionService, LedgerService, str]:
    ledger = LedgerService(JsonlLedger(tmp_path / "ledger.jsonl"))
    receipts = FrogReceiptService(ledger)
    snapshot = _snapshot()
    receipts.record(snapshot, imported_at=IMPORTED)
    return FrogTransitionProjectionService(receipts), ledger, str(snapshot["digest"])


def test_projection_is_deterministic_explicit_and_read_only(tmp_path: Path) -> None:
    service, ledger, digest = _service(tmp_path)
    before = ledger.snapshot()
    projection = service.project(
        digest,
        repo_path="/project",
        generated_at=CAPTURED + timedelta(hours=2),
        stale_after_seconds=3600,
    )
    payload = frog_transition_projection_to_payload(projection)

    assert payload["schema"] == FROG_TRANSITION_PROJECTION_SCHEMA
    assert payload["source"]["digest"] == digest
    assert payload["source"]["stale"] is True
    assert payload["authority"]["coordination"] == "observation_only"
    assert [card["id"] for card in payload["cards"]] == [
        "active", "reviewing", "unknown", "blocked", "eligible", "unmet",
        "conflicted", "done-dep",
    ]
    cards = {card["id"]: card for card in payload["cards"]}
    assert cards["unknown"]["lane"] == "review"
    assert cards["unknown"]["planning"]["eligible"] is False
    assert cards["unknown"]["attention"] == ["unknown_source_status"]
    assert cards["blocked"]["attention"] == ["blocked"]
    assert cards["unmet"]["attention"] == ["dependencies_unmet"]
    assert cards["conflicted"]["attention"] == ["active_conflict"]
    assert cards["eligible"]["observations"] == {
        "assignments": ["assignment-worker", "task-owner"],
        "locks": [{"id": "7", "status": "active"}],
    }
    assert payload["counts"]["ignored_observations"] == {"assignments": 1, "locks": 1}
    json.dumps(payload, allow_nan=False)
    assert ledger.snapshot() == before


def test_projection_refuses_ambiguous_time_and_corrupt_planning(tmp_path: Path) -> None:
    service, _, digest = _service(tmp_path)
    with pytest.raises(FrogProjectionError, match="timezone"):
        service.project(digest, generated_at=datetime(2026, 8, 24, 2, 0))
    with pytest.raises(FrogProjectionError, match="predates"):
        service.project(digest, generated_at=CAPTURED)
    with pytest.raises(FrogProjectionError, match="positive"):
        service.project(digest, generated_at=IMPORTED, stale_after_seconds=0)
    with pytest.raises(FrogProjectionError, match="not found"):
        service.project("sha256:" + "0" * 64, generated_at=IMPORTED)


def test_projection_normalizes_planning_validation_errors(tmp_path: Path) -> None:
    ledger = LedgerService(JsonlLedger(tmp_path / "ledger.jsonl"))
    receipts = FrogReceiptService(ledger)
    snapshot = _snapshot()
    snapshot["records"]["task_dependencies"][0]["depends_on_slug"] = "missing"  # type: ignore[index]
    contents = {key: snapshot[key] for key in ("source_kind", "source_label", "source_schema", "records")}
    canonical = json.dumps(contents, sort_keys=True, separators=(",", ":"), allow_nan=False)
    snapshot["digest"] = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    receipts.record(snapshot, imported_at=IMPORTED)
    with pytest.raises(FrogProjectionError, match="missing task"):
        FrogTransitionProjectionService(receipts).project(
            str(snapshot["digest"]), generated_at=IMPORTED
        )
