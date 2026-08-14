from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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


NOW = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)


def intent(*, state: TaskState = TaskState.TODO) -> TaskIntent:
    return TaskIntent.create(
        id="native-task-1",
        title="Create native task intent",
        why="New work must not require Frog.",
        what="Define a portable task model.",
        roi_note="Unlock native planning commands.",
        priority=TaskPriority.P0,
        state=state,
        scopes=(Scope.file("src/**"), Scope.contract("task-v1")),
        created_at=NOW,
    )


def test_create_and_transition_preserve_intent_and_append_state_history() -> None:
    created = intent()
    active = created.transition(
        TaskState.IN_PROGRESS,
        actor_id="worker-1",
        rationale="local Change Set and claim acquired",
        occurred_at=NOW + timedelta(minutes=1),
    )
    done = active.transition(
        TaskState.DONE,
        actor_id="reviewer-1",
        rationale="application completion gates passed",
        occurred_at=NOW + timedelta(minutes=2),
    )

    assert created.state_events == ()
    assert active.state is TaskState.IN_PROGRESS
    assert done.state is TaskState.DONE
    assert [event.state for event in done.state_events] == [
        TaskState.IN_PROGRESS,
        TaskState.DONE,
    ]
    assert done.scopes == ("file:src/**", "contract:task-v1")
    assert done.title == created.title
    with pytest.raises(TaskError, match="cannot transition"):
        done.transition(
            TaskState.TODO,
            actor_id="worker-1",
            rationale="reopen silently",
            occurred_at=NOW + timedelta(minutes=3),
        )


def test_task_intent_refuses_invalid_identity_scope_state_and_history() -> None:
    with pytest.raises(TaskError, match="new task state"):
        intent(state=TaskState.IN_PROGRESS)
    with pytest.raises(TaskError, match="portable identifier"):
        TaskIntent.create(
            id="Not Portable",
            title="title",
            why="why",
            what="what",
            roi_note=None,
            priority=TaskPriority.P1,
            state=TaskState.IDEA,
            scopes=(),
            created_at=NOW,
        )
    with pytest.raises(TaskError, match="duplicates"):
        TaskIntent.create(
            id="duplicate-scope",
            title="title",
            why="why",
            what="what",
            roi_note=None,
            priority=TaskPriority.P1,
            state=TaskState.TODO,
            scopes=(Scope.file("src/**"), Scope.file("src/**")),
            created_at=NOW,
        )
    with pytest.raises(TaskError, match="begin as idea or todo"):
        TaskIntent(
            "terminal-without-history",
            "title",
            "why",
            "what",
            None,
            TaskPriority.P1,
            TaskState.DONE,
            (),
            NOW,
            NOW,
        )
    event = TaskStateEvent(
        TaskState.TODO,
        TaskState.BLOCKED,
        "worker",
        "waiting",
        NOW + timedelta(minutes=1),
    )
    with pytest.raises(TaskError, match="does not match"):
        TaskIntent(
            "broken-history",
            "title",
            "why",
            "what",
            None,
            TaskPriority.P1,
            TaskState.TODO,
            (),
            NOW,
            NOW + timedelta(minutes=1),
            (event,),
        )


def test_dependency_is_directed_and_conflict_is_symmetric() -> None:
    dependency = TaskDependency("task-b", "task-a", NOW)
    conflict = TaskConflict.between(
        "task-b", "task-a", reason="shared contract", created_at=NOW
    )

    assert dependency.task_id == "task-b"
    assert dependency.depends_on_task_id == "task-a"
    assert conflict.first_task_id == "task-a"
    assert conflict.second_task_id == "task-b"
    assert conflict.includes("task-b")
    assert conflict.other("task-a") == "task-b"
    with pytest.raises(TaskError, match="depend on itself"):
        TaskDependency("task-a", "task-a", NOW)
    with pytest.raises(TaskError, match="distinct and canonical"):
        TaskConflict("task-b", "task-a", "shared", NOW)
    with pytest.raises(TaskError, match="does not include"):
        conflict.other("task-c")
