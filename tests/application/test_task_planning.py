from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from weftmark.application.task_planning import (
    TaskPlanningError,
    TaskPlanningService,
    task_selection_to_payload,
)
from weftmark.domain.scope import Scope
from weftmark.domain.task import (
    TaskConflict,
    TaskDependency,
    TaskIntent,
    TaskPriority,
    TaskState,
)


NOW = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)


class StaticTasks:
    def __init__(self, tasks, dependencies=(), conflicts=()) -> None:
        self._tasks = tuple(tasks)
        self._dependencies = tuple(dependencies)
        self._conflicts = tuple(conflicts)

    def list(self):
        return self._tasks

    def dependencies(self):
        return self._dependencies

    def conflicts(self):
        return self._conflicts


def task(
    id: str,
    *,
    priority: TaskPriority,
    state: TaskState = TaskState.TODO,
    offset: int = 0,
) -> TaskIntent:
    value = TaskIntent.create(
        id=id,
        title=id.replace("-", " ").title(),
        why="Choose useful work.",
        what="Evaluate native intent.",
        roi_note=None,
        priority=priority,
        state=TaskState.TODO if state not in {TaskState.IDEA, TaskState.TODO} else state,
        scopes=(Scope.contract(f"scope-{id}"),),
        created_at=NOW + timedelta(seconds=offset),
    )
    if state is TaskState.IN_PROGRESS:
        return value.transition(
            state, actor_id="worker", rationale="claimed", occurred_at=value.updated_at
        )
    if state is TaskState.BLOCKED:
        return value.transition(
            state, actor_id="worker", rationale="waiting", occurred_at=value.updated_at
        )
    if state is TaskState.ABANDONED:
        return value.transition(
            state, actor_id="worker", rationale="cancelled", occurred_at=value.updated_at
        )
    if state is TaskState.DONE:
        active = value.transition(
            TaskState.IN_PROGRESS,
            actor_id="worker",
            rationale="claimed",
            occurred_at=value.updated_at,
        )
        return active.transition(
            state,
            actor_id="reviewer",
            rationale="completion gates passed",
            occurred_at=value.updated_at,
        )
    return value


def test_next_ranks_only_dependency_and_conflict_eligible_native_intent() -> None:
    done = task("done-dep", priority=TaskPriority.P3, state=TaskState.DONE)
    abandoned = task(
        "abandoned-dep", priority=TaskPriority.P3, state=TaskState.ABANDONED
    )
    active = task("active", priority=TaskPriority.P3, state=TaskState.IN_PROGRESS)
    high = task("eligible-high", priority=TaskPriority.P0, state=TaskState.IDEA)
    low = task("eligible-low", priority=TaskPriority.P2, offset=1)
    unmet = task("unmet", priority=TaskPriority.P0)
    conflicted = task("conflicted", priority=TaskPriority.P1)
    blocked = task("blocked", priority=TaskPriority.P0, state=TaskState.BLOCKED)
    service = TaskPlanningService(
        StaticTasks(
            (done, abandoned, active, high, low, unmet, conflicted, blocked),
            dependencies=(
                TaskDependency("eligible-low", "done-dep", NOW),
                TaskDependency("unmet", "abandoned-dep", NOW),
            ),
            conflicts=(
                TaskConflict.between(
                    "conflicted", "active", reason="shared", created_at=NOW
                ),
            ),
        )
    )

    selection = service.next(limit=5)

    assert [value.task.id for value in selection.tasks] == [
        "eligible-high",
        "eligible-low",
    ]
    assert selection.considered == 8
    assert selection.eligible == 2
    assert service.eligibility("eligible-high").eligible is True
    assert service.eligibility("unmet").eligible is False
    reasons = {value.task.id: value.reasons for value in selection.skipped}
    assert reasons["unmet"] == ("dependencies not done: abandoned-dep",)
    assert reasons["conflicted"] == ("conflicts in progress: active",)
    assert reasons["blocked"] == ("task state is not selectable: blocked",)
    payload = task_selection_to_payload(selection)
    assert payload["tasks"][0]["task"]["id"] == "eligible-high"
    assert payload["authority"].startswith("advisory native intent")


def test_next_refuses_invalid_limit() -> None:
    service = TaskPlanningService(StaticTasks(()))
    with pytest.raises(TaskPlanningError, match="limit"):
        service.next(limit=0)
