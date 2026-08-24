"""Read-only transition board projection over an immutable Frog snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from weftmark.application.frog_planning import FrogPlanningError, FrogPlanningService
from weftmark.application.frog_receipts import FrogReceiptService


FROG_TRANSITION_PROJECTION_SCHEMA = "weftmark.frog-transition-projection.v0"


class FrogProjectionError(ValueError):
    """Raised when a safe transition projection cannot be produced."""


class FrogTransitionLane(StrEnum):
    BACKLOG = "backlog"
    ACTIVE = "active"
    REVIEW = "review"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class FrogLockObservation:
    lock_id: str
    status: str


@dataclass(frozen=True, slots=True)
class FrogTransitionCard:
    task_slug: str
    title: str
    repo_path: str | None
    lane: FrogTransitionLane
    workflow_status: str
    git_status: str | None
    priority: str
    created_at: str
    eligible: bool
    eligibility_reasons: tuple[str, ...]
    dependencies: tuple[str, ...]
    conflicts: tuple[str, ...]
    assignment_observations: tuple[str, ...]
    lock_observations: tuple[FrogLockObservation, ...]
    attention: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FrogTransitionProjection:
    snapshot_digest: str
    source_label: str
    captured_at: datetime
    imported_at: datetime
    generated_at: datetime
    stale_after_seconds: int
    stale: bool
    repo_path: str | None
    ignored_assignment_records: int
    ignored_lock_records: int
    cards: tuple[FrogTransitionCard, ...]


class FrogTransitionProjectionService:
    """Project Frog intent and observations without importing their authority."""

    def __init__(self, receipts: FrogReceiptService) -> None:
        self._receipts = receipts
        self._planning = FrogPlanningService(receipts)

    def project(
        self,
        snapshot_digest: str,
        *,
        generated_at: datetime,
        repo_path: str | None = None,
        stale_after_seconds: int = 3600,
    ) -> FrogTransitionProjection:
        _require_aware(generated_at, "generated_at")
        if stale_after_seconds < 1:
            raise FrogProjectionError("stale_after_seconds must be positive")
        receipt = self._receipts.get(snapshot_digest)
        if receipt is None:
            raise FrogProjectionError(f"Frog snapshot not found: {snapshot_digest}")
        if generated_at < max(receipt.captured_at, receipt.imported_at):
            raise FrogProjectionError("generated_at predates the Frog snapshot receipt")

        records = receipt.snapshot["records"]
        dependencies = _relations(
            records["task_dependencies"], "task_slug", "depends_on_slug",
            relation="depends_on",
        )
        conflicts = _symmetric_relations(
            records["task_conflicts"], "task_slug", "conflicts_with_slug"
        )
        assignments, ignored_assignments = _assignments(
            records["task_assignments"]
        )
        locks, ignored_locks = _locks(records["locks"])

        cards: list[FrogTransitionCard] = []
        for task in records["tasks"]:
            if repo_path is not None and task.get("repo_path") != repo_path:
                continue
            slug = _required_text(task, "slug")
            status = _required_text(task, "workflow_status").casefold()
            try:
                eligibility = self._planning.eligibility(snapshot_digest, slug)
            except FrogPlanningError as error:
                raise FrogProjectionError(str(error)) from error
            reasons = list(eligibility.reasons)
            known_status = status in _DONE | _ACTIVE | _BACKLOG | {"review", "blocked"}
            if not known_status:
                reasons.append(f"unknown source status: {status}")
            source_assignee = _optional_text(task.get("assigned_agent"))
            observed_assignees = set(assignments.get(slug, ()))
            if source_assignee is not None:
                observed_assignees.add(source_assignee)
            attention = _attention(status, tuple(reasons))
            cards.append(
                FrogTransitionCard(
                    task_slug=slug,
                    title=_required_text(task, "title"),
                    repo_path=_optional_text(task.get("repo_path")),
                    lane=_lane(status),
                    workflow_status=status,
                    git_status=_optional_text(task.get("git_status")),
                    priority=_required_text(task, "priority").casefold(),
                    created_at=_required_text(task, "created_at"),
                    eligible=not reasons,
                    eligibility_reasons=tuple(reasons),
                    dependencies=tuple(sorted(dependencies.get(slug, ()))),
                    conflicts=tuple(sorted(conflicts.get(slug, ()))),
                    assignment_observations=tuple(sorted(observed_assignees)),
                    lock_observations=tuple(locks.get(slug, ())),
                    attention=attention,
                )
            )

        ordered = tuple(sorted(cards, key=_card_sort_key))
        age = (generated_at - receipt.captured_at).total_seconds()
        return FrogTransitionProjection(
            snapshot_digest=receipt.digest,
            source_label=receipt.source_label,
            captured_at=receipt.captured_at,
            imported_at=receipt.imported_at,
            generated_at=generated_at,
            stale_after_seconds=stale_after_seconds,
            stale=age > stale_after_seconds,
            repo_path=repo_path,
            ignored_assignment_records=ignored_assignments,
            ignored_lock_records=ignored_locks,
            cards=ordered,
        )


def frog_transition_projection_to_payload(
    value: FrogTransitionProjection,
) -> dict[str, Any]:
    lane_counts = {
        lane.value: sum(card.lane == lane for card in value.cards)
        for lane in FrogTransitionLane
    }
    return {
        "schema": FROG_TRANSITION_PROJECTION_SCHEMA,
        "generated_at": value.generated_at.isoformat(),
        "source": {
            "kind": "frog_snapshot_receipt",
            "label": value.source_label,
            "digest": value.snapshot_digest,
            "captured_at": value.captured_at.isoformat(),
            "imported_at": value.imported_at.isoformat(),
            "stale_after_seconds": value.stale_after_seconds,
            "stale": value.stale,
        },
        "authority": {
            "source_intent": "frog",
            "coordination": "observation_only",
            "promotion_and_claim": "explicit_weftmark_actions_required",
        },
        "filter": {"repo_path": value.repo_path},
        "counts": {
            "cards": len(value.cards),
            "lanes": lane_counts,
            "ignored_observations": {
                "assignments": value.ignored_assignment_records,
                "locks": value.ignored_lock_records,
            },
        },
        "cards": [
            {
                "id": card.task_slug,
                "title": card.title,
                "repo_path": card.repo_path,
                "lane": card.lane.value,
                "source": {
                    "workflow_status": card.workflow_status,
                    "git_status": card.git_status,
                    "priority": card.priority,
                    "created_at": card.created_at,
                },
                "planning": {
                    "eligible": card.eligible,
                    "reasons": list(card.eligibility_reasons),
                    "dependencies": list(card.dependencies),
                    "conflicts": list(card.conflicts),
                },
                "observations": {
                    "assignments": list(card.assignment_observations),
                    "locks": [
                        {"id": lock.lock_id, "status": lock.status}
                        for lock in card.lock_observations
                    ],
                },
                "attention": list(card.attention),
            }
            for card in value.cards
        ],
    }


_DONE = frozenset({"done", "cancelled", "abandoned", "archived"})
_ACTIVE = frozenset({"in_progress", "in-progress", "doing", "wip", "active", "started"})
_BACKLOG = frozenset({"idea", "todo", "planned", "backlog"})


def _lane(status: str) -> FrogTransitionLane:
    if status in _DONE:
        return FrogTransitionLane.DONE
    if status in _ACTIVE:
        return FrogTransitionLane.ACTIVE
    if status in _BACKLOG:
        return FrogTransitionLane.BACKLOG
    return FrogTransitionLane.REVIEW


def _attention(status: str, reasons: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    if status == "blocked":
        values.append("blocked")
    elif status not in _DONE | _ACTIVE | _BACKLOG | {"review"}:
        values.append("unknown_source_status")
    if any(reason.startswith("dependencies not done:") for reason in reasons):
        values.append("dependencies_unmet")
    if any(reason.startswith("source conflicts in progress:") for reason in reasons):
        values.append("active_conflict")
    return tuple(values)


def _relations(
    values: list[Mapping[str, Any]], left: str, right: str, *, relation: str
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for value in values:
        if value.get("relation") == relation:
            result.setdefault(_required_text(value, left), set()).add(
                _required_text(value, right)
            )
    return result


def _symmetric_relations(
    values: list[Mapping[str, Any]], left: str, right: str
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for value in values:
        left_value = _required_text(value, left)
        right_value = _required_text(value, right)
        result.setdefault(left_value, set()).add(right_value)
        result.setdefault(right_value, set()).add(left_value)
    return result


def _assignments(
    values: list[Mapping[str, Any]],
) -> tuple[dict[str, set[str]], int]:
    result: dict[str, set[str]] = {}
    ignored = 0
    for value in values:
        task_slug = _try_text(value, "task_slug")
        agent_name = _try_text(value, "agent_name")
        if task_slug is None or agent_name is None:
            ignored += 1
            continue
        result.setdefault(task_slug, set()).add(agent_name)
    return result, ignored


def _locks(
    values: list[Mapping[str, Any]],
) -> tuple[dict[str, list[FrogLockObservation]], int]:
    result: dict[str, list[FrogLockObservation]] = {}
    ignored = 0
    for value in values:
        scope_key = _try_text(value, "scope_key")
        lock_id = _try_text(value, "id")
        status = _try_text(value, "status")
        if scope_key is None or lock_id is None or status is None:
            ignored += 1
            continue
        if not scope_key.startswith("task:") or not scope_key.removeprefix("task:"):
            continue
        slug = scope_key.removeprefix("task:")
        result.setdefault(slug, []).append(
            FrogLockObservation(
                lock_id=lock_id,
                status=status.casefold(),
            )
        )
    for observations in result.values():
        observations.sort(key=lambda value: (value.status, value.lock_id))
    return result, ignored


def _card_sort_key(card: FrogTransitionCard) -> tuple[int, int, str, str]:
    lane_rank = {
        FrogTransitionLane.ACTIVE: 0,
        FrogTransitionLane.REVIEW: 1,
        FrogTransitionLane.BACKLOG: 2,
        FrogTransitionLane.DONE: 3,
    }
    priority = (
        int(card.priority[1:])
        if card.priority.startswith("p") and card.priority[1:].isdigit()
        else 9
    )
    return lane_rank[card.lane], priority, card.created_at, card.task_slug


def _required_text(value: Mapping[str, Any], name: str) -> str:
    if name not in value:
        raise FrogProjectionError(f"Frog record lacks {name}")
    text = str(value[name]).strip()
    if not text:
        raise FrogProjectionError(f"Frog record has empty {name}")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _try_text(value: Mapping[str, Any], name: str) -> str | None:
    if name not in value or value[name] is None:
        return None
    return _optional_text(value[name])


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FrogProjectionError(f"{name} must include a timezone")
