"""Stable read-only projection for Kanban-style board clients.

The projection deliberately depends on the existing StatusService read model.
It does not own Change Set state, Git state, evidence, review, or handoff data.
That keeps external boards replaceable while giving them one versioned payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from weftmark.application.status import (
    ChangeSetStatus,
    ScopeCollision,
    TaskChangeSetLink,
    TaskSource,
    TaskStatus,
    WorkspaceStatus,
)


KANBAN_PROJECTION_SCHEMA = "weftmark.kanban-projection.v0"


class KanbanLane(StrEnum):
    BACKLOG = "backlog"
    ACTIVE = "active"
    REVIEW = "review"
    READY = "ready"
    DONE = "done"


class KanbanAttention(StrEnum):
    DIRTY_WORKTREE = "dirty_worktree"
    OBSOLETE_EVIDENCE = "obsolete_evidence"
    FAILED_EVIDENCE = "failed_evidence"
    UNAVAILABLE_EVIDENCE = "unavailable_evidence"
    SCOPE_COLLISION = "scope_collision"
    BLOCKED = "blocked"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    STALE_REVIEW = "stale_review"
    STALE_HANDOFF = "stale_handoff"
    UNKNOWN_LIFECYCLE = "unknown_lifecycle_state"


@dataclass(frozen=True, slots=True)
class KanbanCardProjection:
    change_set_id: str
    title: str
    lane: KanbanLane
    lifecycle_state: str
    readiness: str
    branch: str
    head_sha: str
    observed_at: datetime
    dirty_paths: tuple[str, ...]
    active_claim_ids: tuple[str, ...]
    evidence_total: int
    evidence_current: int
    evidence_obsolete: int
    evidence_failed: int
    evidence_unavailable: int
    latest_review_id: str | None
    latest_review_outcome: str | None
    latest_review_head_sha: str | None
    latest_review_is_current: bool
    latest_handoff_id: str | None
    latest_handoff_head_sha: str | None
    latest_handoff_is_current: bool
    attention: tuple[KanbanAttention, ...]
    scope_collisions: tuple[ScopeCollision, ...] = ()


@dataclass(frozen=True, slots=True)
class KanbanPlanCardProjection:
    task_id: str
    title: str
    lane: KanbanLane
    task_state: str
    priority: str
    created_at: datetime
    updated_at: datetime
    dependencies: tuple[str, ...]
    conflicts: tuple[str, ...]
    sources: tuple[TaskSource, ...]
    change_set_ids: tuple[str, ...]
    attention: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KanbanProjection:
    generated_at: datetime
    active_claim_count: int
    expired_claim_count: int
    released_claim_count: int
    cards: tuple[KanbanCardProjection, ...]
    plan_cards: tuple[KanbanPlanCardProjection, ...] = ()
    task_change_set_links: tuple[TaskChangeSetLink, ...] = ()


def _lane_for(status: ChangeSetStatus) -> KanbanLane:
    if status.lifecycle_state == "planned":
        return KanbanLane.BACKLOG
    if status.lifecycle_state == "active":
        return KanbanLane.ACTIVE
    if status.lifecycle_state == "review":
        if status.readiness in {"ready", "ready_with_follow_up"}:
            return KanbanLane.READY
        return KanbanLane.REVIEW
    if status.lifecycle_state in {"merged", "closed", "abandoned"}:
        return KanbanLane.DONE
    # A newer server lifecycle must not make an older board silently call work done.
    return KanbanLane.REVIEW


def _attention_for(status: ChangeSetStatus) -> tuple[KanbanAttention, ...]:
    values: list[KanbanAttention] = []
    if status.lifecycle_state not in {
        "planned",
        "active",
        "review",
        "merged",
        "closed",
        "abandoned",
    }:
        values.append(KanbanAttention.UNKNOWN_LIFECYCLE)
    if status.dirty_paths:
        values.append(KanbanAttention.DIRTY_WORKTREE)
    if status.obsolete_evidence_count:
        values.append(KanbanAttention.OBSOLETE_EVIDENCE)
    if status.failed_evidence_count:
        values.append(KanbanAttention.FAILED_EVIDENCE)
    if status.unavailable_evidence_count:
        values.append(KanbanAttention.UNAVAILABLE_EVIDENCE)
    if status.scope_collisions:
        values.append(KanbanAttention.SCOPE_COLLISION)
    if status.readiness == "blocked":
        values.append(KanbanAttention.BLOCKED)
    if status.readiness == "evidence_incomplete":
        values.append(KanbanAttention.EVIDENCE_INCOMPLETE)
    if status.latest_review_id is not None and not status.latest_review_is_current:
        values.append(KanbanAttention.STALE_REVIEW)
    if status.latest_handoff_id is not None and not status.latest_handoff_is_current:
        values.append(KanbanAttention.STALE_HANDOFF)
    return tuple(values)


def project_change_set(status: ChangeSetStatus) -> KanbanCardProjection:
    """Project one authoritative WeftMark status record into board-facing state."""

    return KanbanCardProjection(
        change_set_id=status.id,
        title=status.goal,
        lane=_lane_for(status),
        lifecycle_state=status.lifecycle_state,
        readiness=status.readiness,
        branch=status.branch,
        head_sha=status.observed_head_sha,
        observed_at=status.observed_at,
        dirty_paths=status.dirty_paths,
        active_claim_ids=status.active_claim_ids,
        evidence_total=status.evidence_count,
        evidence_current=status.current_evidence_count,
        evidence_obsolete=status.obsolete_evidence_count,
        evidence_failed=status.failed_evidence_count,
        evidence_unavailable=status.unavailable_evidence_count,
        latest_review_id=status.latest_review_id,
        latest_review_outcome=status.latest_review_outcome,
        latest_review_head_sha=status.latest_review_head_sha,
        latest_review_is_current=status.latest_review_is_current,
        latest_handoff_id=status.latest_handoff_id,
        latest_handoff_head_sha=status.latest_handoff_head_sha,
        latest_handoff_is_current=status.latest_handoff_is_current,
        attention=_attention_for(status),
        scope_collisions=status.scope_collisions,
    )


def project_workspace(status: WorkspaceStatus) -> KanbanProjection:
    """Build the v0 external-board read model without mutating workspace state."""

    links_by_task: dict[str, list[str]] = {}
    change_set_ids = {value.id for value in status.change_sets}
    for link in status.task_change_set_links:
        links_by_task.setdefault(link.task_id, []).append(link.change_set_id)
    return KanbanProjection(
        generated_at=status.generated_at,
        active_claim_count=status.active_claim_count,
        expired_claim_count=status.expired_claim_count,
        released_claim_count=status.released_claim_count,
        cards=tuple(project_change_set(value) for value in status.change_sets),
        plan_cards=tuple(
            _project_task(
                value,
                tuple(sorted(links_by_task.get(value.id, ()))),
                change_set_ids,
            )
            for value in sorted(status.tasks, key=_task_sort_key)
        ),
        task_change_set_links=status.task_change_set_links,
    )


def _task_sort_key(value: TaskStatus) -> tuple[int, datetime, str]:
    priority = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}.get(value.priority, 4)
    return priority, value.created_at, value.id


def _project_task(
    task: TaskStatus,
    linked_change_sets: tuple[str, ...],
    known_change_sets: set[str],
) -> KanbanPlanCardProjection:
    if task.state in {"idea", "todo"}:
        lane = KanbanLane.BACKLOG
    elif task.state == "in_progress":
        lane = KanbanLane.ACTIVE
    elif task.state in {"done", "abandoned"}:
        lane = KanbanLane.DONE
    else:
        lane = KanbanLane.REVIEW
    attention: list[str] = []
    if task.state == "blocked":
        attention.append("blocked")
    if task.state == "in_progress" and not linked_change_sets:
        attention.append("missing_change_set_link")
    if any(value not in known_change_sets for value in linked_change_sets):
        attention.append("missing_change_set")
    return KanbanPlanCardProjection(
        task_id=task.id,
        title=task.title,
        lane=lane,
        task_state=task.state,
        priority=task.priority,
        created_at=task.created_at,
        updated_at=task.updated_at,
        dependencies=task.dependencies,
        conflicts=task.conflicts,
        sources=task.sources,
        change_set_ids=linked_change_sets,
        attention=tuple(attention),
    )


def _collision_to_payload(value: ScopeCollision) -> dict[str, Any]:
    return {
        "claim_id": value.claim_id,
        "competing_change_set_id": value.competing_change_set_id,
        "requested_scope": value.requested_scope.to_dict(),
        "owned_scope": value.owned_scope.to_dict(),
    }


def kanban_projection_to_payload(projection: KanbanProjection) -> dict[str, Any]:
    """Serialize the versioned projection using JSON-compatible primitives."""

    return {
        "schema": KANBAN_PROJECTION_SCHEMA,
        "generated_at": projection.generated_at.isoformat(),
        "authority": {
            "coordination": "weftmark",
            "projection": "read_only",
        },
        "counts": {
            "cards": len(projection.cards),
            **(
                {
                    "plan_cards": len(projection.plan_cards),
                    "total_cards": len(projection.cards) + len(projection.plan_cards),
                }
                if projection.plan_cards or projection.task_change_set_links
                else {}
            ),
            "active_claims": projection.active_claim_count,
            "expired_claims": projection.expired_claim_count,
            "released_claims": projection.released_claim_count,
        },
        "task_change_set_links": [
            {
                "task_id": value.task_id,
                "change_set_id": value.change_set_id,
                "claim_id": value.claim_id,
                "binding_state": value.binding_state,
            }
            for value in projection.task_change_set_links
        ],
        "plan_cards": [
            {
                "kind": "task",
                "id": card.task_id,
                "title": card.title,
                "lane": card.lane.value,
                "task_state": card.task_state,
                "priority": card.priority,
                "created_at": card.created_at.isoformat(),
                "updated_at": card.updated_at.isoformat(),
                "planning": {
                    "dependencies": list(card.dependencies),
                    "conflicts": list(card.conflicts),
                },
                "sources": [
                    {"kind": value.kind, "label": value.label, "digest": value.digest}
                    for value in card.sources
                ],
                "change_set_ids": list(card.change_set_ids),
                "attention": list(card.attention),
            }
            for card in projection.plan_cards
        ],
        "cards": [
            {
                "kind": "change_set",
                "id": card.change_set_id,
                "title": card.title,
                "lane": card.lane.value,
                "lifecycle_state": card.lifecycle_state,
                "readiness": card.readiness,
                "git": {
                    "branch": card.branch,
                    "head_sha": card.head_sha,
                    "observed_at": card.observed_at.isoformat(),
                    "dirty_paths": list(card.dirty_paths),
                },
                "claims": {
                    "active_ids": list(card.active_claim_ids),
                },
                "scope_collisions": [
                    _collision_to_payload(value) for value in card.scope_collisions
                ],
                "evidence": {
                    "total": card.evidence_total,
                    "current": card.evidence_current,
                    "obsolete": card.evidence_obsolete,
                    "failed": card.evidence_failed,
                    "unavailable": card.evidence_unavailable,
                },
                "review": (
                    None
                    if card.latest_review_id is None
                    else {
                        "id": card.latest_review_id,
                        "outcome": card.latest_review_outcome,
                        "head_sha": card.latest_review_head_sha,
                        "is_current": card.latest_review_is_current,
                    }
                ),
                "handoff": (
                    None
                    if card.latest_handoff_id is None
                    else {
                        "id": card.latest_handoff_id,
                        "head_sha": card.latest_handoff_head_sha,
                        "is_current": card.latest_handoff_is_current,
                    }
                ),
                "attention": [value.value for value in card.attention],
            }
            for card in projection.cards
        ],
    }
