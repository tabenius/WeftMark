"""Change Set identity, lifecycle, and Git-lineage invariants."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum


class ChangeSetError(ValueError):
    """Base class for invalid Change Set operations."""


class InvalidTransition(ChangeSetError):
    """Raised when a lifecycle transition is not allowed."""


class InvalidLineageChange(ChangeSetError):
    """Raised when repository lineage would change without an event."""


class ChangeSetState(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    REVIEW = "review"
    MERGED = "merged"
    CLOSED = "closed"
    ABANDONED = "abandoned"


class LineageEventKind(StrEnum):
    ACTIVATED = "activated"
    HEAD_ADVANCED = "head_advanced"
    BRANCH_MOVED = "branch_moved"
    REBASED = "rebased"


@dataclass(frozen=True, slots=True)
class LineageEvent:
    kind: LineageEventKind
    occurred_at: datetime
    previous_base_sha: str
    base_sha: str
    previous_head_sha: str
    head_sha: str
    previous_branch: str
    branch: str


@dataclass(frozen=True, slots=True)
class ScopeAmendment:
    """An explicit, append-only widening of declared scope; never a narrowing."""

    occurred_at: datetime
    added_scopes: tuple[str, ...]
    reason: str


_ALLOWED_TRANSITIONS: dict[ChangeSetState, frozenset[ChangeSetState]] = {
    ChangeSetState.PLANNED: frozenset({ChangeSetState.ABANDONED}),
    ChangeSetState.ACTIVE: frozenset(
        {ChangeSetState.REVIEW, ChangeSetState.ABANDONED}
    ),
    ChangeSetState.REVIEW: frozenset(
        {ChangeSetState.ACTIVE, ChangeSetState.MERGED, ChangeSetState.ABANDONED}
    ),
    ChangeSetState.MERGED: frozenset({ChangeSetState.CLOSED}),
    ChangeSetState.CLOSED: frozenset(),
    ChangeSetState.ABANDONED: frozenset(),
}

_LINEAGE_STATES = frozenset({ChangeSetState.ACTIVE, ChangeSetState.REVIEW})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ChangeSetError(f"{name} must not be empty")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ChangeSetError(f"{name} must include a timezone")


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """An immutable snapshot of intent, working context, and lineage.

    Mutating operations return a new snapshot and, where Git context changes,
    append an explicit lineage event. Repository identity never changes.
    """

    id: str
    goal: str
    repository_id: str
    base_sha: str
    head_sha: str
    branch: str
    worktree: str
    scopes: tuple[str, ...] = ()
    state: ChangeSetState = ChangeSetState.PLANNED
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    lineage: tuple[LineageEvent, ...] = ()
    scope_amendments: tuple[ScopeAmendment, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "id",
            "goal",
            "repository_id",
            "base_sha",
            "head_sha",
            "branch",
            "worktree",
        ):
            _require_text(name, getattr(self, name))
        _require_aware("created_at", self.created_at)
        _require_aware("updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise ChangeSetError("updated_at must not precede created_at")
        if len(set(self.scopes)) != len(self.scopes):
            raise ChangeSetError("scopes must not contain duplicates")

    @classmethod
    def plan(
        cls,
        *,
        id: str,
        goal: str,
        repository_id: str,
        base_sha: str,
        branch: str,
        worktree: str,
        scopes: tuple[str, ...] = (),
        at: datetime | None = None,
    ) -> ChangeSet:
        timestamp = at or _now()
        return cls(
            id=id,
            goal=goal,
            repository_id=repository_id,
            base_sha=base_sha,
            head_sha=base_sha,
            branch=branch,
            worktree=worktree,
            scopes=scopes,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def activate(self, *, head_sha: str | None = None, at: datetime | None = None) -> ChangeSet:
        if self.state is not ChangeSetState.PLANNED:
            raise InvalidTransition(f"cannot activate a {self.state} change set")
        return self._record_lineage(
            kind=LineageEventKind.ACTIVATED,
            head_sha=head_sha or self.head_sha,
            state=ChangeSetState.ACTIVE,
            at=at,
        )

    def transition(self, state: ChangeSetState, *, at: datetime | None = None) -> ChangeSet:
        if state is ChangeSetState.ACTIVE and self.state is ChangeSetState.PLANNED:
            raise InvalidTransition("planned change sets must use activate()")
        if state not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTransition(f"cannot transition from {self.state} to {state}")
        return replace(self, state=state, updated_at=self._operation_time(at))

    def advance_head(self, head_sha: str, *, at: datetime | None = None) -> ChangeSet:
        return self._record_lineage(
            kind=LineageEventKind.HEAD_ADVANCED,
            head_sha=head_sha,
            at=at,
        )

    def move_branch(
        self,
        branch: str,
        *,
        head_sha: str,
        at: datetime | None = None,
    ) -> ChangeSet:
        return self._record_lineage(
            kind=LineageEventKind.BRANCH_MOVED,
            branch=branch,
            head_sha=head_sha,
            at=at,
        )

    def rebase(
        self,
        base_sha: str,
        *,
        head_sha: str,
        at: datetime | None = None,
    ) -> ChangeSet:
        return self._record_lineage(
            kind=LineageEventKind.REBASED,
            base_sha=base_sha,
            head_sha=head_sha,
            at=at,
        )

    def amend_scope(
        self,
        added_scopes: tuple[str, ...],
        *,
        reason: str,
        at: datetime | None = None,
    ) -> ChangeSet:
        """Explicitly widen declared scope; never narrows or silently widens.

        Scope amendment is not Git lineage: it does not touch base/head/branch
        and is recorded separately in ``scope_amendments``, append-only.
        """
        if self.state not in _LINEAGE_STATES:
            raise InvalidLineageChange(
                f"cannot amend scope while change set is {self.state}"
            )
        if not added_scopes:
            raise ChangeSetError("amend_scope requires at least one scope")
        if len(set(added_scopes)) != len(added_scopes):
            raise ChangeSetError("added_scopes must not contain duplicates")
        _require_text("reason", reason)
        genuinely_new = tuple(
            scope for scope in added_scopes if scope not in self.scopes
        )
        if not genuinely_new:
            raise ChangeSetError("added_scopes are already declared")

        timestamp = self._operation_time(at)
        amendment = ScopeAmendment(
            occurred_at=timestamp,
            added_scopes=genuinely_new,
            reason=reason.strip(),
        )
        return replace(
            self,
            scopes=tuple(sorted((*self.scopes, *genuinely_new))),
            updated_at=timestamp,
            scope_amendments=(*self.scope_amendments, amendment),
        )

    def _record_lineage(
        self,
        *,
        kind: LineageEventKind,
        base_sha: str | None = None,
        head_sha: str | None = None,
        branch: str | None = None,
        state: ChangeSetState | None = None,
        at: datetime | None = None,
    ) -> ChangeSet:
        if kind is not LineageEventKind.ACTIVATED and self.state not in _LINEAGE_STATES:
            raise InvalidLineageChange(
                f"cannot change lineage while change set is {self.state}"
            )

        next_base = base_sha or self.base_sha
        next_head = head_sha or self.head_sha
        next_branch = branch or self.branch
        for name, value in (
            ("base_sha", next_base),
            ("head_sha", next_head),
            ("branch", next_branch),
        ):
            _require_text(name, value)

        if kind is LineageEventKind.HEAD_ADVANCED and next_head == self.head_sha:
            raise InvalidLineageChange("head_sha did not change")
        if kind is LineageEventKind.BRANCH_MOVED and next_branch == self.branch:
            raise InvalidLineageChange("branch did not change")
        if kind is LineageEventKind.REBASED and next_base == self.base_sha:
            raise InvalidLineageChange("base_sha did not change")

        timestamp = self._operation_time(at)
        event = LineageEvent(
            kind=kind,
            occurred_at=timestamp,
            previous_base_sha=self.base_sha,
            base_sha=next_base,
            previous_head_sha=self.head_sha,
            head_sha=next_head,
            previous_branch=self.branch,
            branch=next_branch,
        )
        return replace(
            self,
            base_sha=next_base,
            head_sha=next_head,
            branch=next_branch,
            state=state or self.state,
            updated_at=timestamp,
            lineage=(*self.lineage, event),
        )

    def _operation_time(self, at: datetime | None) -> datetime:
        timestamp = at or _now()
        _require_aware("operation timestamp", timestamp)
        if timestamp < self.updated_at:
            raise ChangeSetError("operation timestamp must not precede updated_at")
        return timestamp
