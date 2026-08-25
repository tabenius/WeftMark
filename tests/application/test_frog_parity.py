from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.claims import ClaimService
from weftmark.application.frog_parity import (
    FROG_PARITY_SCHEMA,
    FrogParityError,
    FrogParityService,
    frog_parity_to_payload,
)
from weftmark.application.frog_receipts import FrogReceiptService
from weftmark.application.frog_task_import import FrogTaskImportService
from weftmark.application.ledger import LedgerService
from weftmark.application.task_claims import TaskClaimService
from weftmark.application.task_planning import TaskPlanningService
from weftmark.application.tasks import TaskService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def git(path: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(path), *args), check=True, capture_output=True)


def snapshot(*, with_lock: bool = False) -> dict[str, object]:
    tasks = [
        {
            "slug": "core",
            "repo_path": "/source/project",
            "title": "Core",
            "why": "Core first",
            "what_text": "Build core",
            "roi_note": None,
            "priority": "p0",
            "workflow_status": "todo",
            "git_status": "not_started",
            "assigned_agent": None,
            "source": "human",
            "external_id": None,
            "created_at": NOW.isoformat(),
        },
        {
            "slug": "ui",
            "repo_path": "/source/project",
            "title": "UI",
            "why": "Expose core",
            "what_text": "Build UI",
            "roi_note": None,
            "priority": "p1",
            "workflow_status": "todo",
            "git_status": "not_started",
            "assigned_agent": None,
            "source": "human",
            "external_id": None,
            "created_at": (NOW + timedelta(seconds=1)).isoformat(),
        },
    ]
    contents = {
        "source_kind": "frog-agents-db",
        "source_label": "workspace-main",
        "source_schema": {"migrations": ["001_initial.sql"]},
        "records": {
            "repos": [],
            "tasks": tasks,
            "task_dependencies": [
                {"task_slug": "ui", "depends_on_slug": "core", "relation": "depends_on"}
            ],
            "task_conflicts": [],
            "task_tags": [],
            "task_assignments": [],
            "agents": [],
            "files": [],
            "task_files": (
                [
                    {
                        "task_slug": "core",
                        "file_path": "/source/project/src/core/value.py",
                        "role": "edit",
                    }
                ]
                if with_lock
                else []
            ),
            "locks": (
                [
                    {
                        "id": 7,
                        "scope_key": "task:core",
                        "status": "active",
                        "started_at": NOW.isoformat(),
                        "lease_seconds": 600,
                        "repo_path": "/source/project",
                        "file_paths": ["/source/project/src/core/value.py"],
                    }
                ]
                if with_lock
                else []
            ),
        },
    }
    canonical = json.dumps(contents, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 1,
        **contents,
        "captured_at": NOW.isoformat(),
        "digest": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
    }


def services(tmp_path: Path):
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    ledger = LedgerService(JsonlLedger(tmp_path / ".git" / "weftmark" / "ledger.jsonl"))
    receipts = FrogReceiptService(ledger)
    tasks = TaskService(ledger)
    workspace = WorkspaceService(LocalGit(tmp_path), ledger)
    claims = ClaimService(workspace, ledger)
    return (
        FrogParityService(receipts, tasks, workspace, claims, ledger),
        FrogTaskImportService(receipts, tasks, ledger),
        receipts,
        tasks,
        workspace,
        claims,
        ledger,
    )


class AdvancingReadLedger:
    def __init__(self, inner: LedgerService) -> None:
        self.inner = inner
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_calls == 2:
            self.inner.record(
                kind="test_observation",
                entity_id="concurrent-write",
                payload={"value": "advanced"},
                recorded_at=NOW + timedelta(seconds=4),
            )
        return self.inner.snapshot()

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def test_report_matches_imported_graph_and_refuses_to_invent_behavioral_proof(
    tmp_path: Path,
) -> None:
    parity, importer, receipts, _, _, _, ledger = services(tmp_path)
    source = snapshot()
    digest = receipts.record(source, imported_at=NOW + timedelta(seconds=2)).receipt.digest
    importer.import_tasks(
        digest,
        ["core", "ui"],
        scopes_by_task={
            "core": (Scope.file("src/core/**"),),
            "ui": (Scope.file("src/ui/**"),),
        },
        imported_at=NOW + timedelta(seconds=3),
    )
    before = ledger.snapshot()

    payload = frog_parity_to_payload(
        parity.compare(
            digest,
            observed_at=NOW + timedelta(minutes=5),
            repo_path="/source/project",
        )
    )

    assert payload["schema"] == FROG_PARITY_SCHEMA
    assert payload["authority"]["mode"] == "read_only_comparison"
    assert payload["authority"]["native_ledger_digest"] == before[-1].digest
    assert payload["authority"]["native_ledger_sequence"] == before[-1].sequence
    assert payload["cutover_ready"] is False
    checks = {value["id"]: value for value in payload["checks"]}
    assert checks["source_freshness"]["classification"] == "match"
    assert checks["task_graph"]["classification"] == "match"
    assert checks["eligibility"]["classification"] == "match"
    assert checks["collision_refusal"]["classification"] == "unavailable"
    assert checks["stale_lease"]["classification"] == "unavailable"
    assert checks["scope_audit"]["classification"] == "unavailable"
    assert checks["completion_gate"]["classification"] == "unavailable"
    assert ledger.snapshot() == before
    json.dumps(payload, allow_nan=False)


