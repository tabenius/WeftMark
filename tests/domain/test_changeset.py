from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from weftmark.domain.changeset import (
    ChangeSet,
    ChangeSetError,
    ChangeSetState,
    InvalidLineageChange,
    InvalidTransition,
    LineageEventKind,
)


NOW = datetime(2026, 8, 13, 21, 0, tzinfo=timezone.utc)


def planned() -> ChangeSet:
    return ChangeSet.plan(
        id="chg_01",
        goal="Prove tenant authentication",
        repository_id="tabenius/example",
        base_sha="a" * 40,
        branch="weft/tenant-auth",
        worktree="/worktrees/chg_01",
        scopes=("src/ingress/**", "contract:tenant-authentication"),
        at=NOW,
    )


def test_plan_binds_intent_context_scope_state_and_timestamps() -> None:
    change_set = planned()

    assert change_set.state is ChangeSetState.PLANNED
    assert change_set.head_sha == change_set.base_sha
    assert change_set.scopes == (
        "src/ingress/**",
        "contract:tenant-authentication",
    )
    assert change_set.created_at == NOW
    assert change_set.updated_at == NOW


def test_activation_and_review_merge_close_lifecycle() -> None:
    active = planned().activate(head_sha="b" * 40)
    review = active.transition(ChangeSetState.REVIEW)
    merged = review.transition(ChangeSetState.MERGED)
    closed = merged.transition(ChangeSetState.CLOSED)

    assert closed.state is ChangeSetState.CLOSED
    assert active.lineage[-1].kind is LineageEventKind.ACTIVATED


def test_review_can_return_to_active_but_closed_cannot() -> None:
    review = planned().activate().transition(ChangeSetState.REVIEW)
    assert review.transition(ChangeSetState.ACTIVE).state is ChangeSetState.ACTIVE

    closed = review.transition(ChangeSetState.MERGED).transition(ChangeSetState.CLOSED)
    with pytest.raises(InvalidTransition):
        closed.transition(ChangeSetState.ACTIVE)


def test_abandoned_change_set_is_terminal() -> None:
    abandoned = planned().transition(ChangeSetState.ABANDONED)

    with pytest.raises(InvalidTransition):
        abandoned.transition(ChangeSetState.ACTIVE)


def test_rebase_is_an_explicit_append_only_lineage_event() -> None:
    active = planned().activate()
    rebased = active.rebase("c" * 40, head_sha="d" * 40)

    assert rebased.repository_id == active.repository_id
    assert rebased.base_sha == "c" * 40
    assert rebased.lineage[-1].kind is LineageEventKind.REBASED
    assert rebased.lineage[-1].previous_base_sha == "a" * 40


def test_branch_movement_requires_an_explicit_lineage_operation() -> None:
    active = planned().activate()
    moved = active.move_branch("weft/tenant-auth-v2", head_sha="e" * 40)

    assert moved.branch == "weft/tenant-auth-v2"
    assert moved.lineage[-1].kind is LineageEventKind.BRANCH_MOVED
    assert moved.lineage[-1].previous_branch == "weft/tenant-auth"


def test_lineage_cannot_move_before_activation_or_after_merge() -> None:
    with pytest.raises(InvalidLineageChange):
        planned().advance_head("b" * 40)

    merged = (
        planned()
        .activate()
        .transition(ChangeSetState.REVIEW)
        .transition(ChangeSetState.MERGED)
    )
    with pytest.raises(InvalidLineageChange):
        merged.advance_head("b" * 40)


def test_amend_scope_widens_declared_scope_as_an_explicit_append_only_event() -> None:
    active = planned().activate()

    amended = active.amend_scope(
        ("file:docs/dogfood-log.md",), reason="documenting this session's dogfood exercise"
    )

    assert amended.scopes == (
        "contract:tenant-authentication",
        "file:docs/dogfood-log.md",
        "src/ingress/**",
    )
    assert active.scopes == ("src/ingress/**", "contract:tenant-authentication")
    assert amended.scope_amendments[-1].added_scopes == ("file:docs/dogfood-log.md",)
    assert amended.scope_amendments[-1].reason == "documenting this session's dogfood exercise"


def test_amend_scope_ignores_already_declared_scopes_but_requires_something_new() -> None:
    active = planned().activate()

    with pytest.raises(ChangeSetError):
        active.amend_scope(("src/ingress/**",), reason="already covered")


def test_amend_scope_rejects_empty_reason_or_scopes() -> None:
    active = planned().activate()

    with pytest.raises(ChangeSetError):
        active.amend_scope((), reason="need something")
    with pytest.raises(ChangeSetError):
        active.amend_scope(("file:new/**",), reason="   ")


def test_amend_scope_cannot_move_before_activation_or_after_merge() -> None:
    with pytest.raises(InvalidLineageChange):
        planned().amend_scope(("file:new/**",), reason="too early")

    merged = (
        planned()
        .activate()
        .transition(ChangeSetState.REVIEW)
        .transition(ChangeSetState.MERGED)
    )
    with pytest.raises(InvalidLineageChange):
        merged.amend_scope(("file:new/**",), reason="too late")


def test_noop_lineage_events_are_rejected() -> None:
    active = planned().activate()

    with pytest.raises(InvalidLineageChange):
        active.advance_head(active.head_sha)
    with pytest.raises(InvalidLineageChange):
        active.move_branch(active.branch, head_sha=active.head_sha)
    with pytest.raises(InvalidLineageChange):
        active.rebase(active.base_sha, head_sha=active.head_sha)


def test_snapshots_are_immutable() -> None:
    change_set = planned()

    with pytest.raises(FrozenInstanceError):
        change_set.repository_id = "other/repository"  # type: ignore[misc]


def test_identity_and_timestamps_fail_closed() -> None:
    with pytest.raises(ChangeSetError, match="id must not be empty"):
        ChangeSet.plan(
            id="",
            goal="goal",
            repository_id="repo",
            base_sha="abc",
            branch="branch",
            worktree="/tmp/worktree",
            at=NOW,
        )

    with pytest.raises(ChangeSetError, match="timezone"):
        ChangeSet.plan(
            id="chg",
            goal="goal",
            repository_id="repo",
            base_sha="abc",
            branch="branch",
            worktree="/tmp/worktree",
            at=NOW.replace(tzinfo=None),
        )


def test_transition_and_lineage_timestamps_are_monotonic() -> None:
    active = planned().activate(at=NOW + timedelta(minutes=1))

    with pytest.raises(ChangeSetError, match="must not precede"):
        active.transition(ChangeSetState.REVIEW, at=NOW)
    with pytest.raises(ChangeSetError, match="must not precede"):
        active.advance_head("b" * 40, at=NOW)
