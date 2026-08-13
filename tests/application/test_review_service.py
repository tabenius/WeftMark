from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from weftmark.application.change_binding import ChangeBinding, GitLineageObservation
from weftmark.application.review_service import ReviewService, ReviewServiceError
from weftmark.application.scope_audit import ScopeAuditService
from weftmark.domain.changeset import ChangeSet
from weftmark.domain.evidence import (
    Evidence,
    EvidenceKind,
    EvidenceProducer,
    EvidenceSubject,
    ProducerKind,
    SubjectKind,
)
from weftmark.domain.policy import EvidencePolicy, EvidenceRequirement
from weftmark.domain.review import FindingSeverity, ReviewFinding, ReviewOutcome
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 14, 0, 20, tzinfo=timezone.utc)
HEAD = "b" * 40
SUBJECT = EvidenceSubject(SubjectKind.CHANGE_SET, "chg-1")
PRODUCER = EvidenceProducer(ProducerKind.CI, "local")


def binding(*, changed: tuple[str, ...] = ("src/app.py",)) -> ChangeBinding:
    change_set = ChangeSet.plan(
        id="chg-1",
        goal="Produce an explainable readiness decision",
        repository_id="repo-1",
        base_sha="a" * 40,
        branch="feature",
        worktree="/work/repo",
        at=NOW,
    ).activate(head_sha=HEAD, at=NOW)
    observation = GitLineageObservation(
        id="chg-1:git:1",
        repository_id="repo-1",
        base_revision="main",
        base_sha="a" * 40,
        head_sha=HEAD,
        branch="feature",
        worktree="/work/repo",
        changed_paths=changed,
        dirty_paths=(),
        observed_at=NOW,
    )
    return ChangeBinding(change_set, "main", (observation,))


def passed(*, id: str = "ev-test", sha: str | None = HEAD) -> Evidence:
    return (
        Evidence.declare(
            id=id,
            kind=EvidenceKind.TEST,
            producer=PRODUCER,
            subject=SUBJECT,
            bound_commit_sha=sha,
            at=NOW,
        )
        .start(at=NOW)
        .pass_(at=NOW)
    )


def policy() -> EvidencePolicy:
    return EvidencePolicy(
        "policy-1",
        SUBJECT,
        (EvidenceRequirement("tests", EvidenceKind.TEST),),
    )


def audit(change_binding: ChangeBinding, *scopes: Scope):
    return ScopeAuditService().audit(
        change_binding,
        declared_scopes=scopes or (Scope.file("src/**"),),
        audited_at=NOW,
    )


def summarize(
    change_binding: ChangeBinding,
    evidence: tuple[Evidence, ...],
    *,
    scope_audit=None,
    additional_findings: tuple[ReviewFinding, ...] = (),
):
    return ReviewService().summarize(
        change_binding,
        scope_audit or audit(change_binding),
        policy(),
        evidence,
        decision_id="decision-1",
        author_id="reviewer-1",
        decided_at=NOW + timedelta(seconds=1),
        additional_findings=additional_findings,
    )


def test_ready_summary_answers_core_reviewer_questions() -> None:
    change_binding = binding()
    summary = summarize(change_binding, (passed(),))

    assert summary.outcome is ReviewOutcome.READY
    assert summary.is_releasable
    assert summary.goal == "Produce an explainable readiness decision"
    assert summary.base_sha == "a" * 40
    assert summary.head_sha == HEAD
    assert summary.changed_paths == ("src/app.py",)
    assert summary.declared_scopes == ("file:src/**",)
    assert summary.lineage_event_count == 1
    assert summary.lineage == change_binding.change_set.lineage
    assert summary.evidence[0].state.value == "passed"
    assert summary.explanations == ("readiness outcome is ready",)


def test_scope_drift_is_blocked_and_can_never_emit_ready() -> None:
    change_binding = binding(changed=("src/app.py", "outside.txt"))
    scope_audit = audit(change_binding, Scope.file("src/**"))

    summary = summarize(change_binding, (passed(),), scope_audit=scope_audit)
    assert summary.outcome is ReviewOutcome.BLOCKED
    assert not summary.is_releasable
    assert summary.decision.unresolved_blockers
    assert any("outside.txt" in reason for reason in summary.explanations)


