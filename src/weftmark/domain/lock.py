"""Lease ownership and conflict rules for canonical WeftMark scopes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatchcase

from weftmark.domain.scope import Scope, ScopeKind


class LockError(ValueError):
    """Base class for invalid lock records and operations."""


class InvalidLockOperation(LockError):
    """Raised when a lease operation is invalid for its current state."""


class LockState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class LockEventKind(StrEnum):
    ACQUIRED = "acquired"
    REACQUIRED = "reacquired"
    RENEWED = "renewed"
    RELEASED = "released"
    EXPIRED = "expired"


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise LockError(f"{name} must not be empty")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LockError(f"{name} must include a timezone")


@dataclass(frozen=True, slots=True)
class LockEvent:
    kind: LockEventKind
    occurred_at: datetime
    previous_expires_at: datetime | None = None
    expires_at: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticLock:
    id: str
    scope: Scope
    agent_id: str
    session_id: str
    change_set_id: str
    acquired_at: datetime
    expires_at: datetime
    state: LockState = LockState.ACTIVE
    updated_at: datetime | None = None
    events: tuple[LockEvent, ...] = ()

    def __post_init__(self) -> None:
        for name in ("id", "agent_id", "session_id", "change_set_id"):
            _require_text(name, getattr(self, name))
        _require_aware("acquired_at", self.acquired_at)
        _require_aware("expires_at", self.expires_at)
        if self.expires_at <= self.acquired_at:
            raise LockError("expires_at must follow acquired_at")
        if self.updated_at is not None:
            _require_aware("updated_at", self.updated_at)
            if self.updated_at < self.acquired_at:
                raise LockError("updated_at must not precede acquired_at")

    @classmethod
    def acquire(
        cls,
        *,
        id: str,
        scope: Scope,
        agent_id: str,
        session_id: str,
        change_set_id: str,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> SemanticLock:
        event = LockEvent(
            LockEventKind.ACQUIRED,
            occurred_at=acquired_at,
            expires_at=expires_at,
        )
        return cls(
            id=id,
            scope=scope,
            agent_id=agent_id,
            session_id=session_id,
            change_set_id=change_set_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
            updated_at=acquired_at,
            events=(event,),
        )

    def state_at(self, at: datetime) -> LockState:
        _require_aware("observation time", at)
        if self.state is LockState.ACTIVE and at >= self.expires_at:
            return LockState.EXPIRED
        return self.state

    def owns_scope_at(self, at: datetime) -> bool:
        return self.state_at(at) is LockState.ACTIVE

    def renew(self, *, at: datetime, expires_at: datetime) -> SemanticLock:
        self._require_active_lease(at)
        _require_aware("expires_at", expires_at)
        if expires_at <= self.expires_at:
            raise InvalidLockOperation("renewal must extend expires_at")
        event = LockEvent(
            LockEventKind.RENEWED,
            occurred_at=at,
            previous_expires_at=self.expires_at,
            expires_at=expires_at,
        )
        return replace(
            self,
            expires_at=expires_at,
            updated_at=at,
            events=(*self.events, event),
        )

    def reacquire(self, *, at: datetime, expires_at: datetime) -> SemanticLock:
        """Restore an expired lease while preserving its ownership history."""

        _require_aware("operation time", at)
        _require_aware("expires_at", expires_at)
        if self.state_at(at) is not LockState.EXPIRED:
            raise InvalidLockOperation("only an expired lease can be reacquired")
        if self.updated_at is not None and at < self.updated_at:
            raise InvalidLockOperation("operation time precedes the previous event")
        if expires_at <= at:
            raise InvalidLockOperation("reacquired lease must expire after operation time")
        event = LockEvent(
            LockEventKind.REACQUIRED,
            occurred_at=at,
            previous_expires_at=self.expires_at,
            expires_at=expires_at,
        )
        return replace(
            self,
            expires_at=expires_at,
            state=LockState.ACTIVE,
            updated_at=at,
            events=(*self.events, event),
        )

    def release(self, *, at: datetime, reason: str) -> SemanticLock:
        _require_text("release reason", reason)
        self._require_active_lease(at)
        event = LockEvent(
            LockEventKind.RELEASED,
            occurred_at=at,
            previous_expires_at=self.expires_at,
            expires_at=self.expires_at,
            reason=reason,
        )
        return replace(
            self,
            state=LockState.RELEASED,
            updated_at=at,
            events=(*self.events, event),
        )

    def observe_expiry(self, *, at: datetime) -> SemanticLock:
        _require_aware("observation time", at)
        if self.state is not LockState.ACTIVE:
            raise InvalidLockOperation(f"cannot expire a {self.state} lock")
        if at < self.expires_at:
            raise InvalidLockOperation("lease has not expired")
        event = LockEvent(
            LockEventKind.EXPIRED,
            occurred_at=self.expires_at,
            previous_expires_at=self.expires_at,
            expires_at=self.expires_at,
        )
        return replace(
            self,
            state=LockState.EXPIRED,
            updated_at=at,
            events=(*self.events, event),
        )

    def conflicts_with(self, other: SemanticLock, *, at: datetime) -> bool:
        if self.id == other.id:
            return False
        if not self.owns_scope_at(at) or not other.owns_scope_at(at):
            return False
        return scopes_overlap(self.scope, other.scope)

    def _require_active_lease(self, at: datetime) -> None:
        _require_aware("operation time", at)
        if self.state is not LockState.ACTIVE:
            raise InvalidLockOperation(f"lock is {self.state}")
        if at < self.acquired_at:
            raise InvalidLockOperation("operation time precedes acquisition")
        if self.updated_at is not None and at < self.updated_at:
            raise InvalidLockOperation("operation time precedes the previous event")
        if at >= self.expires_at:
            raise InvalidLockOperation("lease has expired")


def scopes_overlap(first: Scope, second: Scope) -> bool:
    """Return whether two canonical scopes represent competing ownership."""

    if first.kind is not second.kind:
        return False
    if first.kind is not ScopeKind.FILE:
        return first.identity == second.identity
    return _file_scopes_overlap(first.key, second.key)


def _file_scopes_overlap(first: str, second: str) -> bool:
    if first == second:
        return True
    first_magic = _has_magic(first)
    second_magic = _has_magic(second)
    if not first_magic and not second_magic:
        return False
    if first_magic and not second_magic:
        return fnmatchcase(second, first)
    if second_magic and not first_magic:
        return fnmatchcase(first, second)

    first_prefix = _literal_prefix(first)
    second_prefix = _literal_prefix(second)
    if not first_prefix or not second_prefix:
        return True
    return _path_prefix(first_prefix, second_prefix) or _path_prefix(
        second_prefix, first_prefix
    )


def _has_magic(value: str) -> bool:
    return any(character in value for character in "*?[")


def _literal_prefix(value: str) -> str:
    positions = [value.find(character) for character in "*?[" if character in value]
    return value[: min(positions)].rstrip("/")


def _path_prefix(value: str, prefix: str) -> bool:
    return value == prefix or value.startswith(f"{prefix}/")
