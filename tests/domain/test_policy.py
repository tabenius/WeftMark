from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from weftmark.domain.evidence import (
    Environment,
    Evidence,
    EvidenceKind,
    EvidenceProducer,
    EvidenceSubject,
    ProducerKind,
    SubjectKind,
)
from weftmark.domain.policy import (
    EvidencePolicy,
    EvidencePolicyError,
    EvidenceProblem,
    EvidenceRequirement,
)
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 13, 20, 0, tzinfo=timezone.utc)
SUBJECT = EvidenceSubject(SubjectKind.CHANGE_SET, "chg-1")
PRODUCER = EvidenceProducer(ProducerKind.CI, "github-actions")


def record(kind: EvidenceKind, id: str) -> Evidence:
    return Evidence.declare(
        id=id,
        kind=kind,
        producer=PRODUCER,
        subject=SUBJECT,
        bound_commit_sha="a" * 40,
        environment=Environment("linux-python-3.12"),
        at=NOW,
    )


def policy(*requirements: EvidenceRequirement) -> EvidencePolicy:
    return EvidencePolicy("policy-1", SUBJECT, requirements)


def test_policy_can_express_every_planned_proof_kind() -> None:
    kinds = (
        EvidenceKind.CI,
        EvidenceKind.SECURITY,
        EvidenceKind.TEST,
        EvidenceKind.DOCS,
        EvidenceKind.BENCHMARK,
        EvidenceKind.DEPLOYMENT,
    )
    configured = policy(
        *(EvidenceRequirement(f"require-{kind.value}", kind) for kind in kinds)
    )

    assert tuple(rule.kind for rule in configured.requirements) == kinds


def test_required_passes_make_policy_satisfied() -> None:
    configured = policy(
        EvidenceRequirement("tests", EvidenceKind.TEST),
        EvidenceRequirement("ci", EvidenceKind.CI),
    )
    evidence = (
        record(EvidenceKind.TEST, "ev-test").start().pass_(),
        record(EvidenceKind.CI, "ev-ci").start().pass_(),
    )

    result = configured.evaluate(
        evidence,
        current_commit_sha="a" * 40,
        current_environment_fingerprint="linux-python-3.12",
    )
    assert result.is_satisfied
    assert result.satisfied_requirement_ids == ("tests", "ci")
    assert result.issues == ()


@pytest.mark.parametrize(
    ("state", "problem"),
    [
        ("missing", EvidenceProblem.MISSING),
        ("failed", EvidenceProblem.FAILED),
        ("unavailable", EvidenceProblem.UNAVAILABLE),
        ("stale", EvidenceProblem.STALE),
    ],
)
def test_required_problems_remain_distinct_and_explainable(
    state: str, problem: EvidenceProblem
) -> None:
    configured = policy(EvidenceRequirement("ci", EvidenceKind.CI))
    if state == "missing":
        evidence: tuple[Evidence, ...] = ()
    else:
        declared = record(EvidenceKind.CI, "ev-ci")
        evidence = {
            "failed": (declared.start().fail(detail="tests failed"),),
            "unavailable": (declared.unavailable(reason="runner did not start"),),
            "stale": (declared.start().pass_().mark_stale(
                current_commit_sha="b" * 40,
                current_environment_fingerprint="linux-python-3.12",
            ),),
        }[state]

    result = configured.evaluate(evidence)
    assert not result.is_satisfied
    assert result.issues[0].problem is problem
    assert result.issues[0].explanation == f"required ci evidence is {problem.value}"


def test_unavailable_required_evidence_is_never_treated_as_passed() -> None:
    configured = policy(EvidenceRequirement("ci", EvidenceKind.CI))
    unavailable = record(EvidenceKind.CI, "ev-ci").unavailable(
        reason="billing-disabled runner"
    )

    result = configured.evaluate((unavailable,))
    assert result.satisfied_requirement_ids == ()
    assert result.required_issues[0].problem is EvidenceProblem.UNAVAILABLE


def test_dynamic_binding_change_makes_previous_pass_stale() -> None:
    configured = policy(EvidenceRequirement("tests", EvidenceKind.TEST))
    passed = record(EvidenceKind.TEST, "ev-test").start().pass_()

    result = configured.evaluate(
        (passed,),
        current_commit_sha="b" * 40,
        current_environment_fingerprint="linux-python-3.12",
    )
    assert result.issues[0].problem is EvidenceProblem.STALE