def test_report_exposes_stale_source_and_native_graph_drift_as_blockers(
    tmp_path: Path,
) -> None:
    parity, importer, receipts, tasks, _, _, _ = services(tmp_path)
    source = snapshot()
    digest = receipts.record(source, imported_at=NOW + timedelta(seconds=2)).receipt.digest
    importer.import_tasks(
        digest,
        ["core", "ui"],
        scopes_by_task={
            "core": (Scope.file("src/core/**"),),
            "ui": (Scope.file("src/ui/**"),),
        },
        imported_at=NOW + timedelta(seconds=3),
    )
    tasks.add_conflict(
        "core", "ui", reason="local drift", created_at=NOW + timedelta(seconds=4)
    )

    payload = frog_parity_to_payload(
        parity.compare(
            digest,
            observed_at=NOW + timedelta(hours=2),
            repo_path="/source/project",
            stale_after_seconds=60,
        )
    )

    checks = {value["id"]: value for value in payload["checks"]}
    assert checks["source_freshness"]["classification"] == "blocker"
    assert checks["task_graph"]["classification"] == "blocker"
    assert checks["task_graph"]["weftmark"]["conflicts_match"] is False


def test_report_compares_paired_lease_and_scope_audit_observations(
    tmp_path: Path,
) -> None:
    parity, importer, receipts, tasks, workspace, claims, ledger = services(tmp_path)
    source = snapshot(with_lock=True)
    digest = receipts.record(source, imported_at=NOW + timedelta(seconds=2)).receipt.digest
    importer.import_tasks(
        digest,
        ["core", "ui"],
        scopes_by_task={
            "core": (Scope.file("src/core/**"),),
            "ui": (Scope.file("src/ui/**"),),
        },
        imported_at=NOW + timedelta(seconds=3),
    )
    TaskClaimService(
        TaskPlanningService(tasks), tasks, workspace, claims, ledger
    ).claim(
        "core",
        change_set_id="core-cs",
        claim_id="core-claim",
        base_revision="HEAD",
        agent_id="worker",
        session_id="session",
        claimed_at=NOW + timedelta(seconds=4),
        lease_seconds=600,
    )

    payload = frog_parity_to_payload(
        parity.compare(
            digest,
            observed_at=NOW + timedelta(minutes=5),
            repo_path="/source/project",
        )
    )

    checks = {value["id"]: value for value in payload["checks"]}
    assert checks["stale_lease"]["classification"] == "match"
    assert checks["scope_audit"]["classification"] == "match"


