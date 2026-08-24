"""Read-only, provider-neutral contract for hosted code-forge facts.

The forge boundary deliberately speaks in Change Requests rather than GitHub
pull requests or GitLab merge requests. Local-only WeftMark operation never
requires a forge adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, Protocol, TypeVar, runtime_checkable

from weftmark.application.ports.git import GitDiffEntry, GitObjectId


class ForgeContractError(ValueError):
    """Raised when an adapter returns an invalid forge contract value."""


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip() or "\x00" in value:
        raise ForgeContractError(f"{name} must be non-empty and NUL-free")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ForgeContractError(f"{name} must include a timezone")


class ForgeAvailability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ForgeResult(Generic[T]):
    """One forge observation with explicit absence, unsupported and failure states.

    `missing` means the provider was reachable and the requested forge fact did
    not exist. `unsupported` means the configured provider/instance cannot
    represent the requested capability. `unavailable` means the observation
    could not be made. None of those states is equivalent to a failed test/check.
    """

    availability: ForgeAvailability
    value: T | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.availability is ForgeAvailability.AVAILABLE:
            if self.value is None:
                raise ForgeContractError("available forge result requires a value")
            if self.detail is not None:
                raise ForgeContractError("available forge result cannot carry failure detail")
        else:
            if self.value is not None:
                raise ForgeContractError("non-available forge result cannot carry a value")
            if self.availability in {ForgeAvailability.UNSUPPORTED, ForgeAvailability.UNAVAILABLE}:
                if self.detail is None or not self.detail.strip():
                    raise ForgeContractError(
                        f"{self.availability.value} forge result requires detail"
                    )

    @classmethod
    def available(cls, value: T) -> ForgeResult[T]:
        return cls(ForgeAvailability.AVAILABLE, value=value)

    @classmethod
    def missing(cls, detail: str | None = None) -> ForgeResult[T]:
        return cls(ForgeAvailability.MISSING, detail=detail)

    @classmethod
    def unsupported(cls, detail: str) -> ForgeResult[T]:
        return cls(ForgeAvailability.UNSUPPORTED, detail=detail)

    @classmethod
    def unavailable(cls, detail: str) -> ForgeResult[T]:
        return cls(ForgeAvailability.UNAVAILABLE, detail=detail)


@dataclass(frozen=True, slots=True)
class ForgeCapabilities:
    """Capabilities exposed by the configured forge adapter/instance.

    These are observation capabilities, not authorization grants. A supported
    capability can still return missing or unavailable for one observation.
    """

    change_requests: bool = True
    checks: bool = True
    workflow_runs: bool = True
    reviews: bool = True
    comments: bool = True
    changed_files: bool = True


class ForgeChangeState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class ForgeRunStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    WAITING = "waiting"
    PENDING = "pending"
    REQUESTED = "requested"
    UNKNOWN = "unknown"


class ForgeConclusion(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    NEUTRAL = "neutral"
    ACTION_REQUIRED = "action_required"
    STALE = "stale"
    TIMED_OUT = "timed_out"
    STARTUP_FAILURE = "startup_failure"
    UNKNOWN = "unknown"


class ForgeReviewState(StrEnum):
    PENDING = "pending"
    COMMENTED = "commented"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    DISMISSED = "dismissed"
    UNKNOWN = "unknown"


class ForgeCommentKind(StrEnum):
    GENERAL = "general"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class ForgeRepository:
    provider: str
    id: str
    web_url: str

    def __post_init__(self) -> None:
        _require_text("forge provider", self.provider)
        _require_text("forge repository id", self.id)
        _require_text("forge repository URL", self.web_url)


@dataclass(frozen=True, slots=True)
class ForgeActor:
    id: str
    login: str

    def __post_init__(self) -> None:
        _require_text("forge actor id", self.id)
        _require_text("forge actor login", self.login)


@dataclass(frozen=True, slots=True)
class ForgeChangeRequest:
    external_id: str
    title: str
    state: ForgeChangeState
    source_branch: str
    target_branch: str
    head: GitObjectId
    base: GitObjectId
    web_url: str
    author: ForgeActor
    draft: bool
    updated_at: datetime
    merged_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "external_id",
            "title",
            "source_branch",
            "target_branch",
            "web_url",
        ):
            _require_text(name, getattr(self, name))
        _require_aware("change request updated_at", self.updated_at)
        if self.merged_at is not None:
            _require_aware("change request merged_at", self.merged_at)
        if self.state is ForgeChangeState.MERGED and self.merged_at is None:
            raise ForgeContractError("merged change request requires merged_at")
        if self.state is not ForgeChangeState.MERGED and self.merged_at is not None:
            raise ForgeContractError("non-merged change request cannot carry merged_at")


@dataclass(frozen=True, slots=True)
class ForgeCheck:
    external_id: str
    name: str
    status: ForgeRunStatus
    conclusion: ForgeConclusion | None
    head: GitObjectId
    details_url: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text("check external_id", self.external_id)
        _require_text("check name", self.name)
        if self.details_url is not None:
            _require_text("check details_url", self.details_url)
        if self.started_at is not None:
            _require_aware("check started_at", self.started_at)
        if self.completed_at is not None:
            _require_aware("check completed_at", self.completed_at)
        if self.status is ForgeRunStatus.COMPLETED and self.conclusion is None:
            raise ForgeContractError("completed check requires a conclusion")
        if self.status is not ForgeRunStatus.COMPLETED and self.conclusion is not None:
            raise ForgeContractError("incomplete check cannot carry a conclusion")


@dataclass(frozen=True, slots=True)
class ForgeWorkflowRun:
    external_id: str
    name: str
    event: str
    status: ForgeRunStatus
    conclusion: ForgeConclusion | None
    head: GitObjectId
    web_url: str
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("external_id", "name", "event", "web_url"):
            _require_text(name, getattr(self, name))
        if self.started_at is not None:
            _require_aware("workflow started_at", self.started_at)
        if self.completed_at is not None:
            _require_aware("workflow completed_at", self.completed_at)
        if self.status is ForgeRunStatus.COMPLETED and self.conclusion is None:
            raise ForgeContractError("completed workflow requires a conclusion")
        if self.status is not ForgeRunStatus.COMPLETED and self.conclusion is not None:
            raise ForgeContractError("incomplete workflow cannot carry a conclusion")


@dataclass(frozen=True, slots=True)
class ForgeReview:
    external_id: str
    author: ForgeActor
    state: ForgeReviewState
    body: str
    submitted_at: datetime | None = None
    commit: GitObjectId | None = None

    def __post_init__(self) -> None:
        _require_text("review external_id", self.external_id)
        if self.submitted_at is not None:
            _require_aware("review submitted_at", self.submitted_at)


@dataclass(frozen=True, slots=True)
class ForgeComment:
    external_id: str
    author: ForgeActor
    kind: ForgeCommentKind
    body: str
    created_at: datetime
    updated_at: datetime
    web_url: str
    path: str | None = None
    line: int | None = None

    def __post_init__(self) -> None:
        _require_text("comment external_id", self.external_id)
        _require_text("comment URL", self.web_url)
        _require_aware("comment created_at", self.created_at)
        _require_aware("comment updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise ForgeContractError("comment updated_at cannot precede created_at")
        if self.path is not None:
            GitDiffEntry(path=self.path, kind=_dummy_change_kind())
        if self.line is not None and self.line < 1:
            raise ForgeContractError("comment line must be positive")


@dataclass(frozen=True, slots=True)
class ForgeChangedFile:
    entry: GitDiffEntry
    additions: int | None
    deletions: int | None

    def __post_init__(self) -> None:
        if any(
            count is not None and type(count) is not int
            for count in (self.additions, self.deletions)
        ):
            raise ForgeContractError(
                "changed-file counts must be integers or unavailable"
            )
        if any(
            count is not None and count < 0
            for count in (self.additions, self.deletions)
        ):
            raise ForgeContractError("changed-file counts cannot be negative")


def _dummy_change_kind():
    from weftmark.application.ports.git import GitChangeKind

    return GitChangeKind.MODIFIED


@runtime_checkable
class ForgePort(Protocol):
    """Optional read-side facts from one configured code forge repository."""

    def repository(self) -> ForgeRepository:
        """Return provider/repository identity without requiring local Git."""

    def capabilities(self) -> ForgeCapabilities:
        """Describe which ForgePort observation families this adapter supports."""
        return ForgeCapabilities()

    def change_request(self, external_id: str) -> ForgeResult[ForgeChangeRequest]:
        """Read a pull/merge/change request by provider-visible identifier."""

    def checks(self, head: GitObjectId) -> ForgeResult[tuple[ForgeCheck, ...]]:
        """Read commit checks; missing means the provider reported no checks."""

    def workflow_runs(
        self, head: GitObjectId
    ) -> ForgeResult[tuple[ForgeWorkflowRun, ...]]:
        """Read CI workflow runs; missing means no runs exist for this head."""

    def reviews(self, external_id: str) -> ForgeResult[tuple[ForgeReview, ...]]:
        """Read review submissions for one change request."""

    def comments(self, external_id: str) -> ForgeResult[tuple[ForgeComment, ...]]:
        """Read general and code-review comments for one change request."""

    def changed_files(
        self, external_id: str
    ) -> ForgeResult[tuple[ForgeChangedFile, ...]]:
        """Read provider-reported changed files for one change request."""
