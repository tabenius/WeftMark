from __future__ import annotations

from datetime import datetime, timezone

from weftmark.application.kanban_projection import (
    KANBAN_PROJECTION_SCHEMA,
    KanbanAttention,
    KanbanLane,
    kanban_projection_to_payload,
    project_change_set,
    project_workspace,
)
from weftmark.application.status import ChangeSetStatus, WorkspaceStatus


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def status(
    *,
    id: str,
    lifecycle_state: str,
    readiness: str = "unreviewed",
    dirty_paths: tuple[str, ...] = (),
    obsolete_evidence_count: int = 0,
    failed_evidence_count: int = 0,
    unavailable_evidence_count: int = 0,
    latest_review_id: str | None = None,
    latest_review_outcome: str | None = None,
    latest_review_is_current: bool = False,
    latest_handoff_id: str | None = None,
    latest_handoff_is_current: bool = False,
) -> ChangeSetStatus:
    return ChangeSetStatus(
        id=id,
        goal=f"Goal {id}",
        lifecycle_state=lifecycle_state,
        branch=f"weft/{id}",
        observed_head_sha=f"head-{id}",
        observed_at=NOW,
        dirty_paths=dirty_paths,
        active_claim_ids=(f"claim-{id}",) if lifecycle_state == "active" else (),
        evidence_count=2,
        current_evidence_count=2 - obsolete_evidence_count,
        obsolete_evidence_count=obsolete_evidence_count,
        failed_evidence_count=failed_evidence_count,
        unavailable_evidence_count=unavailable_evidence_count,
        latest_review_id=latest_review_id,
        latest_review_outcome=latest_review_outcome,
        latest_review_head_sha=(
            None if latest_review_id is None else f"review-head-{id}"
        ),
        latest_review_is_current=latest_review_is_current,
        latest_handoff_id=latest_handoff_id,
        latest_handoff_head_sha=(
            None if latest_handoff_id is None else f"handoff-head-{id}"
        ),
        latest_handoff_is_current=latest_handoff_is_current,
    )


def test_projection_maps_domain_lifecycle_to_small_board_lane_set() -> None:
    values = (
        status(id="planned", lifecycle_state="planned"),
        status(id="active", lifecycle_state="active"),
        status(
            id="review",
            lifecycle_state="review",
            readiness="evidence_incomplete",
            latest_review_id="review-review",
            latest_review_outcome="evidence_incomplete",
            latest_review_is_current=True,
        ),
        status(
            id="ready",
            lifecycle_state="review",
            readiness="ready",
            latest_review_id="review-ready",
            latest_review_outcome="ready",
            latest_review_is_current=True,
        ),
        status(
            id="ready-follow-up",
            lifecycle_state="review",
            readiness="ready_with_follow_up",
            latest_review_id="review-ready-follow-up",
            latest_review_outcome="ready_with_follow_up",
            latest_review_is_current=True,
        ),
        status(id="merged", lifecycle_state="merged"),
    )

    lanes = tuple(project_change_set(value).lane for value in values)

    assert lanes == (
        KanbanLane.BACKLOG,
        KanbanLane.ACTIVE,
        KanbanLane.REVIEW,
        KanbanLane.READY,
        KanbanLane.READY,
        KanbanLane.DONE,
    )


def test_projection_preserves_authoritative_status_and_marks_attention() -> None:
    value = status(
        id="stale",
        lifecycle_state="review",
        readiness="stale",
        dirty_paths=("src/auth.py",),
        obsolete_evidence_count=1,
        failed_evidence_count=1,
        unavailable_evidence_count=1,
        latest_review_id="review-stale",
        latest_review_outcome="ready",
        latest_review_is_current=False,
        latest_handoff_id="handoff-stale",
        latest_handoff_is_current=False,
    )

    card = project_change_set(value)

    assert card.readiness == "stale"
    assert card.attention == (
        KanbanAttention.DIRTY_WORKTREE,
        KanbanAttention.OBSOLETE_EVIDENCE,
        KanbanAttention.FAILED_EVIDENCE,
        KanbanAttention.UNAVAILABLE_EVIDENCE,
        KanbanAttention.STALE_REVIEW,
        KanbanAttention.STALE_HANDOFF,
    )


def test_failed_evidence_is_visible_before_formal_review() -> None:
    card = project_change_set(
        status(
            id="failing",
            lifecycle_state="active",
            readiness="unreviewed",
            failed_evidence_count=1,
            unavailable_evidence_count=1,
        )
    )

    assert card.lane is KanbanLane.ACTIVE
    assert card.readiness == "unreviewed"
    assert KanbanAttention.FAILED_EVIDENCE in card.attention
    assert KanbanAttention.UNAVAILABLE_EVIDENCE in card.attention


def test_unknown_lifecycle_fails_safe_into_review_attention() -> None:
    card = project_change_set(status(id="future", lifecycle_state="future_state"))

    assert card.lane is KanbanLane.REVIEW
    assert KanbanAttention.UNKNOWN_LIFECYCLE in card.attention


def test_workspace_payload_is_versioned_read_only_and_json_compatible() -> None:
    workspace = WorkspaceStatus(
        generated_at=NOW,
        change_sets=(status(id="active", lifecycle_state="active"),),
        active_claim_count=1,
        expired_claim_count=2,
        released_claim_count=3,
    )

    payload = kanban_projection_to_payload(project_workspace(workspace))

    assert payload["schema"] == KANBAN_PROJECTION_SCHEMA
    assert payload["authority"] == {
        "coordination": "weftmark",
        "projection": "read_only",
    }
    assert payload["counts"] == {
        "cards": 1,
        "active_claims": 1,
        "expired_claims": 2,
        "released_claims": 3,
    }
    assert payload["cards"][0]["lane"] == "active"
    assert payload["cards"][0]["claims"]["active_ids"] == ["claim-active"]
    assert payload["cards"][0]["git"]["head_sha"] == "head-active"
