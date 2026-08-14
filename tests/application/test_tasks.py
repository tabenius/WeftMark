from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import LedgerDraft
from weftmark.application.tasks import (
    TaskService,
    TaskServiceError,
    conflict_from_payload,
    conflict_to_payload,
    dependency_from_payload,
    dependency_to_payload,
    task_from_payload,
    task_to_payload,
)
from weftmark.domain.scope import Scope
from weftmark.domain.task import (
    TaskConflict,
    TaskDependency,
    TaskIntent,
    TaskPriority,
    TaskState,
)


NOW = datetime(2026, 8, 14, 17, 0, tzinfo=timezone.utc)


class InterleavingLedger:
    def __init__(self, inner: JsonlLedger, pending: LedgerDraft) -> None:
        self.inner = inner
        self.pending = pending

    def append(self, draft: LedgerDraft):
        return self.inner.append(draft)

    def append_if_head(self, draft: LedgerDraft, *, expected_digest: str):
        if self.pending is not None:
            self.inner.append(self.pending)
            self.pending = None
        return self.inner.append_if_head(draft, expected_digest=expected_digest)

    def entries(self):
        return self.inner.entries()


def task(id: str, *, priority: TaskPriority = TaskPriority.P1) -> TaskIntent:
    return TaskIntent.create(
        id=id,
        title=f"Task {id}",
        why="Native coordination needs durable intent.",
        what="Persist and relate this task.",
        roi_note=None,
        priority=priority,
        state=TaskState.TODO,
        scopes=(Scope.file(f"work/{id}/**"),),
        created_at=NOW,
    )


def service(tmp_path: Path) -> tuple[TaskService, LedgerService]:
    ledger = LedgerService(JsonlLedger(tmp_path / "ledger.jsonl"))
    return TaskService(ledger), ledger


def test_create_list_and_non_terminal_transitions_round_trip(tmp_path: Path) -> None:
    tasks, ledger = service(tmp_path)
    first = tasks.create(task("task-a", priority=TaskPriority.P0))
    second = tasks.create(task("task-b"))

    active = tasks.transition(
        first.id,
        state=TaskState.IN_PROGRESS,
        actor_id="worker-1",
        rationale="local claim acquired",
        occurred_at=NOW + timedelta(minutes=1),
    )

    assert tasks.get("task-a") == active
    assert tasks.require("task-b") == second
    assert tasks.list() == (active, second)
    assert active.state_events[-1].rationale == "local claim acquired"
    before = ledger.snapshot()
    with pytest.raises(TaskServiceError, match="completion workflow"):
        tasks.transition(
            first.id,
            state=TaskState.DONE,
            actor_id="worker-1",
            rationale="tests passed",
            occurred_at=NOW + timedelta(minutes=2),
        )
    assert ledger.snapshot() == before
    with pytest.raises(TaskServiceError, match="Task not found"):
        tasks.require("missing")
    with pytest.raises(TaskServiceError, match="already exists"):
        tasks.create(task("task-a"))
    with pytest.raises(TaskServiceError, match="begin as idea or todo"):
        tasks.create(
            task("already-active").transition(
                TaskState.IN_PROGRESS,
                actor_id="worker",
                rationale="claimed elsewhere",
                occurred_at=NOW + timedelta(minutes=1),
            )
        )


def test_dependencies_are_idempotent_cycle_free_and_reference_existing_tasks(
    tmp_path: Path,
) -> None:
    tasks, ledger = service(tmp_path)
    for id in ("task-a", "task-b", "task-c"):
        tasks.create(task(id))

    first = tasks.add_dependency("task-b", "task-a", created_at=NOW)
    repeated = tasks.add_dependency(
        "task-b", "task-a", created_at=NOW + timedelta(minutes=1)
    )
    tasks.add_dependency("task-c", "task-b", created_at=NOW)

    assert first.created is True
    assert repeated.created is False
    assert repeated.relation == first.relation
    assert [(value.task_id, value.depends_on_task_id) for value in tasks.dependencies()] == [
        ("task-b", "task-a"),
        ("task-c", "task-b"),
    ]
    before = ledger.snapshot()
    with pytest.raises(TaskServiceError, match="cycle"):
        tasks.add_dependency("task-a", "task-c", created_at=NOW)
    with pytest.raises(TaskServiceError, match="missing task"):
        tasks.add_dependency("missing", "task-a", created_at=NOW)
    with pytest.raises(TaskServiceError, match="depend on itself"):
        tasks.add_dependency("task-a", "task-a", created_at=NOW)
    assert ledger.snapshot() == before


def test_conflicts_are_symmetric_idempotent_and_immutable(tmp_path: Path) -> None:
    tasks, ledger = service(tmp_path)
    tasks.create(task("task-a"))
    tasks.create(task("task-b"))

    first = tasks.add_conflict(
        "task-b", "task-a", reason="shared contract", created_at=NOW
    )
    repeated = tasks.add_conflict(
        "task-a",
        "task-b",
        reason="shared contract",
        created_at=NOW + timedelta(minutes=1),
    )

    assert first.created is True
    assert repeated.created is False
    assert tasks.conflicts() == (first.relation,)
    before = ledger.snapshot()
    with pytest.raises(TaskServiceError, match="different reason"):
        tasks.add_conflict(
            "task-a", "task-b", reason="different", created_at=NOW
        )
    with pytest.raises(TaskServiceError, match="missing task"):
        tasks.add_conflict(
            "task-a", "missing", reason="shared", created_at=NOW
        )
    assert ledger.snapshot() == before


def test_versioned_codecs_round_trip_and_refuse_malformed_payloads() -> None:
    value = task("task-a").transition(
        TaskState.IN_PROGRESS,
        actor_id="worker",
        rationale="claimed",
        occurred_at=NOW + timedelta(minutes=1),
    )
    dependency = TaskDependency("task-b", "task-a", NOW)
    conflict = TaskConflict.between(
        "task-a", "task-b", reason="shared", created_at=NOW
    )

    assert task_from_payload(task_to_payload(value)) == value
    assert dependency_from_payload(dependency_to_payload(dependency)) == dependency
    assert conflict_from_payload(conflict_to_payload(conflict)) == conflict
    malformed = task_to_payload(value)
    malformed["schema_version"] = 99
    with pytest.raises(TaskServiceError, match="malformed"):
        task_from_payload(malformed)


def test_interleaved_duplicate_task_creation_is_refused(tmp_path: Path) -> None:
    value = task("task-race")
    pending = LedgerDraft(
        "task",
        value.id,
        json.dumps(task_to_payload(value), sort_keys=True, separators=(",", ":")),
        NOW,
    )
    port = InterleavingLedger(JsonlLedger(tmp_path / "race.jsonl"), pending)
    tasks = TaskService(LedgerService(port))

    with pytest.raises(TaskServiceError, match="already exists"):
        tasks.create(value)
    assert tasks.get(value.id) == value