def test_newer_failed_retry_cannot_be_masked_by_older_pass() -> None:
    configured = policy(EvidenceRequirement("tests", EvidenceKind.TEST))
    passed = (
        record(EvidenceKind.TEST, "ev-pass")
        .start(at=NOW + timedelta(seconds=1))
        .pass_(at=NOW + timedelta(seconds=2))
    )
    failed_retry = (
        Evidence.declare(
            id="ev-retry",
            kind=EvidenceKind.TEST,
            producer=PRODUCER,
            subject=SUBJECT,
            at=NOW + timedelta(seconds=3),
        )
        .start(at=NOW + timedelta(seconds=4))
        .fail(detail="regression", at=NOW + timedelta(seconds=5))
    )

    result = configured.evaluate((passed, failed_retry))
    assert not result.is_satisfied
    assert result.issues[0].problem is EvidenceProblem.FAILED
    assert result.issues[0].evidence_ids == ("ev-pass", "ev-retry")


def test_newer_pass_replaces_an_older_failure() -> None:
    configured = policy(EvidenceRequirement("tests", EvidenceKind.TEST))
    failed = (
        record(EvidenceKind.TEST, "ev-fail")
        .start(at=NOW + timedelta(seconds=1))
        .fail(detail="initial failure", at=NOW + timedelta(seconds=2))
    )
    passed_retry = (
        Evidence.declare(
            id="ev-retry",
            kind=EvidenceKind.TEST,
            producer=PRODUCER,
            subject=SUBJECT,
            at=NOW + timedelta(seconds=3),
        )
        .start(at=NOW + timedelta(seconds=4))
        .pass_(at=NOW + timedelta(seconds=5))
    )

    result = configured.evaluate((failed, passed_retry))
    assert result.is_satisfied


def test_tied_contradictory_observations_fail_closed() -> None:
    configured = policy(EvidenceRequirement("tests", EvidenceKind.TEST))
    completed_at = NOW + timedelta(seconds=2)
    passed = (
        record(EvidenceKind.TEST, "ev-pass")
        .start(at=NOW + timedelta(seconds=1))
        .pass_(at=completed_at)
    )
    failed = (
        record(EvidenceKind.TEST, "ev-fail")
        .start(at=NOW + timedelta(seconds=1))
        .fail(detail="conflicting result", at=completed_at)
    )

    result = configured.evaluate((passed, failed))
    assert not result.is_satisfied
    assert result.issues[0].problem is EvidenceProblem.FAILED


def test_optional_problem_is_visible_without_blocking_required_satisfaction() -> None:
    configured = policy(
        EvidenceRequirement("tests", EvidenceKind.TEST),
        EvidenceRequirement("benchmark", EvidenceKind.BENCHMARK, required=False),
    )
    passed = record(EvidenceKind.TEST, "ev-test").start().pass_()

    result = configured.evaluate((passed,))
    assert result.is_satisfied
    assert result.issues[0].problem is EvidenceProblem.MISSING
    assert not result.issues[0].required


def test_scoped_requirements_apply_only_to_overlapping_declared_scope() -> None:
    configured = policy(
        EvidenceRequirement(
            "security-review",
            EvidenceKind.SECURITY,
            scopes=(Scope.contract("tenant-auth"),),
        ),
        EvidenceRequirement("tests", EvidenceKind.TEST),
    )

    unrelated = configured.evaluate((), declared_scopes=(Scope.contract("billing"),))
    related = configured.evaluate(
        (), declared_scopes=(Scope.contract("Tenant Auth"),)
    )
    assert unrelated.applicable_requirement_ids == ("tests",)
    assert related.applicable_requirement_ids == ("security-review", "tests")


def test_evidence_for_another_subject_cannot_satisfy_policy() -> None:
    configured = policy(EvidenceRequirement("tests", EvidenceKind.TEST))
    other = Evidence.declare(
        id="ev-other",
        kind=EvidenceKind.TEST,
        producer=PRODUCER,
        subject=EvidenceSubject(SubjectKind.CHANGE_SET, "chg-other"),
        at=NOW,
    ).start(at=NOW + timedelta(seconds=1)).pass_(at=NOW + timedelta(seconds=2))

    result = configured.evaluate((other,))
    assert result.issues[0].problem is EvidenceProblem.MISSING


def test_ambiguous_policy_configuration_fails_closed() -> None:
    rule = EvidenceRequirement("tests", EvidenceKind.TEST)

    with pytest.raises(EvidencePolicyError, match="at least one"):
        policy()
    with pytest.raises(EvidencePolicyError, match="duplicate requirement"):
        policy(rule, rule)
    with pytest.raises(EvidencePolicyError, match="duplicate scope"):
        EvidenceRequirement(
            "security",
            EvidenceKind.SECURITY,
            scopes=(Scope.contract("auth"), Scope.contract("Auth")),
        )
