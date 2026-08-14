"""Vendor-neutral task intent and plan-relation invariants."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from weftmark.domain.scope import Scope, ScopeError


class TaskError(ValueError):
    """Raised when task intent or a plan relation violates its contract."""


class TaskState(StrEnum):
    IDEA = "idea"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    ABANDONED = "abandoned"


class TaskPriority(StrEnum):
    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,127})$")
_ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.IDEA: frozenset({TaskState.TODO, TaskState.ABANDONED}),
    TaskState.TODO: frozenset(
        {TaskState.IN_PROGRESS, TaskState.BLOCKED, TaskState.ABANDONED}
    ),
    TaskState.IN_PROGRESS: frozenset(
        {TaskState.TODO, TaskState.BLOCKED, TaskState.DONE, TaskState.ABANDONED}
    ),
    TaskState.BLOCKED: frozenset(
        {TaskState.TODO, TaskState.IN_PROGRESS, TaskState.ABANDONED}
    ),
    TaskState.DONE: frozenset(),
    TaskState.ABANDONED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TaskStateEvent:
    previous_state: TaskState
    state: TaskState
    actor_id: str
    rationale: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_text("actor_id", self.actor_id)
        _require_text("rationale", self.rationale)
        _require_aware("occurred_at", self.occurred_at)
        if self.previous_state is self.state:
            raise TaskError("task state event must change state")


@dataclass(frozen=True, slots=True)
class TaskIntent:
    id: str
    title: str
    why: str
    what: str
    roi_note: str | None
    priority: TaskPriority
    state: TaskState
    scopes: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    state_events: tuple[TaskStateEvent, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier("task id", self.id)
        _require_text("title", self.title)
        _require_text("why", self.why)
        _require_text("what", self.what)
        if self.roi_note is not None:
            _require_text("roi_note", self.roi_note)
        _require_aware("created_at", self.created_at)
        _require_aware("updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise TaskError("updated_at must not precede created_at")
        if len(set(self.scopes)) != len(self.scopes):
            raise TaskError("task scopes must not contain duplicates")
        for value in self.scopes:
            try:
                scope = Scope.parse(value)
            except ScopeError as error:
                raise TaskError("task scope is invalid") from error
            if scope.canonical != value:
                raise TaskError("task scopes must be canonical")
        _validate_event_chain(self)

    @classmethod
    def create(
        cls,
        *,
        id: str,
        title: str,
        why: str,
        what: str,
        roi_note: str | None,
        priority: TaskPriority,
        state: TaskState,
        scopes: tuple[Scope, ...],
        created_at: datetime,
    ) -> TaskIntent:
        if state not in {TaskState.IDEA, TaskState.TODO}:
            raise TaskError("new task state must be idea or todo")
        canonical = tuple(scope.canonical for scope in scopes)
        return cls(
            id,
            title,
            why,
            what,
            roi_note,
            priority,
            state,
            canonical,
            created_at,
            created_at,
        )

    def transition(
        self,
        state: TaskState,
        *,
        actor_id: str,
        rationale: str,
        occurred_at: datetime,
    ) -> TaskIntent:
        if state not in _ALLOWED_TRANSITIONS[self.state]:
            raise TaskError(f"cannot transition task from {self.state} to {state}")
        if occurred_at < self.updated_at:
            raise TaskError("task transition time must not move backwards")
        event = TaskStateEvent(
            self.state,
            state,
            actor_id,
            rationale,
            occurred_at,
        )
        return replace(
            self,
            state=state,
            updated_at=occurred_at,
            state_events=self.state_events + (event,),
        )


@dataclass(frozen=True, slots=True)
class TaskDependency:
    task_id: str
    depends_on_task_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_identifier("task_id", self.task_id)
        _require_identifier("depends_on_task_id", self.depends_on_task_id)
        _require_aware("created_at", self.created_at)
        if self.task_id == self.depends_on_task_id:
            raise TaskError("task cannot depend on itself")


@dataclass(frozen=True, slots=True)
class TaskConflict:
    first_task_id: str
    second_task_id: str
    reason: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_identifier("first_task_id", self.first_task_id)
        _require_identifier("second_task_id", self.second_task_id)
        _require_text("reason", self.reason)
        _require_aware("created_at", self.created_at)
        if self.first_task_id >= self.second_task_id:
            raise TaskError("task conflict identities must be distinct and canonical")

    @classmethod
    def between(
        cls,
        left_task_id: str,
        right_task_id: str,
        *,
        reason: str,
        created_at: datetime,
    ) -> TaskConflict:
        first, second = sorted((left_task_id, right_task_id))
        return cls(first, second, reason, created_at)

    def includes(self, task_id: str) -> bool:
        return task_id in {self.first_task_id, self.second_task_id}

    def other(self, task_id: str) -> str:
        if task_id == self.first_task_id:
            return self.second_task_id
        if task_id == self.second_task_id:
            return self.first_task_id
        raise TaskError(f"task conflict does not include: {task_id}")


def _validate_event_chain(task: TaskIntent) -> None:
    previous = task.state_events[0].previous_state if task.state_events else task.state
    if previous not in {TaskState.IDEA, TaskState.TODO}:
        raise TaskError("task history must begin as idea or todo")
    last_at = task.created_at
    for event in task.state_events:
        if event.previous_state is not previous:
            raise TaskError("task state event chain is discontinuous")
        if event.state not in _ALLOWED_TRANSITIONS[event.previous_state]:
            raise TaskError("task state event transition is invalid")
        if event.occurred_at < last_at:
            raise TaskError("task state event time must not move backwards")
        previous = event.state
        last_at = event.occurred_at
    if previous is not task.state or last_at != task.updated_at:
        raise TaskError("task state does not match its event history")


def _require_identifier(name: str, value: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise TaskError(f"{name} must be a lowercase portable identifier")


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise TaskError(f"{name} must not be empty")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TaskError(f"{name} must include a timezone")
