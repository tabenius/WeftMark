"""Typed, immutable evidence and staleness semantics."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum


class EvidenceError(ValueError):
    """Base class for invalid evidence records and operations."""


class InvalidEvidenceTransition(EvidenceError):
    """Raised when evidence state would move through an invalid path."""


class EvidenceKind(StrEnum):
    TEST = "test"
    CI = "ci"
    REVIEW = "review"
    BENCHMARK = "benchmark"
    DEPLOYMENT = "deployment"
    SECURITY = "security"
    DOCS = "docs"
    ARTIFACT = "artifact"


class EvidenceState(StrEnum):
    DECLARED = "declared"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    SUPERSEDED = "superseded"


class ProducerKind(StrEnum):
    WORKER = "worker"
    CI = "ci"
    HUMAN = "human"
    SYSTEM = "system"


class SubjectKind(StrEnum):
    TASK = "task"
    CHANGE_SET = "change_set"
    COMMIT = "commit"
    REVIEW = "review"
    DEPLOYMENT = "deployment"


class StaleReason(StrEnum):
    COMMIT_CHANGED = "commit_changed"
    ENVIRONMENT_CHANGED = "environment_changed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise EvidenceError(f"{name} must not be empty")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceError(f"{name} must include a timezone")


@dataclass(frozen=True, slots=True)
class EvidenceProducer:
    kind: ProducerKind
    id: str

    def __post_init__(self) -> None:
        _require_text("producer id", self.id)


@dataclass(frozen=True, slots=True)
class EvidenceSubject:
    kind: SubjectKind
    id: str

    def __post_init__(self) -> None:
        _require_text("subject id", self.id)


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    uri: str
    digest: str | None = None

    def __post_init__(self) -> None:
        _require_text("artifact uri", self.uri)
        if self.digest is not None:
            _require_text("artifact digest", self.digest)


@dataclass(frozen=True, slots=True)
class Command:
    argv: tuple[str, ...]
    cwd: str

    def __post_init__(self) -> None:
        if not self.argv or any(not part for part in self.argv):
            raise EvidenceError("command argv must not be empty")
        _require_text("command cwd", self.cwd)


@dataclass(frozen=True, slots=True)
class Environment:
    fingerprint: str
    description: str | None = None

    def __post_init__(self) -> None:
        _require_text("environment fingerprint", self.fingerprint)


_ALLOWED_TRANSITIONS: dict[EvidenceState, frozenset[EvidenceState]] = {
    EvidenceState.DECLARED: frozenset(
        {EvidenceState.RUNNING, EvidenceState.UNAVAILABLE}
    ),
    EvidenceState.RUNNING: frozenset(
        {EvidenceState.PASSED, EvidenceState.FAILED, EvidenceState.UNAVAILABLE}
    ),
    EvidenceState.PASSED: frozenset(
        {EvidenceState.STALE, EvidenceState.SUPERSEDED}
    ),
    EvidenceState.FAILED: frozenset(
        {EvidenceState.STALE, EvidenceState.SUPERSEDED}
    ),
    EvidenceState.UNAVAILABLE: frozenset(
        {EvidenceState.STALE, EvidenceState.SUPERSEDED}
    ),
    EvidenceState.STALE: frozenset({EvidenceState.SUPERSEDED}),
    EvidenceState.SUPERSEDED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    kind: EvidenceKind
    producer: EvidenceProducer
    subject: EvidenceSubject
    bound_commit_sha: str | None = None
    environment: Environment | None = None
    command: Command | None = None
    artifacts: tuple[ArtifactReference, ...] = ()
    state: EvidenceState = EvidenceState.DECLARED
    detail: str | None = None
    stale_reasons: frozenset[StaleReason] = frozenset()
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text("evidence id", self.id)
        if self.bound_commit_sha is not None:
            _require_text("bound commit sha", self.bound_commit_sha)
        _require_aware("created_at", self.created_at)
        _require_aware("updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise EvidenceError("updated_at must not precede created_at")

    @classmethod
    def declare(
        cls,
        *,
        id: str,
        kind: EvidenceKind,
        producer: EvidenceProducer,
        subject: EvidenceSubject,
        bound_commit_sha: str | None = None,
        environment: Environment | None = None,
        command: Command | None = None,
        artifacts: tuple[ArtifactReference, ...] = (),
        at: datetime | None = None,
    ) -> Evidence:
        timestamp = at or _now()
        return cls(
            id=id,
            kind=kind,
            producer=producer,
            subject=subject,
            bound_commit_sha=bound_commit_sha,
            environment=environment,
            command=command,
            artifacts=artifacts,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def start(self, *, at: datetime | None = None) -> Evidence:
        timestamp = self._operation_time(at)
        return self._transition(
            EvidenceState.RUNNING, at=timestamp, started_at=timestamp
        )

    def pass_(self, *, detail: str | None = None, at: datetime | None = None) -> Evidence:
        return self._complete(EvidenceState.PASSED, detail=detail, at=at)

    def fail(self, *, detail: str, at: datetime | None = None) -> Evidence:
        _require_text("failure detail", detail)
        return self._complete(EvidenceState.FAILED, detail=detail, at=at)

    def unavailable(self, *, reason: str, at: datetime | None = None) -> Evidence:
        _require_text("unavailable reason", reason)
        timestamp = self._operation_time(at)
        return self._transition(
            EvidenceState.UNAVAILABLE,
            at=timestamp,
            detail=reason,
            completed_at=timestamp,
        )

    def staleness(
        self,
        *,
        current_commit_sha: str | None,
        current_environment_fingerprint: str | None,
    ) -> frozenset[StaleReason]:
        reasons: set[StaleReason] = set()
        if (
            self.bound_commit_sha is not None
            and current_commit_sha is not None
            and current_commit_sha != self.bound_commit_sha
        ):
            reasons.add(StaleReason.COMMIT_CHANGED)
        if (
            self.environment is not None
            and current_environment_fingerprint is not None
            and current_environment_fingerprint != self.environment.fingerprint
        ):
            reasons.add(StaleReason.ENVIRONMENT_CHANGED)
        return frozenset(reasons)

    def mark_stale(
        self,
        *,
        current_commit_sha: str | None,
        current_environment_fingerprint: str | None,
        at: datetime | None = None,
    ) -> Evidence:
        reasons = self.staleness(
            current_commit_sha=current_commit_sha,
            current_environment_fingerprint=current_environment_fingerprint,
        )
        if not reasons:
            raise EvidenceError("evidence bindings are still current")
        return self._transition(
            EvidenceState.STALE,
            at=self._operation_time(at),
            stale_reasons=reasons,
        )

    def supersede(self, *, at: datetime | None = None) -> Evidence:
        return self._transition(
            EvidenceState.SUPERSEDED, at=self._operation_time(at)
        )

    def _complete(
        self,
        state: EvidenceState,
        *,
        detail: str | None,
        at: datetime | None,
    ) -> Evidence:
        timestamp = self._operation_time(at)
        return self._transition(
            state, at=timestamp, detail=detail, completed_at=timestamp
        )

    def _transition(self, state: EvidenceState, *, at: datetime, **changes: object) -> Evidence:
        if state not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidEvidenceTransition(
                f"cannot transition evidence from {self.state} to {state}"
            )
        return replace(self, state=state, updated_at=at, **changes)

    def _operation_time(self, at: datetime | None) -> datetime:
        timestamp = at or _now()
        _require_aware("operation timestamp", timestamp)
        if timestamp < self.updated_at:
            raise EvidenceError("operation timestamp must not precede updated_at")
        return timestamp

