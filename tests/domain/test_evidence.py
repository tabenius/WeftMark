from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from weftmark.domain.evidence import (
    ArtifactReference,
    Command,
    Environment,
    Evidence,
    EvidenceError,
    EvidenceKind,
    EvidenceProducer,
    EvidenceState,
    EvidenceSubject,
    InvalidEvidenceTransition,
    ProducerKind,
    StaleReason,
    SubjectKind,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)


def declared(*, kind: EvidenceKind = EvidenceKind.TEST) -> Evidence:
    return Evidence.declare(
        id="ev_01",
        kind=kind,
        producer=EvidenceProducer(ProducerKind.WORKER, "codex-session-42"),
        subject=EvidenceSubject(SubjectKind.CHANGE_SET, "chg_01"),
        bound_commit_sha="a" * 40,
        environment=Environment("linux-python-3.13"),
        command=Command(("python", "-m", "pytest"), "/src/example"),
        artifacts=(ArtifactReference("file:///tmp/pytest.log", "sha256:123"),),
        at=NOW,
    )


@pytest.mark.parametrize("kind", list(SubjectKind))
def test_evidence_can_bind_to_every_subject_kind(kind: SubjectKind) -> None:
    evidence = Evidence.declare(
        id=f"ev-{kind}",
        kind=EvidenceKind.REVIEW,
        producer=EvidenceProducer(ProducerKind.HUMAN, "reviewer-1"),
        subject=EvidenceSubject(kind, "subject-1"),
        at=NOW,
    )
    assert evidence.subject.kind is kind


def test_successful_execution_has_explicit_running_and_passed_states() -> None:
    running = declared().start(at=NOW + timedelta(seconds=1))
    passed = running.pass_(at=NOW + timedelta(seconds=2))

    assert running.state is EvidenceState.RUNNING
    assert passed.state is EvidenceState.PASSED
    assert passed.started_at == NOW + timedelta(seconds=1)
    assert passed.completed_at == NOW + timedelta(seconds=2)


def test_missing_ci_is_unavailable_not_failed() -> None:
    ci = declared(kind=EvidenceKind.CI)
    unavailable = ci.unavailable(reason="hosted runner did not start")

    assert unavailable.state is EvidenceState.UNAVAILABLE
    assert unavailable.detail == "hosted runner did not start"
    with pytest.raises(InvalidEvidenceTransition):
        ci.fail(detail="runner did not start")


def test_executed_failure_is_distinct_from_unavailability() -> None:
    failed = declared().start().fail(detail="2 assertions failed")

    assert failed.state is EvidenceState.FAILED
    assert failed.state is not EvidenceState.UNAVAILABLE


def test_commit_and_environment_changes_compute_staleness() -> None:
    passed = declared().start().pass_()

    reasons = passed.staleness(
        current_commit_sha="b" * 40,
        current_environment_fingerprint="macos-python-3.13",
    )
    assert reasons == {
        StaleReason.COMMIT_CHANGED,
        StaleReason.ENVIRONMENT_CHANGED,
    }

    stale = passed.mark_stale(
        current_commit_sha="b" * 40,
        current_environment_fingerprint="macos-python-3.13",
    )
    assert stale.state is EvidenceState.STALE
    assert stale.stale_reasons == reasons


def test_current_evidence_cannot_be_marked_stale() -> None:
    passed = declared().start().pass_()

    with pytest.raises(EvidenceError, match="still current"):
        passed.mark_stale(
            current_commit_sha="a" * 40,
            current_environment_fingerprint="linux-python-3.13",
        )


def test_binding_and_identity_are_immutable() -> None:
    evidence = declared()

    with pytest.raises(FrozenInstanceError):
        evidence.bound_commit_sha = "b" * 40  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evidence.subject.id = "different"  # type: ignore[misc]


def test_terminal_evidence_cannot_be_reused_as_a_new_run() -> None:
    passed = declared().start().pass_()

    with pytest.raises(InvalidEvidenceTransition):
        passed.start()
    assert passed.supersede().state is EvidenceState.SUPERSEDED


def test_operation_timestamps_are_monotonic_and_timezone_aware() -> None:
    evidence = declared()

    with pytest.raises(EvidenceError, match="must not precede"):
        evidence.start(at=NOW - timedelta(seconds=1))
    with pytest.raises(EvidenceError, match="timezone"):
        evidence.start(at=NOW.replace(tzinfo=None))
