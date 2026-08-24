from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.adapters.weft_plan import WeftPlanAdapter, WeftPlanTask
from weftmark.application.ledger import LedgerService
from weftmark.application.plan_import import (
    PlanImportDriftError,
    PlanImportError,
    PlanImportService,
)
from weftmark.application.tasks import TaskService, TaskServiceError
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskIntent, TaskState


NOW = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)


def _task(
    slug: str,
    *,
    status: str = "todo",
    depends: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
) -> str:
    depends_yaml = "".join(f"\n      - {value}" for value in depends) or " []"
    conflicts_yaml = "".join(f"\n      - {value}" for value in conflicts) or " []"
    return f"""  - slug: {slug}
    title: {slug} title
    status: {status}
    priority: P1
    depends:{depends_yaml}
    conflicts:{conflicts_yaml}
    purpose: Preserve {slug} intent.
    scope:
      files:
        - src/{slug}.py
      contracts:
        - adapter:{slug}-v0
    deliverables:
      - Deliver {slug}.
    accept:
      - {slug} is inspectable.
    negative:
      - Runtime authority is not inferred.
    evidence:
      - kind: test
        command: python -m pytest
"""


def _snapshot(tmp_path: Path):
    path = tmp_path / "tasks" / "plan.weft.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "format: weft-task-v0\nphase: test\nsummary: Test plan.\ntasks:\n"
        + _task("done-base", status="done")
        + _task("idea", status="idea")
        + _task(
            "active",
            status="in_progress",
            depends=("done-base", "idea"),
            conflicts=("peer",),
        )
        + _task("peer"),
        encoding="utf-8",
    )
    return WeftPlanAdapter(tmp_path).load(), path


def _service(tmp_path: Path):
    ledger = LedgerService(JsonlLedger(tmp_path / "ledger.jsonl"))
    tasks = TaskService(ledger)
    return PlanImportService(tasks, ledger), tasks, ledger


def test_import_is_idempotent_and_does_not_import_source_runtime_authority(
    tmp_path: Path,
) -> None:
    snapshot, _ = _snapshot(tmp_path)
    service, tasks, ledger = _service(tmp_path)

    first = service.import_snapshot(
        snapshot,
        source_label="workspace/main",
        imported_at=NOW,
    )
    before_repeat = ledger.snapshot()
    repeated = service.import_snapshot(
        snapshot,
        source_label="workspace/main",
        imported_at=NOW + timedelta(hours=1),
    )

    assert first.imported is True
    assert first.created_tasks == ("active", "idea", "peer")
    assert first.skipped_terminal_tasks == ("done-base",)
    assert first.satisfied_source_dependencies == (("active", "done-base"),)
    assert first.created_dependencies == (("active", "idea"),)
    assert first.created_conflicts == (("active", "peer"),)
    assert tasks.require("active").state is TaskState.TODO
    assert tasks.require("idea").state is TaskState.IDEA
    assert tasks.get("done-base") is None
    assert tasks.require("active").scopes == (
        "contract:adapter/active-v0",
        "file:src/active.py",
    )
    assert repeated.imported is False
    assert repeated.existing_tasks == ("active", "idea", "peer")
    assert ledger.snapshot() == before_repeat


def test_import_reports_semantic_and_file_drift_without_mutation(tmp_path: Path) -> None:
    snapshot, path = _snapshot(tmp_path)
    service, _, ledger = _service(tmp_path)
    service.import_snapshot(snapshot, source_label="workspace/main", imported_at=NOW)
    before = ledger.snapshot()
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "active title", "changed active title"
        ),
        encoding="utf-8",
    )
    changed = WeftPlanAdapter(tmp_path).load()

    with pytest.raises(PlanImportDriftError) as raised:
        service.import_snapshot(
            changed,
            source_label="workspace/main",
            imported_at=NOW + timedelta(minutes=1),
        )

    assert raised.value.drift.changed_tasks == ("active",)
    assert raised.value.drift.changed_files == ("tasks/plan.weft.yml",)
    assert ledger.snapshot() == before


def test_import_recovers_matching_existing_runtime_state_without_overwrite(
    tmp_path: Path,
) -> None:
    snapshot, _ = _snapshot(tmp_path)
    service, tasks, _ = _service(tmp_path)
    active_source = next(task for task in snapshot.tasks if task.slug == "active")
    tasks.create(_native(active_source, created_at=NOW - timedelta(hours=1)))
    tasks.transition(
        "active",
        state=TaskState.IN_PROGRESS,
        actor_id="existing-worker",
        rationale="existing native claim",
        occurred_at=NOW - timedelta(minutes=30),
    )

    result = service.import_snapshot(
        snapshot,
        source_label="workspace/main",
        imported_at=NOW,
    )

    assert result.existing_tasks == ("active",)
    assert result.created_tasks == ("idea", "peer")
    assert tasks.require("active").state is TaskState.IN_PROGRESS


