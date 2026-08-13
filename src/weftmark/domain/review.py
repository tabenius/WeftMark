"""Persistent review findings, waivers, and readiness decisions."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum

from weftmark.domain.scope import Scope


class ReviewError(ValueError):
    """Base class for invalid review records and operations."""


class InvalidFindingOperation(ReviewError):
    """Raised when a finding cannot move through the requested transition."""


class InvalidReviewDecision(ReviewError):
    """Raised when a decision contradicts its findings or evidence snapshot."""


class FindingSeverity(StrEnum):
    BLOCKING = "blocking"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    WAIVED = "waived"


class FindingEventKind(StrEnum):
    RESOLVED = "resolved"
    WAIVED = "waived"


class ReviewOutcome(StrEnum):
    READY = "ready"
    READY_WITH_FOLLOW_UP = "ready_with_follow_up"
    BLOCKED = "blocked"
    STALE = "stale"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"


_RELEASABLE_OUTCOMES = frozenset(
    {ReviewOutcome.READY, ReviewOutcome.READY_WITH_FOLLOW_UP}
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ReviewError(f"{name} must not be empty")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReviewError(f"{name} must include a timezone")


@dataclass(frozen=True, slots=True)
class FindingEvent:
    kind: FindingEventKind
    actor_id: str
    rationale: str
    occurred_at: datetime
    finding_id: str

    def __post_init__(self) -> None:
        _require_text("event actor", self.actor_id)
        _require_text("event rationale", self.rationale)
        _require_text("event finding id", self.finding_id)
        _require_aware("event occurred_at", self.occurred_at)


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    id: str
    severity: FindingSeverity
    scope: Scope
    rationale: str
    status: FindingStatus = FindingStatus.OPEN
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    events: tuple[FindingEvent, ...] = ()

    def __post_init__(self) -> None:
        _require_text("finding id", self.id)
        _require_text("finding rationale", self.rationale)
        _require_aware("finding created_at", self.created_at)
        _require_aware("finding updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise ReviewError("finding updated_at must not precede created_at")
        if self.status is FindingStatus.OPEN and self.events:
            raise ReviewError("open findings must not contain terminal events")
        if self.status is not FindingStatus.OPEN:
            if len(self.events) != 1 or self.events[-1].kind.value != self.status.value:
                raise ReviewError("finding status must match its latest event")
            if self.events[-1].finding_id != self.id:
                raise ReviewError("finding event must identify its finding")
            if self.events[-1].occurred_at != self.updated_at:
                raise ReviewError("finding event time must match updated_at")

    @property
    def is_unresolved_blocker(self) -> bool:
        return (
            self.severity is FindingSeverity.BLOCKING
            and self.status is FindingStatus.OPEN
        )

    def resolve(
        self, *, actor_id: str, rationale: str, at: datetime | None = None
    ) -> ReviewFinding:
        return self._close(
            FindingEventKind.RESOLVED,
            actor_id=actor_id,
            rationale=rationale,
            at=at,
        )

    def waive(
        self, *, actor_id: str, rationale: str, at: datetime | None = None
    ) -> ReviewFinding:
        return self._close(
            FindingEventKind.WAIVED,
            actor_id=actor_id,
            rationale=rationale,
            at=at,
        )

    def _close(
        self,
        kind: FindingEventKind,
        *,
        actor_id: str,
        rationale: str,
        at: datetime | None,
    ) -> ReviewFinding:
        if self.status is not FindingStatus.OPEN:
            raise InvalidFindingOperation(f"finding is already {self.status}")
        timestamp = at or _now()
        _require_aware("finding operation time", timestamp)
        if timestamp < self.updated_at:
            raise InvalidFindingOperation(
                "finding operation time precedes the previous event"
            )
        event = FindingEvent(
            kind=kind,
            actor_id=actor_id,
            rationale=rationale,
            occurred_at=timestamp,
            finding_id=self.id,
        )
        return replace(
            self,
            status=FindingStatus(kind.value),
            updated_at=timestamp,
            events=(*self.events, event),
        )


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """An immutable decision bound to one Change Set head and evidence snapshot."""

    id: str
    change_set_id: str
    author_id: str
    outcome: ReviewOutcome
    head_sha: str
    evidence_ids: tuple[str, ...]
    findings: tuple[ReviewFinding, ...]
    rationale: str
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        for name in ("id", "change_set_id", "author_id", "head_sha", "rationale"):
            _require_text(name, getattr(self, name))
        _require_aware("decision created_at", self.created_at)
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise InvalidReviewDecision("evidence snapshot contains duplicate ids")
        if any(not evidence_id.strip() for evidence_id in self.evidence_ids):
            raise InvalidReviewDecision("evidence ids must not be empty")
        finding_ids = tuple(finding.id for finding in self.findings)
        if len(set(finding_ids)) != len(finding_ids):
            raise InvalidReviewDecision("review contains duplicate finding ids")

        blockers = self.unresolved_blockers
        if self.outcome in _RELEASABLE_OUTCOMES and blockers:
            raise InvalidReviewDecision(
                "releasable decision cannot contain unresolved blocking findings"
            )
        if self.outcome is ReviewOutcome.BLOCKED and not blockers:
            raise InvalidReviewDecision(
                "blocked decision requires an unresolved blocking finding"
            )
        if self.outcome is ReviewOutcome.READY and self.open_findings:
            raise InvalidReviewDecision(
                "ready decision cannot contain open findings; use ready-with-follow-up"
            )
        if self.outcome is ReviewOutcome.READY_WITH_FOLLOW_UP and not self.open_findings:
            raise InvalidReviewDecision(
                "ready-with-follow-up requires at least one open non-blocking finding"
            )
        if any(finding.updated_at > self.created_at for finding in self.findings):
            raise InvalidReviewDecision(
                "review cannot snapshot a finding updated after the decision"
            )

    @property
    def unresolved_blockers(self) -> tuple[ReviewFinding, ...]:
        return tuple(finding for finding in self.findings if finding.is_unresolved_blocker)

    @property
    def open_findings(self) -> tuple[ReviewFinding, ...]:
        return tuple(
            finding for finding in self.findings if finding.status is FindingStatus.OPEN
        )

    @property
    def is_releasable(self) -> bool:
        return self.outcome in _RELEASABLE_OUTCOMES and not self.unresolved_blockers
