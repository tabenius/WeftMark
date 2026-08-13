"""Compose lineage, scope, evidence, and findings into reviewer readiness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from weftmark.application.change_binding import ChangeBinding
from weftmark.application.scope_audit import ScopeAuditResult
from weftmark.domain.changeset import LineageEvent
from weftmark.domain.evidence import (
    Evidence,
    EvidenceKind,
    EvidenceState,
    SubjectKind,
)
from weftmark.domain.policy import (
    EvidencePolicy,
    EvidencePolicyResult,
    EvidenceProblem,
)
from weftmark.domain.review import (
    FindingStatus,
    ReviewDecision,
    ReviewFinding,
    ReviewOutcome,
)


class ReviewServiceError(ValueError):
    """Raised when inputs do not describe one consistent review snapshot."""


@dataclass(frozen=True, slots=True)
class EvidenceSummary:
    id: str
    kind: EvidenceKind
    state: EvidenceState
    bound_commit_sha: str | None
    is_obsolete: bool


@dataclass(frozen=True, slots=True)
class ReviewerSummary:
    decision: ReviewDecision
    goal: str
    repository_id: str
    base_sha: str
    head_sha: str
    branch: str
    worktree: str
    declared_scopes: tuple[str, ...]
    changed_paths: tuple[str, ...]
    dirty_paths: tuple[str, ...]
    semantic_changes: tuple[str, ...]
    lineage: tuple[LineageEvent, ...]
    evidence: tuple[EvidenceSummary, ...]
    policy: EvidencePolicyResult
    explanations: tuple[str, ...]

    @property
    def outcome(self) -> ReviewOutcome:
        return self.decision.outcome

    @property
    def is_releasable(self) -> bool:
        return self.decision.is_releasable

    @property
    def lineage_event_count(self) -> int:
        return len(self.lineage)


class ReviewService:
    def summarize(
        self,
        binding: ChangeBinding,
        scope_audit: ScopeAuditResult,
        policy: EvidencePolicy,
        evidence: Iterable[Evidence],
        *,
        decision_id: str,
        author_id: str,
        decided_at: datetime,
        additional_findings: tuple[ReviewFinding, ...] = (),
        current_environment_fingerprint: str | None = None,
    ) -> ReviewerSummary:
        if decided_at.tzinfo is None or decided_at.utcoffset() is None:
            raise ReviewServiceError("decided_at must include a timezone")
        if scope_audit.observation_id != binding.latest.id:
            raise ReviewServiceError("scope audit does not describe latest Git observation")
        expected_subject = policy.subject
        if (
            expected_subject.kind is not SubjectKind.CHANGE_SET
            or expected_subject.id != binding.change_set.id
        ):
            raise ReviewServiceError("evidence policy does not describe Change Set")

        records = tuple(
            record
            for record in evidence
            if record.subject == expected_subject
            and record.state is not EvidenceState.SUPERSEDED
        )
        if len({record.id for record in records}) != len(records):
            raise ReviewServiceError("evidence snapshot contains duplicate ids")
        findings = (*scope_audit.findings, *additional_findings)
        finding_ids = tuple(finding.id for finding in findings)
        if len(set(finding_ids)) != len(finding_ids):
            raise ReviewServiceError("review inputs contain duplicate finding ids")
        if any(finding.updated_at > decided_at for finding in findings):
            raise ReviewServiceError("finding was updated after decision time")

        evaluation = policy.evaluate(
            records,
            declared_scopes=scope_audit.declared_scopes,
            current_commit_sha=binding.latest.head_sha,
            current_environment_fingerprint=current_environment_fingerprint,
        )
        obsolete_ids = binding.obsolete_evidence_ids(records)
        blocking_obsolete = _blocking_obsolete_ids(
            policy,
            scope_audit,
            records,
            binding.latest.head_sha,
        )
        outcome = _outcome(
            findings,
            evaluation,
            blocking_obsolete=blocking_obsolete,
        )
        explanations = _explanations(
            outcome,
            findings,
            evaluation,
            blocking_obsolete=blocking_obsolete,
        )
        decision = ReviewDecision(
            id=decision_id,
            change_set_id=binding.change_set.id,
            author_id=author_id,
            outcome=outcome,
            head_sha=binding.latest.head_sha,
            evidence_ids=tuple(record.id for record in records),
            findings=findings,
            rationale="; ".join(explanations),
            created_at=decided_at,
        )
        return ReviewerSummary(
            decision=decision,
            goal=binding.change_set.goal,
            repository_id=binding.change_set.repository_id,
            base_sha=binding.latest.base_sha,
            head_sha=binding.latest.head_sha,
            branch=binding.latest.branch,
            worktree=binding.latest.worktree,
            declared_scopes=tuple(
                scope.canonical for scope in scope_audit.declared_scopes
            ),
            changed_paths=binding.latest.changed_paths,
            dirty_paths=binding.latest.dirty_paths,
            semantic_changes=tuple(
                scope.canonical for scope in scope_audit.semantic_changes
            ),
            lineage=binding.change_set.lineage,
            evidence=tuple(
                EvidenceSummary(
                    id=record.id,
                    kind=record.kind,
                    state=record.state,
                    bound_commit_sha=record.bound_commit_sha,
                    is_obsolete=record.id in obsolete_ids,
                )
                for record in records
            ),
            policy=evaluation,
            explanations=explanations,
        )


def _blocking_obsolete_ids(
    policy: EvidencePolicy,
    scope_audit: ScopeAuditResult,
    evidence: tuple[Evidence, ...],
    head_sha: str,
) -> tuple[str, ...]:
    ids: set[str] = set()
    for requirement in policy.requirements:
        if not requirement.required or not requirement.applies_to(
            scope_audit.declared_scopes
        ):
            continue
        matching = tuple(record for record in evidence if record.kind is requirement.kind)
        if not matching:
            continue
        latest_at = max(record.updated_at for record in matching)
        ids.update(
            record.id
            for record in matching
            if record.updated_at == latest_at and record.bound_commit_sha != head_sha
        )
    return tuple(sorted(ids))


def _outcome(
    findings: tuple[ReviewFinding, ...],
    policy: EvidencePolicyResult,
    *,
    blocking_obsolete: tuple[str, ...],
) -> ReviewOutcome:
    if any(finding.is_unresolved_blocker for finding in findings):
        return ReviewOutcome.BLOCKED
    if blocking_obsolete or any(
        issue.problem is EvidenceProblem.STALE for issue in policy.required_issues
    ):
        return ReviewOutcome.STALE
    if not policy.is_satisfied:
        return ReviewOutcome.EVIDENCE_INCOMPLETE
    if any(finding.status is FindingStatus.OPEN for finding in findings):
        return ReviewOutcome.READY_WITH_FOLLOW_UP
    return ReviewOutcome.READY


def _explanations(
    outcome: ReviewOutcome,
    findings: tuple[ReviewFinding, ...],
    policy: EvidencePolicyResult,
    *,
    blocking_obsolete: tuple[str, ...],
) -> tuple[str, ...]:
    explanations: list[str] = [f"readiness outcome is {outcome.value}"]
    explanations.extend(
        f"finding {finding.id}: {finding.rationale}"
        for finding in findings
        if finding.status is FindingStatus.OPEN
    )
    explanations.extend(
        f"requirement {issue.requirement_id}: {issue.explanation}"
        for issue in policy.issues
    )
    if blocking_obsolete:
        explanations.append(
            "required evidence is unbound or obsolete: "
            + ", ".join(blocking_obsolete)
        )
    return tuple(explanations)
