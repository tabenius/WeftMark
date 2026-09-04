from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from weftmark.application.status import ChangeSetStatus, ScopeCollision
from weftmark.domain.scope import Scope
from weftmark.tui.formatting import (
    attention_rank,
    blockers_text,
    detail_text,
    evidence_summary,
    sort_statuses,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def make_status(
    id: str,
    *,
    reviewed: bool = False,
    scope_collisions: tuple[ScopeCollision, ...] = (),
) -> ChangeSetStatus:
    return ChangeSetStatus(
        id=id,
        goal=f"Goal for {id}",
        lifecycle_state="active",
        branch="main",
        observed_head_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        observed_at=NOW,
        dirty_paths=(),
        active_claim_ids=(),
        evidence_count=2,
        current_evidence_count=2,
        obsolete_evidence_count=0,
        failed_evidence_count=0,
        unavailable_evidence_count=0,
        latest_review_id="rev-1" if reviewed else None,
        latest_review_outcome="ready" if reviewed else None,
        latest_review_head_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        if reviewed
        else None,
        latest_review_is_current=reviewed,
        latest_handoff_id=None,
        latest_handoff_head_sha=None,
        latest_handoff_is_current=False,
        scope_collisions=scope_collisions,
    )


def make_collision(claim_id: str = "other-claim") -> ScopeCollision:
    return ScopeCollision(
        claim_id=claim_id,
        competing_change_set_id="other-cs",
        requested_scope=Scope.file("a.py"),
        owned_scope=Scope.contract("api-v1"),
    )


def test_attention_rank_prioritizes_blockers_over_unready_over_ready() -> None:
    blocked = make_status("blocked-cs", reviewed=True, scope_collisions=(make_collision(),))
    unready = make_status("unready-cs")
    ready = make_status("ready-cs", reviewed=True)

    assert attention_rank(blocked) == 0
    assert attention_rank(unready) == 1
    assert attention_rank(ready) == 2


def test_sort_statuses_orders_blockers_then_unready_then_ready_by_id() -> None:
    ready_b = make_status("ready-b", reviewed=True)
    ready_a = make_status("ready-a", reviewed=True)
    unready = make_status("unready")
    blocked = make_status("blocked", reviewed=True, scope_collisions=(make_collision(),))

    ordered = sort_statuses((ready_b, ready_a, unready, blocked))

    assert [item.id for item in ordered] == ["blocked", "unready", "ready-a", "ready-b"]


def test_evidence_summary_reports_current_over_total_and_failures() -> None:
    healthy = make_status("cs-1")
    assert evidence_summary(healthy) == "2/2"

    failing = replace(healthy, failed_evidence_count=1)
    assert evidence_summary(failing) == "2/2 (1 failed)"


def test_blockers_text_describes_each_collision() -> None:
    blocked = make_status(
        "blocked-cs", reviewed=True, scope_collisions=(make_collision(),)
    )

    assert blockers_text(blocked) == (
        "blocked by claim other-claim (other-cs) on contract:api-v1",
    )


def test_detail_text_includes_goal_state_evidence_and_blockers() -> None:
    blocked = make_status(
        "blocked-cs", reviewed=True, scope_collisions=(make_collision(),)
    )

    text = detail_text(blocked)

    assert "blocked-cs" in text
    assert "Goal for blocked-cs" in text
    assert "evidence: 2/2" in text
    assert "blocked by claim other-claim" in text


def test_detail_text_reports_no_review_and_no_handoff_when_absent() -> None:
    unready = make_status("unready-cs")

    text = detail_text(unready)

    assert "review: none" in text
    assert "handoff: none" in text
