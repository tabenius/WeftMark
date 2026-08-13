"""Explainable policy evaluation over typed evidence records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from weftmark.domain.evidence import (
    Evidence,
    EvidenceKind,
    EvidenceState,
    EvidenceSubject,
)
from weftmark.domain.lock import scopes_overlap
from weftmark.domain.scope import Scope


class EvidencePolicyError(ValueError):
    """Raised when an evidence policy is ambiguous or malformed."""


class EvidenceProblem(StrEnum):
    MISSING = "missing"
    STALE = "stale"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise EvidencePolicyError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    id: str
    kind: EvidenceKind
    required: bool = True
    scopes: tuple[Scope, ...] = ()
    description: str | None = None

    def __post_init__(self) -> None:
        _require_text("requirement id", self.id)
        if self.description is not None:
            _require_text("requirement description", self.description)
        if len(set(scope.identity for scope in self.scopes)) != len(self.scopes):
            raise EvidencePolicyError("requirement contains duplicate scope identities")

    def applies_to(self, declared_scopes: tuple[Scope, ...]) -> bool:
        if not self.scopes:
            return True
        return any(
            scopes_overlap(required_scope, declared_scope)
            for required_scope in self.scopes
            for declared_scope in declared_scopes
        )


@dataclass(frozen=True, slots=True)
class EvidenceIssue:
    requirement_id: str
    kind: EvidenceKind
    required: bool
    problem: EvidenceProblem
    evidence_ids: tuple[str, ...]
    observed_states: tuple[EvidenceState, ...]

    @property
    def explanation(self) -> str:
        importance = "required" if self.required else "optional"
        return f"{importance} {self.kind.value} evidence is {self.problem.value}"


@dataclass(frozen=True, slots=True)
class EvidencePolicyResult:
    policy_id: str
    applicable_requirement_ids: tuple[str, ...]
    satisfied_requirement_ids: tuple[str, ...]
    issues: tuple[EvidenceIssue, ...]

    @property
    def is_satisfied(self) -> bool:
        return not any(issue.required for issue in self.issues)

    @property
    def required_issues(self) -> tuple[EvidenceIssue, ...]:
        return tuple(issue for issue in self.issues if issue.required)


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    id: str
    subject: EvidenceSubject
    requirements: tuple[EvidenceRequirement, ...]

    def __post_init__(self) -> None:
        _require_text("policy id", self.id)
        requirement_ids = tuple(requirement.id for requirement in self.requirements)
        if not requirement_ids:
            raise EvidencePolicyError("policy must contain at least one requirement")
        if len(set(requirement_ids)) != len(requirement_ids):
            raise EvidencePolicyError("policy contains duplicate requirement ids")

    def evaluate(
        self,
        evidence: Iterable[Evidence],
        *,
        declared_scopes: tuple[Scope, ...] = (),
        current_commit_sha: str | None = None,
        current_environment_fingerprint: str | None = None,
    ) -> EvidencePolicyResult:
        records = tuple(record for record in evidence if record.subject == self.subject)
        applicable = tuple(
            requirement
            for requirement in self.requirements
            if requirement.applies_to(declared_scopes)
        )
        satisfied: list[str] = []
        issues: list[EvidenceIssue] = []

        for requirement in applicable:
            matching = tuple(
                record
                for record in records
                if record.kind is requirement.kind
                and record.state is not EvidenceState.SUPERSEDED
            )
            latest = _latest_records(matching)
            if latest and all(
                _is_current_pass(
                    record,
                    current_commit_sha=current_commit_sha,
                    current_environment_fingerprint=current_environment_fingerprint,
                )
                for record in latest
            ):
                satisfied.append(requirement.id)
                continue

            problem = _classify_problem(
                latest,
                current_commit_sha=current_commit_sha,
                current_environment_fingerprint=current_environment_fingerprint,
            )
            issues.append(
                EvidenceIssue(
                    requirement_id=requirement.id,
                    kind=requirement.kind,
                    required=requirement.required,
                    problem=problem,
                    evidence_ids=tuple(record.id for record in matching),
                    observed_states=tuple(record.state for record in matching),
                )
            )

        return EvidencePolicyResult(
            policy_id=self.id,
            applicable_requirement_ids=tuple(rule.id for rule in applicable),
            satisfied_requirement_ids=tuple(satisfied),
            issues=tuple(issues),
        )


def _latest_records(evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    """Keep the newest observation, preserving ties for fail-closed evaluation."""

    if not evidence:
        return ()
    latest_at = max(record.updated_at for record in evidence)
    return tuple(record for record in evidence if record.updated_at == latest_at)


def _is_current_pass(
    evidence: Evidence,
    *,
    current_commit_sha: str | None,
    current_environment_fingerprint: str | None,
) -> bool:
    return evidence.state is EvidenceState.PASSED and not evidence.staleness(
        current_commit_sha=current_commit_sha,
        current_environment_fingerprint=current_environment_fingerprint,
    )


def _classify_problem(
    evidence: tuple[Evidence, ...],
    *,
    current_commit_sha: str | None,
    current_environment_fingerprint: str | None,
) -> EvidenceProblem:
    if any(
        record.state is EvidenceState.STALE
        or (
            record.state is EvidenceState.PASSED
            and record.staleness(
                current_commit_sha=current_commit_sha,
                current_environment_fingerprint=current_environment_fingerprint,
            )
        )
        for record in evidence
    ):
        return EvidenceProblem.STALE
    if any(record.state is EvidenceState.FAILED for record in evidence):
        return EvidenceProblem.FAILED
    if any(record.state is EvidenceState.UNAVAILABLE for record in evidence):
        return EvidenceProblem.UNAVAILABLE
    return EvidenceProblem.MISSING
