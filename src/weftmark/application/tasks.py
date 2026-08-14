"""Durable native task intent and plan-relation workflows."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import (
    LEDGER_GENESIS_DIGEST,
    LedgerEntry,
    LedgerHeadChanged,
)
from weftmark.domain.scope import Scope
from weftmark.domain.task import (
    TaskConflict,
    TaskDependency,
    TaskError,
    TaskIntent,
    TaskPriority,
    TaskState,
    TaskStateEvent,
)


class TaskServiceError(ValueError):
    """Raised when native task persistence or plan relations are invalid."""


@dataclass(frozen=True, slots=True)
class RelationResult:
    relation: TaskDependency | TaskConflict
    created: bool


class TaskService:
    def __init__(self, ledger: LedgerService) -> None:
        self._ledger = ledger

    def create(self, task: TaskIntent) -> TaskIntent:
        if task.state not in {TaskState.IDEA, TaskState.TODO} or task.state_events:
            raise TaskServiceError("new persisted task must begin as idea or todo")
        for _ in range(8):
            entries = self._ledger.snapshot()
            if task.id in _latest_tasks(entries):
                raise TaskServiceError(f"Task already exists: {task.id}")
            try:
                self._record_task(task, entries)
                return task
            except LedgerHeadChanged:
                continue
        raise TaskServiceError("ledger remained busy while creating task")

    def get(self, id: str) -> TaskIntent | None:
        return _latest_tasks(self._ledger.snapshot()).get(id)

    def require(self, id: str) -> TaskIntent:
        task = self.get(id)
        if task is None:
            raise TaskServiceError(f"Task not found: {id}")
        return task

    def list(self) -> tuple[TaskIntent, ...]:
        tasks = _latest_tasks(self._ledger.snapshot())
        return tuple(tasks[id] for id in sorted(tasks))

    def transition(
        self,
        id: str,
        *,
        state: TaskState,
        actor_id: str,
        rationale: str,
        occurred_at: datetime,
    ) -> TaskIntent:
        if state is TaskState.DONE:
            raise TaskServiceError("done requires the task completion workflow")
        for _ in range(8):
            entries = self._ledger.snapshot()
            current = _latest_tasks(entries).get(id)
            if current is None:
                raise TaskServiceError(f"Task not found: {id}")
            try:
                changed = current.transition(
                    state,
                    actor_id=actor_id,
                    rationale=rationale,
                    occurred_at=occurred_at,
                )
            except TaskError as error:
                raise TaskServiceError(str(error)) from error
            try:
                self._record_task(changed, entries)
                return changed
            except LedgerHeadChanged:
                continue
        raise TaskServiceError("ledger remained busy while transitioning task")

    def add_dependency(
        self,
        task_id: str,
        depends_on_task_id: str,
        *,
        created_at: datetime,
    ) -> RelationResult:
        try:
            relation = TaskDependency(task_id, depends_on_task_id, created_at)
        except TaskError as error:
            raise TaskServiceError(str(error)) from error
        entity_id = _relation_id("dependency", task_id, depends_on_task_id)
        for _ in range(8):
            entries = self._ledger.snapshot()
            tasks = _latest_tasks(entries)
            _require_relation_tasks(tasks, task_id, depends_on_task_id)
            dependencies = _dependencies(entries)
            existing = dependencies.get(entity_id)
            if existing is not None:
                return RelationResult(existing, False)
            graph: dict[str, set[str]] = {}
            for value in dependencies.values():
                graph.setdefault(value.task_id, set()).add(value.depends_on_task_id)
            graph.setdefault(task_id, set()).add(depends_on_task_id)
            if _reaches(graph, depends_on_task_id, task_id):
                raise TaskServiceError(
                    f"dependency would create a cycle: {task_id} -> {depends_on_task_id}"
                )
            try:
                self._record_relation(
                    "task_dependency",
                    entity_id,
                    dependency_to_payload(relation),
                    created_at,
                    entries,
                )
                return RelationResult(relation, True)
            except LedgerHeadChanged:
                continue
        raise TaskServiceError("ledger remained busy while adding dependency")

    def add_conflict(
        self,
        left_task_id: str,
        right_task_id: str,
        *,
        reason: str,
        created_at: datetime,
    ) -> RelationResult:
        try:
            relation = TaskConflict.between(
                left_task_id,
                right_task_id,
                reason=reason,
                created_at=created_at,
            )
        except TaskError as error:
            raise TaskServiceError(str(error)) from error
        entity_id = _relation_id(
            "conflict", relation.first_task_id, relation.second_task_id
        )
        for _ in range(8):
            entries = self._ledger.snapshot()
            tasks = _latest_tasks(entries)
            _require_relation_tasks(
                tasks, relation.first_task_id, relation.second_task_id
            )
            existing = _conflicts(entries).get(entity_id)
            if existing is not None:
                if existing.reason != reason:
                    raise TaskServiceError(
                        "task conflict already exists with a different reason"
                    )
                return RelationResult(existing, False)
            try:
                self._record_relation(
                    "task_conflict",
                    entity_id,
                    conflict_to_payload(relation),
                    created_at,
                    entries,
                )
                return RelationResult(relation, True)
            except LedgerHeadChanged:
                continue
        raise TaskServiceError("ledger remained busy while adding conflict")

    def dependencies(self) -> tuple[TaskDependency, ...]:
        values = _dependencies(self._ledger.snapshot())
        return tuple(
            sorted(
                values.values(),
                key=lambda value: (value.task_id, value.depends_on_task_id),
            )
        )

    def conflicts(self) -> tuple[TaskConflict, ...]:
        values = _conflicts(self._ledger.snapshot())
        return tuple(
            sorted(
                values.values(),
                key=lambda value: (value.first_task_id, value.second_task_id),
            )
        )

    def _record_task(
        self, task: TaskIntent, entries: tuple[LedgerEntry, ...]
    ) -> None:
        self._record_relation(
            "task",
            task.id,
            task_to_payload(task),
            task.updated_at,
            entries,
        )

    def _record_relation(
        self,
        kind: str,
        entity_id: str,
        payload: Mapping[str, Any],
        recorded_at: datetime,
        entries: tuple[LedgerEntry, ...],
    ) -> None:
        expected = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
        self._ledger.record_if_head(
            kind=kind,
            entity_id=entity_id,
            payload=payload,
            recorded_at=recorded_at,
            expected_digest=expected,
        )


def task_to_payload(task: TaskIntent) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": task.id,
        "title": task.title,
        "why": task.why,
        "what": task.what,
        "roi_note": task.roi_note,
        "priority": task.priority.value,
        "state": task.state.value,
        "scopes": [Scope.parse(value).to_dict() for value in task.scopes],
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "state_events": [
            {
                "previous_state": event.previous_state.value,
                "state": event.state.value,
                "actor_id": event.actor_id,
                "rationale": event.rationale,
                "occurred_at": event.occurred_at.isoformat(),
            }
            for event in task.state_events
        ],
    }


def task_from_payload(payload: Mapping[str, Any]) -> TaskIntent:
    try:
        if payload["schema_version"] != 1:
            raise ValueError("unsupported task schema")
        return TaskIntent(
            id=str(payload["id"]),
            title=str(payload["title"]),
            why=str(payload["why"]),
            what=str(payload["what"]),
            roi_note=(
                None if payload["roi_note"] is None else str(payload["roi_note"])
            ),
            priority=TaskPriority(str(payload["priority"])),
            state=TaskState(str(payload["state"])),
            scopes=tuple(
                Scope.from_dict(value).canonical for value in payload["scopes"]
            ),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            state_events=tuple(
                TaskStateEvent(
                    TaskState(str(value["previous_state"])),
                    TaskState(str(value["state"])),
                    str(value["actor_id"]),
                    str(value["rationale"]),
                    datetime.fromisoformat(str(value["occurred_at"])),
                )
                for value in payload["state_events"]
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TaskServiceError("stored Task Intent is malformed") from error


def dependency_to_payload(value: TaskDependency) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": value.task_id,
        "depends_on_task_id": value.depends_on_task_id,
        "created_at": value.created_at.isoformat(),
    }


def dependency_from_payload(payload: Mapping[str, Any]) -> TaskDependency:
    try:
        if payload["schema_version"] != 1:
            raise ValueError("unsupported dependency schema")
        return TaskDependency(
            str(payload["task_id"]),
            str(payload["depends_on_task_id"]),
            datetime.fromisoformat(str(payload["created_at"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TaskServiceError("stored task dependency is malformed") from error


def conflict_to_payload(value: TaskConflict) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "first_task_id": value.first_task_id,
        "second_task_id": value.second_task_id,
        "reason": value.reason,
        "created_at": value.created_at.isoformat(),
    }


def conflict_from_payload(payload: Mapping[str, Any]) -> TaskConflict:
    try:
        if payload["schema_version"] != 1:
            raise ValueError("unsupported conflict schema")
        return TaskConflict(
            str(payload["first_task_id"]),
            str(payload["second_task_id"]),
            str(payload["reason"]),
            datetime.fromisoformat(str(payload["created_at"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TaskServiceError("stored task conflict is malformed") from error


def _latest_tasks(entries: tuple[LedgerEntry, ...]) -> dict[str, TaskIntent]:
    tasks: dict[str, TaskIntent] = {}
    for entry in entries:
        if entry.kind == "task":
            task = task_from_payload(entry.payload)
            if task.id != entry.entity_id:
                raise TaskServiceError("stored Task Intent identity mismatch")
            tasks[entry.entity_id] = task
    return tasks


def _dependencies(entries: tuple[LedgerEntry, ...]) -> dict[str, TaskDependency]:
    return _immutable_relations(
        entries,
        "task_dependency",
        dependency_from_payload,
        lambda value: _relation_id(
            "dependency", value.task_id, value.depends_on_task_id
        ),
    )


def _conflicts(entries: tuple[LedgerEntry, ...]) -> dict[str, TaskConflict]:
    return _immutable_relations(
        entries,
        "task_conflict",
        conflict_from_payload,
        lambda value: _relation_id(
            "conflict", value.first_task_id, value.second_task_id
        ),
    )


def _immutable_relations(entries, kind, decoder, identity):
    values = {}
    for entry in entries:
        if entry.kind != kind:
            continue
        value = decoder(entry.payload)
        if entry.entity_id != identity(value):
            raise TaskServiceError(f"stored {kind} identity mismatch")
        if entry.entity_id in values and values[entry.entity_id] != value:
            raise TaskServiceError(f"stored {kind} identity was rewritten")
        values[entry.entity_id] = value
    return values


def _require_relation_tasks(
    tasks: Mapping[str, TaskIntent], first: str, second: str
) -> None:
    missing = tuple(id for id in (first, second) if id not in tasks)
    if missing:
        raise TaskServiceError("relation references missing task: " + ", ".join(missing))


def _relation_id(kind: str, first: str, second: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{first}\0{second}".encode()).hexdigest()
    return f"sha256:{digest}"


def _reaches(graph: Mapping[str, set[str]], start: str, target: str) -> bool:
    pending = [start]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, ()))
    return False
