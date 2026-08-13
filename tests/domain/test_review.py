from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from weftmark.domain.review import (
    FindingEventKind,
    FindingSeverity,
    FindingStatus,
    InvalidFindingOperation,
    InvalidReviewDecision,
    ReviewDecision,
    ReviewError,
    ReviewFinding,
    ReviewOutcome,
)
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 13, 20, 0, tzinfo=timezone.utc)


def finding(
    id: str = "finding-1",
    *,
    severity: FindingSeverity = FindingSeverity.BLOCKING,
) -> ReviewFinding:
    return ReviewFinding(
        id=id,
        severity=severity,
        scope=Scope.contract("tenant-auth"),
        rationale="Tenant ownership is not checked",
        created_at=NOW,
        updated_at=NOW,
    )


def decision(
    outcome: ReviewOutcome,
    *,
    findings: tuple[ReviewFinding, ...] = (),
    at: datetime = NOW,
) -> ReviewDecision:
    return ReviewDecision(
        id="review-1",
        change_set_id="chg-1",
        author_id="reviewer-1",
        outcome=outcome,
        head_sha="a" * 40,
        evidence_ids=("ev-tests", "ev-security"),
        findings=findings,
        rationale="Reviewed declared scope and evidence",
        created_at=at,
    )


@pytest.mark.parametrize(
    "outcome",
    [
        ReviewOutcome.READY,
        ReviewOutcome.STALE,
        ReviewOutcome.EVIDENCE_INCOMPLETE,
    ],
)
def test_non_blocked_decision_states_are_persistent(outcome: ReviewOutcome) -> None:
    review = decision(outcome)

    assert review.outcome is outcome
    assert review.head_sha == "a" * 40
    assert review.evidence_ids == ("ev-tests", "ev-security")


def test_ready_with_follow_up_requires_open_non_blocking_finding() -> None:
    follow_up = finding("follow-up", severity=FindingSeverity.MEDIUM)

    review = decision(ReviewOutcome.READY_WITH_FOLLOW_UP, findings=(follow_up,))
    assert review.is_releasable

    with pytest.raises(InvalidReviewDecision, match="requires at least one"):
        decision(ReviewOutcome.READY_WITH_FOLLOW_UP)


def test_plain_ready_cannot_hide_an_open_non_blocking_follow_up() -> None:
    follow_up = finding("follow-up", severity=FindingSeverity.LOW)

    with pytest.raises(InvalidReviewDecision, match="ready-with-follow-up"):
        decision(ReviewOutcome.READY, findings=(follow_up,))


@pytest.mark.parametrize(
    "outcome", [ReviewOutcome.READY, ReviewOutcome.READY_WITH_FOLLOW_UP]
)
def test_unresolved_blocker_prevents_every_releasable_outcome(
    outcome: ReviewOutcome,
) -> None:
    findings = (finding(),)

    with pytest.raises(InvalidReviewDecision, match="unresolved blocking"):
        decision(outcome, findings=findings)


def test_blocked_outcome_requires_an_unresolved_blocker() -> None:
    blocker = finding()
    assert not decision(ReviewOutcome.BLOCKED, findings=(blocker,)).is_releasable

    with pytest.raises(InvalidReviewDecision, match="requires an unresolved"):
        decision(ReviewOutcome.BLOCKED)


def test_resolution_records_actor_rationale_time_and_finding() -> None:
    resolved = finding().resolve(
        actor_id="author-1",
        rationale="Ownership check added and tested",
        at=NOW + timedelta(minutes=5),
    )

    assert resolved.status is FindingStatus.RESOLVED
    assert resolved.events[-1].kind is FindingEventKind.RESOLVED
    assert resolved.events[-1].actor_id == "author-1"
    assert resolved.events[-1].rationale == "Ownership check added and tested"
    assert resolved.events[-1].finding_id == resolved.id


def test_waiver_is_auditable_and_allows_releasable_decision() -> None:
    waived = finding().waive(
        actor_id="security-owner",
        rationale="Accepted for isolated alpha fixture",
        at=NOW + timedelta(minutes=5),
    )

    review = decision(
        ReviewOutcome.READY,
        findings=(waived,),
        at=NOW + timedelta(minutes=6),
    )
    assert waived.status is FindingStatus.WAIVED
    assert waived.events[-1].kind is FindingEventKind.WAIVED
    assert waived.events[-1].actor_id == "security-owner"
    assert review.is_releasable


def test_waiver_cannot_omit_actor_rationale_or_timestamp_integrity() -> None:
    blocker = finding()

    with pytest.raises(ReviewError, match="actor"):
        blocker.waive(actor_id="", rationale="accepted")
    with pytest.raises(ReviewError, match="rationale"):
        blocker.waive(actor_id="owner", rationale="")
    with pytest.raises(InvalidFindingOperation, match="precedes"):
        blocker.waive(
            actor_id="owner",
            rationale="accepted",
            at=NOW - timedelta(seconds=1),
        )


def test_closed_finding_cannot_be_resolved_or_waived_again() -> None:
    resolved = finding().resolve(actor_id="author", rationale="fixed")

    with pytest.raises(InvalidFindingOperation, match="already resolved"):
        resolved.waive(actor_id="owner", rationale="not needed")


def test_decision_provenance_and_author_are_immutable() -> None:
    review = decision(ReviewOutcome.READY)

    with pytest.raises(FrozenInstanceError):
        review.author_id = "other-reviewer"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        review.head_sha = "b" * 40  # type: ignore[misc]


def test_duplicate_snapshot_ids_fail_closed() -> None:
    review = decision(ReviewOutcome.READY)

    with pytest.raises(InvalidReviewDecision, match="duplicate ids"):
        replace(review, evidence_ids=("ev-1", "ev-1"))
    with pytest.raises(InvalidReviewDecision, match="duplicate finding"):
        replace(review, findings=(finding(), finding()))


def test_inconsistent_terminal_finding_snapshots_fail_closed() -> None:
    waived = finding().waive(actor_id="owner", rationale="accepted")

    with pytest.raises(ReviewError, match="event time"):
        replace(waived, updated_at=waived.updated_at + timedelta(seconds=1))
    with pytest.raises(InvalidReviewDecision, match="updated after"):
        decision(
            ReviewOutcome.READY,
            findings=(
                finding().waive(
                    actor_id="owner",
                    rationale="accepted",
                    at=NOW + timedelta(seconds=1),
                ),
            ),
        )
