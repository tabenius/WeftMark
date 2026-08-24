from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.frog_receipts import FrogReceiptService, FrogSnapshotReceipt
from weftmark.application.frog_task_import import (
    FrogTaskImportError,
    FrogTaskImportService,
)
from weftmark.application.ledger import LedgerService
from weftmark.application.tasks import TaskService
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskState


NOW = datetime(2026, 8, 24, 3, 0, tzinfo=timezone.utc)


def task(
    slug: str,
    *,
    status: str = "todo",
    source: str | None = "human",
    title: str | None = None,
) -> dict[str, object]:
    return {
        "slug": slug,
        "repo_path": "/source/project",
        "title": title or slug.replace("-", " ").title(),
        "why": f"Why {slug}",
        "what_text": f"Build {slug}",
        "roi_note": None,
        "priority": "P1",
        "workflow_status": status,
        "assigned_agent": "frog-worker",
        "source": source,
        "external_id": None,
    }


def snapshot(
    tasks: list[dict[str, object]],
    *,
    dependencies: list[tuple[str, str]] | None = None,
    conflicts: list[tuple[str, str]] | None = None,
    source_label: str = "workspace-main",
) -> dict[str, object]:
    contents = {
        "source_kind": "frog-agents-db",
        "source_label": source_label,
        "source_schema": {"migrations": ["001_initial.sql"]},
        "records": {
            "repos": [],
            "tasks": tasks,
            "task_dependencies": [
                {
                    "task_slug": left,
                    "depends_on_slug": right,
                    "relation": "depends_on",
                }
                for left, right in dependencies or []
            ],
            "task_conflicts": [
                {"task_slug": left, "conflicts_with_slug": right, "reason": "shared contract"}
                for left, right in conflicts or []
            ],
            "task_tags": [],
            "task_assignments": [
                {"id": 1, "task_slug": tasks[0]["slug"], "agent_name": "frog-worker", "active": 1}
            ] if tasks else [],
            "agents": [{"name": "frog-worker", "kind": "agent"}],
            "files": [],
            "task_files": [],
            "locks": [{"id": 1, "scope_key": "task:any", "status": "active"}],
        },
    }
    canonical = json.dumps(contents, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "schema_version": 1,
        **contents,
        "captured_at": NOW.isoformat(),
        "digest": f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
    }


def services(tmp_path: Path) -> tuple[FrogTaskImportService, FrogReceiptService, TaskService, LedgerService]:
    ledger = LedgerService(JsonlLedger(tmp_path / "ledger.jsonl"))
    receipts = FrogReceiptService(ledger)
    tasks = TaskService(ledger)
    return FrogTaskImportService(receipts, tasks, ledger), receipts, tasks, ledger


def record(receipts: FrogReceiptService, value: dict[str, object]) -> str:
    result = receipts.record(value, imported_at=NOW)
    return result.receipt.digest


def test_imports_selected_graph_idempotently_without_runtime_authority(tmp_path: Path) -> None:
    service, receipts, tasks, ledger = services(tmp_path)
    value = snapshot(
        [task("core", status="in_progress"), task("ui"), task("legacy", status="done")],
        dependencies=[("ui", "core"), ("core", "legacy")],
        conflicts=[("core", "ui")],
    )
    digest = record(receipts, value)
    scopes = {
        "core": (Scope.file("src/core/**"), Scope.contract("frog-import-v0")),
        "ui": (Scope.file("src/ui/**"),),
    }

    first = service.import_tasks(
        digest, ["ui", "legacy", "core"], scopes_by_task=scopes, imported_at=NOW
    )
    entry_count = len(ledger.snapshot())
    repeated = service.import_tasks(
        digest, ["core", "legacy", "ui"], scopes_by_task=scopes, imported_at=NOW
    )

    assert first.imported is True
    assert first.created_tasks == ("core", "ui")
    assert first.created_dependencies == (("ui", "core"),)
    assert first.created_conflicts == (("core", "ui"),)
    assert first.skipped_terminal_tasks == ("legacy",)
    assert first.satisfied_source_dependencies == (("core", "legacy"),)
    assert repeated.imported is False
    assert repeated.created_tasks == ()
    assert repeated.existing_tasks == ("core", "ui")
    assert len(ledger.snapshot()) == entry_count
    assert tasks.require("core").state is TaskState.TODO
    assert tasks.require("ui").state is TaskState.TODO
    assert tasks.require("core").state_events == ()
    assert tasks.get("legacy") is None
    assert not any(entry.kind in {"claim", "changeset"} for entry in ledger.snapshot())