@pytest.mark.parametrize("sha", ("a" * 40, None))
def test_obsolete_or_unbound_required_evidence_makes_review_stale(
    sha: str | None,
) -> None:
    summary = summarize(binding(), (passed(sha=sha),))

    assert summary.outcome is ReviewOutcome.STALE
    assert summary.evidence[0].is_obsolete
    assert any("unbound or obsolete" in reason for reason in summary.explanations)


def test_missing_required_evidence_is_incomplete() -> None:
    summary = summarize(binding(), ())

    assert summary.outcome is ReviewOutcome.EVIDENCE_INCOMPLETE
    assert not summary.is_releasable
    assert any("required test evidence is missing" in reason for reason in summary.explanations)


@pytest.mark.parametrize("state", ("failed", "unavailable"))
def test_failed_and_unavailable_evidence_remain_explainable(state: str) -> None:
    declared = Evidence.declare(
        id=f"ev-{state}",
        kind=EvidenceKind.TEST,
        producer=PRODUCER,
        subject=SUBJECT,
        bound_commit_sha=HEAD,
        at=NOW,
    )
    evidence = (
        declared.start(at=NOW).fail(detail="failed", at=NOW)
        if state == "failed"
        else declared.unavailable(reason="runner unavailable", at=NOW)
    )

    summary = summarize(binding(), (evidence,))
    assert summary.outcome is ReviewOutcome.EVIDENCE_INCOMPLETE
    assert any(state in reason for reason in summary.explanations)


def test_open_nonblocking_finding_produces_ready_with_follow_up() -> None:
    finding = ReviewFinding(
        id="finding-low",
        severity=FindingSeverity.LOW,
        scope=Scope.file("src/app.py"),
        rationale="rename for clarity later",
        created_at=NOW,
        updated_at=NOW,
    )

    summary = summarize(binding(), (passed(),), additional_findings=(finding,))
    assert summary.outcome is ReviewOutcome.READY_WITH_FOLLOW_UP
    assert summary.is_releasable


def test_resolved_finding_does_not_prevent_ready() -> None:
    finding = ReviewFinding(
        id="finding-resolved",
        severity=FindingSeverity.BLOCKING,
        scope=Scope.contract("auth"),
        rationale="review auth behavior",
        created_at=NOW,
        updated_at=NOW,
    ).resolve(actor_id="reviewer-1", rationale="verified", at=NOW)

    summary = summarize(binding(), (passed(),), additional_findings=(finding,))
    assert summary.outcome is ReviewOutcome.READY


def test_mismatched_audit_and_policy_inputs_fail_closed() -> None:
    current = binding()
    other = binding(changed=("other.py",))
    other_observation = GitLineageObservation(
        id="chg-1:git:2",
        repository_id="repo-1",
        base_revision="main",
        base_sha="a" * 40,
        head_sha=HEAD,
        branch="feature",
        worktree="/work/repo",
        changed_paths=("other.py",),
        dirty_paths=(),
        observed_at=NOW,
    )
    stale_binding = ChangeBinding(
        other.change_set, "main", (other.observations[0], other_observation)
    )
    stale_audit = audit(stale_binding, Scope.file("**"))

    with pytest.raises(ReviewServiceError, match="latest Git observation"):
        summarize(current, (passed(),), scope_audit=stale_audit)

    wrong_policy = EvidencePolicy(
        "wrong",
        EvidenceSubject(SubjectKind.CHANGE_SET, "chg-other"),
        (EvidenceRequirement("tests", EvidenceKind.TEST),),
    )
    with pytest.raises(ReviewServiceError, match="policy"):
        ReviewService().summarize(
            current,
            audit(current),
            wrong_policy,
            (passed(),),
            decision_id="decision-1",
            author_id="reviewer-1",
            decided_at=NOW,
        )
