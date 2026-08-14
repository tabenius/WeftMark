"""Advisory task eligibility over immutable imported Frog snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from weftmark.application.frog_receipts import FrogReceiptService


class FrogPlanningError(ValueError):
    """Raised when imported planning intent cannot be evaluated safely."""


_DONE = frozenset({"done", "cancelled", "abandoned", "archived"})
_BLOCKED = frozenset({"blocked"})
_IN_PROGRESS = frozenset(
    {"in_progress", "in-progress", "doing", "wip", "active", "review", "started"}
)


@dataclass(frozen=True, slots=True)
class FrogTaskEligibility:
    task: Mapping[str, Any]
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FrogTaskSelection:
    snapshot_digest: str
    source_label: str
    repo_path: str | None
    considered: int
    eligible: int
    tasks: tuple[FrogTaskEligibility, ...]
    skipped: tuple[FrogTaskEligibility, ...]
    ignored_lock_observations: int
    ignored_assignment_observations: int


class FrogPlanningService:
    """Evaluate imported plan intent without claiming or promoting it."""

    def __init__(self, receipts: FrogReceiptService) -> None:
        self._receipts = receipts

    def next(
        self,
        snapshot_digest: str,
        *,
        repo_path: str | None = None,
        limit: int = 1,
    ) -> FrogTaskSelection:
        if limit < 1 or limit > 100:
            raise FrogPlanningError("limit must be between 1 and 100")
        receipt = self._receipts.get(snapshot_digest)
        if receipt is None:
            raise FrogPlanningError(f"Frog snapshot not found: {snapshot_digest}")
        records = receipt.snapshot["records"]
        tasks = tuple(records["tasks"])
        by_slug: dict[str, Mapping[str, Any]] = {}
        for task in tasks:
            slug = _text(task, "slug")
            _optional_text(task, "repo_path")
            _text(task, "title")
            _text(task, "workflow_status")
            _text(task, "priority")
            _text(task, "created_at")
            if slug in by_slug:
                raise FrogPlanningError(f"duplicate Frog task: {slug}")
            by_slug[slug] = task

        dependencies: dict[str, list[str]] = {}
        for relation in records["task_dependencies"]:
            if relation.get("relation") != "depends_on":
                continue
            task_slug = _text(relation, "task_slug")
            depends_on = _text(relation, "depends_on_slug")
            if task_slug not in by_slug or depends_on not in by_slug:
                raise FrogPlanningError("Frog dependency references a missing task")
            dependencies.setdefault(task_slug, []).append(depends_on)

        conflicts: dict[str, set[str]] = {}
        for relation in records["task_conflicts"]:
            left = _text(relation, "task_slug")
            right = _text(relation, "conflicts_with_slug")
            if left not in by_slug or right not in by_slug:
                raise FrogPlanningError("Frog conflict references a missing task")
            conflicts.setdefault(left, set()).add(right)
            conflicts.setdefault(right, set()).add(left)

        evaluated: list[FrogTaskEligibility] = []
        for task in tasks:
            if repo_path is not None and task["repo_path"] != repo_path:
                continue
            slug = str(task["slug"])
            status = str(task.get("workflow_status") or "").casefold()
            reasons: list[str] = []
            if status in _DONE:
                reasons.append(f"source status is terminal: {status}")
            elif status in _BLOCKED:
                reasons.append(f"source status is blocked: {status}")
            unmet = tuple(
                dependency
                for dependency in sorted(dependencies.get(slug, ()))
                if str(by_slug[dependency].get("workflow_status") or "").casefold()
                not in _DONE
            )
            if unmet:
                reasons.append("dependencies not done: " + ", ".join(unmet))
            active_conflicts = tuple(
                conflict
                for conflict in sorted(conflicts.get(slug, ()))
                if str(by_slug[conflict].get("workflow_status") or "").casefold()
                in _IN_PROGRESS
            )
            if active_conflicts:
                reasons.append(
                    "source conflicts in progress: " + ", ".join(active_conflicts)
                )
            evaluated.append(FrogTaskEligibility(task, not reasons, tuple(reasons)))

        eligible = sorted(
            (value for value in evaluated if value.eligible),
            key=lambda value: (
                _priority_rank(value.task.get("priority")),
                str(value.task.get("created_at") or ""),
                str(value.task["slug"]),
            ),
        )
        skipped = tuple(value for value in evaluated if not value.eligible)
        return FrogTaskSelection(
            snapshot_digest,
            receipt.source_label,
            repo_path,
            len(evaluated),
            len(eligible),
            tuple(eligible[:limit]),
            skipped,
            len(records["locks"]),
            len(records["task_assignments"]),
        )


def selection_to_payload(value: FrogTaskSelection) -> dict[str, Any]:
    return {
        "snapshot_digest": value.snapshot_digest,
        "source_label": value.source_label,
        "repo_path": value.repo_path,
        "considered": value.considered,
        "eligible": value.eligible,
        "tasks": [_eligibility_to_payload(item) for item in value.tasks],
        "skipped": [_skipped_to_payload(item) for item in value.skipped[:20]],
        "skipped_count": len(value.skipped),
        "authority": "advisory imported intent; promotion and claim are separate local actions",
        "ignored_observations": {
            "locks": value.ignored_lock_observations,
            "assignments": value.ignored_assignment_observations,
        },
    }


def _eligibility_to_payload(value: FrogTaskEligibility) -> dict[str, Any]:
    return {
        "task": dict(value.task),
        "eligible": value.eligible,
        "reasons": list(value.reasons),
    }


def _skipped_to_payload(value: FrogTaskEligibility) -> dict[str, Any]:
    return {
        "slug": value.task["slug"],
        "reasons": list(value.reasons),
    }


def _text(value: Mapping[str, Any], name: str) -> str:
    try:
        text = str(value[name]).strip()
    except KeyError as error:
        raise FrogPlanningError(f"Frog record lacks {name}") from error
    if not text:
        raise FrogPlanningError(f"Frog record has empty {name}")
    return text


def _optional_text(value: Mapping[str, Any], name: str) -> str | None:
    if name not in value:
        raise FrogPlanningError(f"Frog record lacks {name}")
    if value[name] is None:
        return None
    return _text(value, name)


def _priority_rank(value: object) -> int:
    text = str(value or "").casefold()
    if len(text) >= 2 and text[0] == "p" and text[1:].isdigit():
        return int(text[1:])
    return 9
