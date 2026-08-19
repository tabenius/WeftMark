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

from weftmark.application.status import ChangeSetStatus, WorkspaceStatus


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


@dataclass(frozen=True, slots=True)
class KanbanProjection:
    generated_at: datetime
    active_claim_count: int
    expired_claim_count: int
    released_claim_count: int
    cards: tuple[KanbanCardProjection, ...]


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
    )


def project_workspace(status: WorkspaceStatus) -> KanbanProjection:
    """Build the v0 external-board read model without mutating workspace state."""

    return KanbanProjection(
        generated_at=status.generated_at,
        active_claim_count=status.active_claim_count,
        expired_claim_count=status.expired_claim_count,
        released_claim_count=status.released_claim_count,
        cards=tuple(project_change_set(value) for value in status.change_sets),
    )


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
            "active_claims": projection.active_claim_count,
            "expired_claims": projection.expired_claim_count,
            "released_claims": projection.released_claim_count,
        },
        "cards": [
            {
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