def test_repo_filter_limits_both_source_and_provenance_bound_native_graph(
    tmp_path: Path,
) -> None:
    parity, importer, receipts, _, _, _, _ = services(tmp_path)
    source = snapshot()
    source["records"]["tasks"][1]["repo_path"] = "/source/other"
    contents = {
        "source_kind": source["source_kind"],
        "source_label": source["source_label"],
        "source_schema": source["source_schema"],
        "records": source["records"],
    }
    canonical = json.dumps(contents, sort_keys=True, separators=(",", ":"))
    source["digest"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    digest = receipts.record(source, imported_at=NOW + timedelta(seconds=2)).receipt.digest
    importer.import_tasks(
        digest,
        ["core", "ui"],
        scopes_by_task={
            "core": (Scope.file("src/core/**"),),
            "ui": (Scope.file("src/ui/**"),),
        },
        imported_at=NOW + timedelta(seconds=3),
    )

    payload = frog_parity_to_payload(
        parity.compare(
            digest,
            observed_at=NOW + timedelta(minutes=5),
            repo_path="/source/project",
        )
    )

    graph = next(value for value in payload["checks"] if value["id"] == "task_graph")
    assert graph["classification"] == "match"
    assert graph["frog"] == {
        "selected_tasks": 1,
        "dependencies": 0,
        "conflicts": 0,
    }


def test_report_rejects_unknown_or_future_source_lock_time(
    tmp_path: Path,
) -> None:
    for status, started_at in (
        ("corrupt", NOW.isoformat()),
        ("active", (NOW + timedelta(minutes=1)).isoformat()),
    ):
        case_path = tmp_path / status
        case_path.mkdir()
        parity, importer, receipts, _, _, _, _ = services(case_path)
        source = snapshot(with_lock=True)
        source["records"]["locks"][0]["status"] = status
        source["records"]["locks"][0]["started_at"] = started_at
        contents = {
            "source_kind": source["source_kind"],
            "source_label": source["source_label"],
            "source_schema": source["source_schema"],
            "records": source["records"],
        }
        canonical = json.dumps(contents, sort_keys=True, separators=(",", ":"))
        source["digest"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        digest = receipts.record(
            source, imported_at=NOW + timedelta(seconds=2)
        ).receipt.digest
        importer.import_tasks(
            digest,
            ["core", "ui"],
            scopes_by_task={
                "core": (Scope.file("src/core/**"),),
                "ui": (Scope.file("src/ui/**"),),
            },
            imported_at=NOW + timedelta(seconds=3),
        )

        with pytest.raises(FrogParityError):
            parity.compare(
                digest,
                observed_at=NOW + timedelta(minutes=5),
                repo_path="/source/project",
            )


def test_report_retries_if_native_ledger_advances_during_comparison(
    tmp_path: Path,
) -> None:
    parity, importer, receipts, _, _, _, ledger = services(tmp_path)
    source = snapshot()
    digest = receipts.record(source, imported_at=NOW + timedelta(seconds=2)).receipt.digest
    importer.import_tasks(
        digest,
        ["core", "ui"],
        scopes_by_task={
            "core": (Scope.file("src/core/**"),),
            "ui": (Scope.file("src/ui/**"),),
        },
        imported_at=NOW + timedelta(seconds=3),
    )
    advancing = AdvancingReadLedger(ledger)
    parity._ledger = advancing

    payload = frog_parity_to_payload(
        parity.compare(digest, observed_at=NOW + timedelta(minutes=5))
    )

    head = ledger.snapshot()[-1]
    assert advancing.snapshot_calls >= 4
    assert payload["authority"]["native_ledger_digest"] == head.digest
    assert payload["authority"]["native_ledger_sequence"] == head.sequence


def test_scope_audit_rejects_source_paths_outside_the_task_repository(
    tmp_path: Path,
) -> None:
    parity, importer, receipts, tasks, workspace, claims, ledger = services(tmp_path)
    source = snapshot(with_lock=True)
    outside = "/source/other/outside.py"
    source["records"]["task_files"][0]["file_path"] = outside
    source["records"]["locks"][0]["file_paths"] = [outside]
    contents = {
        "source_kind": source["source_kind"],
        "source_label": source["source_label"],
        "source_schema": source["source_schema"],
        "records": source["records"],
    }
    canonical = json.dumps(contents, sort_keys=True, separators=(",", ":"))
    source["digest"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    digest = receipts.record(source, imported_at=NOW + timedelta(seconds=2)).receipt.digest
    importer.import_tasks(
        digest,
        ["core", "ui"],
        scopes_by_task={
            "core": (Scope.file("src/core/**"),),
            "ui": (Scope.file("src/ui/**"),),
        },
        imported_at=NOW + timedelta(seconds=3),
    )
    TaskClaimService(
        TaskPlanningService(tasks), tasks, workspace, claims, ledger
    ).claim(
        "core",
        change_set_id="core-cs",
        claim_id="core-claim",
        base_revision="HEAD",
        agent_id="worker",
        session_id="session",
        claimed_at=NOW + timedelta(seconds=4),
        lease_seconds=600,
    )

    with pytest.raises(FrogParityError, match="escapes its repository"):
        parity.compare(
            digest,
            observed_at=NOW + timedelta(minutes=5),
            repo_path="/source/project",
        )
