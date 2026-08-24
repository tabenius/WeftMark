from __future__ import annotations

from datetime import datetime, timezone

from weftmark.application.kanban_projection import (
    KANBAN_PROJECTION_SCHEMA,
    KanbanAttention,
    KanbanLane,
    KanbanPlanCardProjection,
    kanban_projection_to_payload,
    project_change_set,
    project_workspace,
)
from weftmark.application.status import (
    ChangeSetStatus,
    ScopeCollision,
    TaskChangeSetLink,
    TaskSource,
    TaskStatus,
    WorkspaceStatus,
)
from weftmark.domain.scope import Scope


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
    scope_collisions: tuple[ScopeCollision, ...] = (),
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
        scope_collisions=scope_collisions,
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


def test_scope_collision_is_projected_as_actionable_attention() -> None:
    collision = ScopeCollision(
        claim_id="claim-owner",
        competing_change_set_id="chg-owner",
        requested_scope=Scope.contract("tenant-auth"),
        owned_scope=Scope.contract("tenant-auth"),
    )
    workspace = WorkspaceStatus(
        generated_at=NOW,
        change_sets=(
            status(
                id="blocked",
                lifecycle_state="active",
                scope_collisions=(collision,),
            ),
        ),
        active_claim_count=1,
        expired_claim_count=0,
        released_claim_count=0,
    )

    card = project_change_set(workspace.change_sets[0])
    payload = kanban_projection_to_payload(project_workspace(workspace))["cards"][0]

    assert KanbanAttention.SCOPE_COLLISION in card.attention
    assert payload["scope_collisions"] == [
        {
            "claim_id": "claim-owner",
            "competing_change_set_id": "chg-owner",
            "requested_scope": {"kind": "contract", "key": "tenant-auth"},
            "owned_scope": {"kind": "contract", "key": "tenant-auth"},
        }
    ]


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
    assert payload["cards"][0]["scope_collisions"] == []
    assert payload["cards"][0]["git"]["head_sha"] == "head-active"


def test_projection_adds_source_labelled_plan_cards_and_explicit_links() -> None:
    task = TaskStatus(
        id="plan-work",
        title="Plan work",
        state="in_progress",
        priority="p0",
        created_at=NOW,
        updated_at=NOW,
        dependencies=("done-dependency",),
        conflicts=("coupled-work",),
        sources=(TaskSource("source_plan", "weftmark/tasks", "sha256:abc"),),
    )
    link = TaskChangeSetLink(
        "plan-work", "active", "claim-plan-work", "completed"
    )
    workspace = WorkspaceStatus(
        generated_at=NOW,
        change_sets=(status(id="active", lifecycle_state="active"),),
        active_claim_count=1,
        expired_claim_count=0,
        released_claim_count=0,
        tasks=(task,),
        task_change_set_links=(link,),
    )

    projection = project_workspace(workspace)
    payload = kanban_projection_to_payload(projection)

    assert isinstance(projection.plan_cards[0], KanbanPlanCardProjection)
    assert payload["counts"] == {
        "cards": 1,
        "plan_cards": 1,
        "total_cards": 2,
        "active_claims": 1,
        "expired_claims": 0,
        "released_claims": 0,
    }
    assert payload["plan_cards"] == [
        {
            "kind": "task",
            "id": "plan-work",
            "title": "Plan work",
            "lane": "active",
            "task_state": "in_progress",
            "priority": "p0",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "planning": {
                "dependencies": ["done-dependency"],
                "conflicts": ["coupled-work"],
            },
            "sources": [
                {
                    "kind": "source_plan",
                    "label": "weftmark/tasks",
                    "digest": "sha256:abc",
                }
            ],
            "change_set_ids": ["active"],
            "attention": [],
        }
    ]
    assert payload["task_change_set_links"] == [
        {
            "task_id": "plan-work",
            "change_set_id": "active",
            "claim_id": "claim-plan-work",
            "binding_state": "completed",
        }
    ]
    assert payload["cards"][0]["kind"] == "change_set"


def test_unlinked_active_task_fails_visible_without_inventing_change_set() -> None:
    task = TaskStatus(
        id="orphan",
        title="Orphan",
        state="in_progress",
        priority="p1",
        created_at=NOW,
        updated_at=NOW,
        dependencies=(),
        conflicts=(),
        sources=(TaskSource("native", "native-ledger", None),),
    )
    workspace = WorkspaceStatus(NOW, (), 0, 0, 0, tasks=(task,))

    payload = kanban_projection_to_payload(project_workspace(workspace))

    assert payload["plan_cards"][0]["attention"] == ["missing_change_set_link"]
    assert payload["plan_cards"][0]["change_set_ids"] == []


def test_dangling_task_link_is_visible_without_inventing_change_set() -> None:
    task = TaskStatus(
        id="dangling",
        title="Dangling",
        state="in_progress",
        priority="p1",
        created_at=NOW,
        updated_at=NOW,
        dependencies=(),
        conflicts=(),
        sources=(TaskSource("native", "native-ledger", None),),
    )
    link = TaskChangeSetLink("dangling", "absent", "claim-dangling", "reserved")
    workspace = WorkspaceStatus(
        NOW,
        (),
        1,
        0,
        0,
        tasks=(task,),
        task_change_set_links=(link,),
    )

    payload = kanban_projection_to_payload(project_workspace(workspace))

    assert payload["plan_cards"][0]["attention"] == ["missing_change_set"]
    assert payload["plan_cards"][0]["change_set_ids"] == ["absent"]
    assert payload["cards"] == []
