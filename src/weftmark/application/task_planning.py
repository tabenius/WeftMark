"""Advisory dependency-aware selection over native Task Intent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from weftmark.application.tasks import TaskService, task_to_payload
from weftmark.domain.task import TaskIntent, TaskState


class TaskPlanningError(ValueError):
    """Raised when native task eligibility cannot be evaluated."""


@dataclass(frozen=True, slots=True)
class TaskEligibility:
    task: TaskIntent
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskSelection:
    considered: int
    eligible: int
    tasks: tuple[TaskEligibility, ...]
    skipped: tuple[TaskEligibility, ...]


class TaskPlanningService:
    def __init__(self, tasks: TaskService) -> None:
        self._tasks = tasks

    def next(self, *, limit: int = 1) -> TaskSelection:
        if limit < 1 or limit > 100:
            raise TaskPlanningError("limit must be between 1 and 100")
        evaluated = self._evaluate()
        eligible = sorted(
            (value for value in evaluated if value.eligible),
            key=lambda value: (
                int(value.task.priority.value[1:]),
                value.task.created_at,
                value.task.id,
            ),
        )
        skipped = tuple(value for value in evaluated if not value.eligible)
        return TaskSelection(
            len(evaluated),
            len(eligible),
            tuple(eligible[:limit]),
            skipped,
        )

    def eligibility(self, task_id: str) -> TaskEligibility:
        matches = tuple(
            value for value in self._evaluate() if value.task.id == task_id
        )
        if len(matches) != 1:
            raise TaskPlanningError(f"Task not found: {task_id}")
        return matches[0]

    def _evaluate(self) -> tuple[TaskEligibility, ...]:
        tasks = self._tasks.list()
        by_id = {task.id: task for task in tasks}
        dependencies: dict[str, list[str]] = {}
        for relation in self._tasks.dependencies():
            if relation.task_id not in by_id or relation.depends_on_task_id not in by_id:
                raise TaskPlanningError("dependency references a missing native task")
            dependencies.setdefault(relation.task_id, []).append(
                relation.depends_on_task_id
            )
        conflicts: dict[str, set[str]] = {}
        for relation in self._tasks.conflicts():
            if (
                relation.first_task_id not in by_id
                or relation.second_task_id not in by_id
            ):
                raise TaskPlanningError("conflict references a missing native task")
            conflicts.setdefault(relation.first_task_id, set()).add(
                relation.second_task_id
            )
            conflicts.setdefault(relation.second_task_id, set()).add(
                relation.first_task_id
            )

        evaluated: list[TaskEligibility] = []
        for task in tasks:
            reasons: list[str] = []
            if task.state not in {TaskState.IDEA, TaskState.TODO}:
                reasons.append(f"task state is not selectable: {task.state.value}")
            unmet = tuple(
                dependency
                for dependency in sorted(dependencies.get(task.id, ()))
                if by_id[dependency].state is not TaskState.DONE
            )
            if unmet:
                reasons.append("dependencies not done: " + ", ".join(unmet))
            active_conflicts = tuple(
                conflict
                for conflict in sorted(conflicts.get(task.id, ()))
                if by_id[conflict].state is TaskState.IN_PROGRESS
            )
            if active_conflicts:
                reasons.append("conflicts in progress: " + ", ".join(active_conflicts))
            evaluated.append(TaskEligibility(task, not reasons, tuple(reasons)))
        return tuple(evaluated)


def task_selection_to_payload(value: TaskSelection) -> dict[str, Any]:
    return {
        "considered": value.considered,
        "eligible": value.eligible,
        "tasks": [
            {
                "task": task_to_payload(item.task),
                "eligible": True,
                "reasons": [],
            }
            for item in value.tasks
        ],
        "skipped": [
            {"id": item.task.id, "reasons": list(item.reasons)}
            for item in value.skipped[:20]
        ],
        "skipped_count": len(value.skipped),
        "authority": "advisory native intent; Change Set and claim acquisition are separate",
    }