def test_import_refuses_mismatched_existing_intent_before_writing(tmp_path: Path) -> None:
    snapshot, _ = _snapshot(tmp_path)
    service, tasks, ledger = _service(tmp_path)
    active_source = next(task for task in snapshot.tasks if task.slug == "active")
    existing = _native(active_source, created_at=NOW)
    tasks.create(
        TaskIntent.create(
            id=existing.id,
            title="different title",
            why=existing.why,
            what=existing.what,
            roi_note=None,
            priority=existing.priority,
            state=TaskState.TODO,
            scopes=tuple(Scope.parse(value) for value in existing.scopes),
            created_at=NOW,
        )
    )
    before = ledger.snapshot()

    with pytest.raises(PlanImportError, match="different immutable intent"):
        service.import_snapshot(
            snapshot,
            source_label="workspace/main",
            imported_at=NOW,
        )

    assert ledger.snapshot() == before


def test_import_refuses_ambiguous_identity_and_time(tmp_path: Path) -> None:
    snapshot, _ = _snapshot(tmp_path)
    service, _, _ = _service(tmp_path)

    with pytest.raises(PlanImportError, match="source_label"):
        service.import_snapshot(snapshot, source_label="../workspace", imported_at=NOW)
    with pytest.raises(PlanImportError, match="timezone"):
        service.import_snapshot(
            snapshot,
            source_label="workspace/main",
            imported_at=NOW.replace(tzinfo=None),
        )


def test_weftmark_imports_its_own_reviewed_source_plan(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    snapshot = WeftPlanAdapter(repository).load()
    service, tasks, _ = _service(tmp_path)

    result = service.import_snapshot(
        snapshot,
        source_label="weftmark/main",
        imported_at=NOW,
    )

    actionable = {task.slug for task in snapshot.tasks if task.status != "done"}
    assert result.imported is True
    assert {task.id for task in tasks.list()} == actionable
    assert "source-plan-native-import-core" in result.skipped_terminal_tasks
    assert "source-plan-native-import" in result.skipped_terminal_tasks
    assert tasks.get("source-plan-native-import-core") is None
    assert tasks.get("source-plan-native-import") is None
    assert tasks.require("frog-native-task-promotion-core").state is TaskState.TODO


def test_import_normalizes_ledger_refusal_without_partial_receipt(tmp_path: Path) -> None:
    snapshot, path = _snapshot(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Preserve active intent.", "token=live-value"
        ),
        encoding="utf-8",
    )
    snapshot = WeftPlanAdapter(tmp_path).load()
    service, _, ledger = _service(tmp_path)

    with pytest.raises(PlanImportError, match="secret"):
        service.import_snapshot(
            snapshot,
            source_label="workspace/main",
            imported_at=NOW,
        )

    assert ledger.snapshot() == ()


def test_import_rejects_malformed_existing_receipt(tmp_path: Path) -> None:
    snapshot, _ = _snapshot(tmp_path)
    service, _, ledger = _service(tmp_path)
    ledger.record(
        kind="source_plan_import",
        entity_id="workspace/main",
        payload={"schema_version": 1, "source_label": "workspace/main"},
        recorded_at=NOW,
    )

    with pytest.raises(PlanImportError, match="receipt is malformed"):
        service.import_snapshot(
            snapshot,
            source_label="workspace/main",
            imported_at=NOW,
        )


def test_import_recovers_matching_concurrent_task_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _ = _snapshot(tmp_path)
    service, tasks, _ = _service(tmp_path)
    create = tasks.create

    def create_then_report_collision(task: TaskIntent) -> TaskIntent:
        create(task)
        raise TaskServiceError(f"Task already exists: {task.id}")

    monkeypatch.setattr(tasks, "create", create_then_report_collision)

    result = service.import_snapshot(
        snapshot,
        source_label="workspace/main",
        imported_at=NOW,
    )

    assert result.created_tasks == ()
    assert result.existing_tasks == ("active", "idea", "peer")


def _native(task: WeftPlanTask, *, created_at: datetime) -> TaskIntent:
    return TaskIntent.create(
        id=task.slug,
        title=task.title,
        why=task.purpose,
        what="\n".join(task.deliverables),
        roi_note=None,
        priority=task.priority,
        state=TaskState.TODO,
        scopes=tuple(Scope.parse(value) for value in task.scopes),
        created_at=created_at,
    )
