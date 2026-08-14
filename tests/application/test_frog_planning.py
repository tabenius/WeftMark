from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.frog_planning import (
    FrogPlanningError,
    FrogPlanningService,
    selection_to_payload,
)
from weftmark.application.frog_receipts import FrogReceiptService
from weftmark.application.ledger import LedgerService


NOW = datetime(2026, 8, 14, 14, 0, tzinfo=timezone.utc)


def snapshot() -> dict[str, object]:
    tasks = [
        task("done-dep", "done", "p0", created="1"),
        task("eligible-high", "idea", "p0", created="2"),
        task("unmet", "idea", "p0", created="3"),
        task("active-other", "in_progress", "p3", created="4", repo="/other"),
        task("conflicted", "idea", "p1", created="5"),
        task(
            "eligible-low",
            "in_progress",
            "p2",
            created="6",
            assigned_agent="external-worker",
            git_status="done",
        ),
        task("blocked", "blocked", "p0", created="7"),
    ]
    records = {
        "repos": [],
        "tasks": tasks,
        "task_dependencies": [
            {
                "task_slug": "eligible-high",
                "depends_on_slug": "done-dep",
                "relation": "depends_on",
            },
            {
                "task_slug": "unmet",
                "depends_on_slug": "active-other",
                "relation": "depends_on",
            },
            {
                "task_slug": "eligible-low",
                "depends_on_slug": "unlisted-document",
                "relation": "review-input",
            },
        ],
        "task_conflicts": [
            {
                "task_slug": "conflicted",
                "conflicts_with_slug": "active-other",
                "reason": "shared contract",
            }
        ],
        "task_tags": [],
        "task_assignments": [
            {"id": 1, "task_slug": "eligible-low", "agent_name": "external-worker"}
        ],
        "agents": [],
        "files": [],
        "task_files": [],
        "locks": [
            {"id": 1, "scope_key": "task:eligible-low", "status": "active"}
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


def task(
    slug: str,
    status: str,
    priority: str,
    *,
    created: str,
    repo: str = "/project",
    assigned_agent: str | None = None,
    git_status: str = "in_progress",
) -> dict[str, object]:
    return {
        "slug": slug,
        "repo_path": repo,
        "title": slug.replace("-", " ").title(),
        "workflow_status": status,
        "git_status": git_status,
        "priority": priority,
        "created_at": created,
        "assigned_agent": assigned_agent,
    }


def service(tmp_path: Path) -> tuple[FrogPlanningService, LedgerService, str]:
    ledger = LedgerService(JsonlLedger(tmp_path / "ledger.jsonl"))
    receipts = FrogReceiptService(ledger)
    source = snapshot()
    receipts.record(source, imported_at=NOW)
    return FrogPlanningService(receipts), ledger, str(source["digest"])


def test_next_ranks_dependency_eligible_intent_without_importing_authority(
    tmp_path: Path,
) -> None:
    planning, ledger, digest = service(tmp_path)
    before = ledger.snapshot()

    selection = planning.next(digest, repo_path="/project", limit=2)

    assert [value.task["slug"] for value in selection.tasks] == [
        "eligible-high",
        "eligible-low",
    ]
    assert selection.considered == 6
    assert selection.eligible == 2
    assert selection.ignored_lock_observations == 1
    assert selection.ignored_assignment_observations == 1
    assert planning.eligibility(digest, "eligible-high").eligible is True
    assert planning.eligibility(digest, "unmet").eligible is False
    reasons = {value.task["slug"]: value.reasons for value in selection.skipped}
    assert reasons["unmet"] == ("dependencies not done: active-other",)
    assert reasons["conflicted"] == (
        "source conflicts in progress: active-other",
    )
    assert reasons["blocked"] == ("source status is blocked: blocked",)
    payload = selection_to_payload(selection)
    assert all(set(item) == {"slug", "reasons"} for item in payload["skipped"])
    assert payload["authority"].startswith("advisory imported intent")
    assert ledger.snapshot() == before
    assert not any(entry.kind in {"changeset", "claim"} for entry in before)


def test_next_refuses_invalid_limits_and_missing_hard_relations(tmp_path: Path) -> None:
    planning, _, digest = service(tmp_path)
    with pytest.raises(FrogPlanningError, match="limit"):
        planning.next(digest, limit=0)
    with pytest.raises(FrogPlanningError, match="snapshot not found"):
        planning.next("sha256:" + "0" * 64)

    ledger = LedgerService(JsonlLedger(tmp_path / "broken.jsonl"))
    receipts = FrogReceiptService(ledger)
    broken = snapshot()
    broken["records"]["task_dependencies"][0]["depends_on_slug"] = "missing"  # type: ignore[index]
    contents = {
        key: broken[key]
        for key in ("source_kind", "source_label", "source_schema", "records")
    }
    canonical = json.dumps(
        contents, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    broken["digest"] = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    receipts.record(broken, imported_at=NOW)
    with pytest.raises(FrogPlanningError, match="missing task"):
        FrogPlanningService(receipts).next(str(broken["digest"]))