def test_idea_is_the_only_source_state_preserved(tmp_path: Path) -> None:
    service, receipts, tasks, _ = services(tmp_path)
    digest = record(receipts, snapshot([task("idea", status="idea")]))
    service.import_tasks(
        digest,
        ["idea"],
        scopes_by_task={"idea": (Scope.file("docs/**"),)},
        imported_at=NOW,
    )
    assert tasks.require("idea").state is TaskState.IDEA


def test_accepts_sparse_legacy_prose_and_unknown_priority_conservatively(
    tmp_path: Path,
) -> None:
    service, receipts, tasks, _ = services(tmp_path)
    value = task("legacy", status="review")
    value["why"] = None
    value["what_text"] = "  "
    value["priority"] = "urgent"
    digest = record(receipts, snapshot([value]))
    service.import_tasks(
        digest,
        ["legacy"],
        scopes_by_task={"legacy": (Scope.file("legacy/**"),)},
        imported_at=NOW,
    )
    imported = tasks.require("legacy")
    assert imported.why == imported.title
    assert imported.what == imported.title
    assert imported.priority.value == "p3"
    assert imported.state is TaskState.TODO


@pytest.mark.parametrize("source", ["weftmark", "weftmark:native", "WeftMark-export"])
def test_refuses_weftmark_origin_to_prevent_import_export_loops(
    tmp_path: Path, source: str
) -> None:
    service, receipts, _, ledger = services(tmp_path)
    digest = record(receipts, snapshot([task("loop", source=source)]))
    before = len(ledger.snapshot())
    with pytest.raises(FrogTaskImportError, match="originated from WeftMark"):
        service.import_tasks(
            digest,
            ["loop"],
            scopes_by_task={"loop": (Scope.file("src/**"),)},
            imported_at=NOW,
        )
    assert len(ledger.snapshot()) == before


def test_refuses_incomplete_relation_closure_and_scope_mapping(tmp_path: Path) -> None:
    service, receipts, _, ledger = services(tmp_path)
    digest = record(
        receipts,
        snapshot([task("a"), task("b")], dependencies=[("a", "b")]),
    )
    before = len(ledger.snapshot())
    with pytest.raises(FrogTaskImportError, match="unselected dependency"):
        service.import_tasks(
            digest,
            ["a"],
            scopes_by_task={"a": (Scope.file("a/**"),)},
            imported_at=NOW,
        )
    with pytest.raises(FrogTaskImportError, match="scope mapping"):
        service.import_tasks(
            digest,
            ["a", "b"],
            scopes_by_task={"a": (Scope.file("a/**"),)},
            imported_at=NOW,
        )
    with pytest.raises(FrogTaskImportError, match="task_slugs must be"):
        service.import_tasks(
            digest,
            "a",  # type: ignore[arg-type]
            scopes_by_task={"a": (Scope.file("a/**"),)},
            imported_at=NOW,
        )
    assert len(ledger.snapshot()) == before


def test_refuses_drift_conflicting_retry_and_existing_intent(tmp_path: Path) -> None:
    service, receipts, tasks, ledger = services(tmp_path)
    first = snapshot([task("a")])
    digest = record(receipts, first)
    service.import_tasks(
        digest,
        ["a"],
        scopes_by_task={"a": (Scope.file("a/**"),)},
        imported_at=NOW,
    )
    before = len(ledger.snapshot())
    with pytest.raises(FrogTaskImportError, match="different native scopes"):
        service.import_tasks(
            digest,
            ["a"],
            scopes_by_task={"a": (Scope.file("other/**"),)},
            imported_at=NOW,
        )

    changed = snapshot([task("a", title="Changed")])
    changed_digest = record(receipts, changed)
    with pytest.raises(FrogTaskImportError, match="drift reconciliation"):
        service.import_tasks(
            changed_digest,
            ["a"],
            scopes_by_task={"a": (Scope.file("a/**"),)},
            imported_at=NOW + timedelta(minutes=1),
        )
    assert len(ledger.snapshot()) == before + 1  # the distinct immutable snapshot receipt only
    assert tasks.require("a").title == "A"


def test_refuses_conflicting_existing_native_intent_before_reservation(
    tmp_path: Path,
) -> None:
    service, receipts, tasks, ledger = services(tmp_path)
    first_digest = record(
        receipts,
        snapshot([task("a")], source_label="first-workspace"),
    )
    service.import_tasks(
        first_digest,
        ["a"],
        scopes_by_task={"a": (Scope.file("a/**"),)},
        imported_at=NOW,
    )
    second_digest = record(
        receipts,
        snapshot([task("a", title="Different")], source_label="second-workspace"),
    )
    before = len(ledger.snapshot())
    with pytest.raises(FrogTaskImportError, match="different immutable intent"):
        service.import_tasks(
            second_digest,
            ["a"],
            scopes_by_task={"a": (Scope.file("a/**"),)},
            imported_at=NOW,
        )
    assert len(ledger.snapshot()) == before
    assert tasks.require("a").title == "A"


def test_refuses_secret_shaped_prose_before_reservation(tmp_path: Path) -> None:
    _, _, tasks, ledger = services(tmp_path)
    value = task("secret")
    value["what_text"] = "token=do-not-store"
    source = snapshot([value])
    digest = str(source["digest"])

    class UnsafeReceiptSource:
        def get(self, requested_digest: str):
            assert requested_digest == digest
            return FrogSnapshotReceipt(
                digest,
                "workspace-main",
                NOW,
                NOW,
                {},
                source,
            )

    service = FrogTaskImportService(UnsafeReceiptSource(), tasks, ledger)  # type: ignore[arg-type]
    before = len(ledger.snapshot())
    with pytest.raises(FrogTaskImportError, match="secret-shaped"):
        service.import_tasks(
            digest,
            ["secret"],
            scopes_by_task={"secret": (Scope.file("src/**"),)},
            imported_at=NOW,
        )
    assert tasks.get("secret") is None
    assert len(ledger.snapshot()) == before


def test_reserved_import_recovers_after_partial_task_creation(tmp_path: Path) -> None:
    service, receipts, tasks, ledger = services(tmp_path)
    digest = record(receipts, snapshot([task("a"), task("b")]))
    scopes = {"a": (Scope.file("a/**"),), "b": (Scope.file("b/**"),)}

    original_create = tasks.create
    calls = 0

    def interrupted(intent):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated interruption")
        return original_create(intent)

    tasks.create = interrupted  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated interruption"):
        service.import_tasks(digest, ["a", "b"], scopes_by_task=scopes, imported_at=NOW)

    recovered_tasks = TaskService(ledger)
    recovered = FrogTaskImportService(receipts, recovered_tasks, ledger).import_tasks(
        digest, ["a", "b"], scopes_by_task=scopes, imported_at=NOW
    )
    assert recovered.imported is False
    assert recovered.created_tasks == ("b",)
    assert recovered.existing_tasks == ("a",)
    assert {value.id for value in recovered_tasks.list()} == {"a", "b"}
