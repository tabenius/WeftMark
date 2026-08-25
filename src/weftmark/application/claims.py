"""Atomic, durable ownership claims over Change Set scopes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import (
    LEDGER_GENESIS_DIGEST,
    LedgerEntry,
    LedgerHeadChanged,
)
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.lock import (
    LockEvent,
    LockEventKind,
    LockState,
    SemanticLock,
)
from weftmark.domain.scope import Scope


class ClaimServiceError(ValueError):
    """Raised when a claim request or stored claim is invalid."""


class ClaimConflict(ClaimServiceError):
    """Raised when an active claim already owns an overlapping scope."""


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    change_set_id: str
    agent_id: str
    session_id: str
    locks: tuple[SemanticLock, ...]
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ClaimServiceError("claim id must not be empty")
        if not self.locks:
            raise ClaimServiceError("claim must contain at least one lock")
        for lock in self.locks:
            if (
                lock.change_set_id != self.change_set_id
                or lock.agent_id != self.agent_id
                or lock.session_id != self.session_id
            ):
                raise ClaimServiceError("claim lock identity is inconsistent")
        if len({lock.scope.identity for lock in self.locks}) != len(self.locks):
            raise ClaimServiceError("claim scopes must not contain duplicates")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ClaimServiceError("claim updated_at must include a timezone")

    def state_at(self, at: datetime) -> LockState:
        states = {lock.state_at(at) for lock in self.locks}
        if len(states) != 1:
            raise ClaimServiceError("claim locks have inconsistent states")
        return states.pop()


class ClaimService:
    def __init__(self, workspace: WorkspaceService, ledger: LedgerService) -> None:
        self._workspace = workspace
        self._ledger = ledger

    def acquire(
        self,
        change_set_id: str,
        *,
        id: str,
        agent_id: str,
        session_id: str,
        acquired_at: datetime,
        lease_seconds: int,
    ) -> Claim:
        _require_lease_seconds(lease_seconds)
        binding = self._workspace.require_change_set(change_set_id)
        scopes = tuple(Scope.parse(value) for value in binding.change_set.scopes)
        expires_at = acquired_at + timedelta(seconds=lease_seconds)
        claim = Claim(
            id=id,
            change_set_id=change_set_id,
            agent_id=agent_id,
            session_id=session_id,
            locks=tuple(
                SemanticLock.acquire(
                    id=f"{id}:{index}",
                    scope=scope,
                    agent_id=agent_id,
                    session_id=session_id,
                    change_set_id=change_set_id,
                    acquired_at=acquired_at,
                    expires_at=expires_at,
                )
                for index, scope in enumerate(scopes, start=1)
            ),
            updated_at=acquired_at,
        )
        for _ in range(8):
            snapshot = self._ledger.snapshot()
            current = _latest_claims(snapshot)
            if id in current:
                raise ClaimServiceError(f"Claim already exists: {id}")
            for existing in current.values():
                for requested_lock in claim.locks:
                    for existing_lock in existing.locks:
                        if requested_lock.conflicts_with(existing_lock, at=acquired_at):
                            raise ClaimConflict(
                                f"scope {requested_lock.scope.canonical} is owned by "
                                f"claim {existing.id} until {existing_lock.expires_at.isoformat()}"
                            )
            try:
                self._append(claim, snapshot)
                return claim
            except LedgerHeadChanged:
                continue
        raise ClaimServiceError("ledger remained busy while acquiring claim; retry")

    def renew(
        self,
        id: str,
        *,
        agent_id: str,
        session_id: str,
        renewed_at: datetime,
        extend_seconds: int,
    ) -> Claim:
        _require_lease_seconds(extend_seconds)
        for _ in range(8):
            snapshot = self._ledger.snapshot()
            claim = _require_claim(snapshot, id)
            _require_owner(claim, agent_id=agent_id, session_id=session_id)
            expires_at = max(lock.expires_at for lock in claim.locks) + timedelta(
                seconds=extend_seconds
            )
            renewed = Claim(
                id=claim.id,
                change_set_id=claim.change_set_id,
                agent_id=claim.agent_id,
                session_id=claim.session_id,
                locks=tuple(
                    lock.renew(at=renewed_at, expires_at=expires_at)
                    for lock in claim.locks
                ),
                updated_at=renewed_at,
            )
            try:
                self._append(renewed, snapshot)
                return renewed
            except LedgerHeadChanged:
                continue
        raise ClaimServiceError("ledger remained busy while renewing claim; retry")

    def reacquire(
        self,
        id: str,
        *,
        agent_id: str,
        session_id: str,
        reacquired_at: datetime,
        lease_seconds: int,
    ) -> Claim:
        """Reacquire an expired claim without changing its bound identity."""

        _require_lease_seconds(lease_seconds)
        for _ in range(8):
            snapshot = self._ledger.snapshot()
            current = _latest_claims(snapshot)
            claim = current.get(id)
            if claim is None:
                raise ClaimServiceError(f"Claim not found: {id}")
            _require_owner(claim, agent_id=agent_id, session_id=session_id)
            if claim.state_at(reacquired_at) is not LockState.EXPIRED:
                raise ClaimServiceError("only an expired claim can be reacquired")
            expires_at = reacquired_at + timedelta(seconds=lease_seconds)
            reacquired = Claim(
                id=claim.id,
                change_set_id=claim.change_set_id,
                agent_id=claim.agent_id,
                session_id=claim.session_id,
                locks=tuple(
                    lock.reacquire(at=reacquired_at, expires_at=expires_at)
                    for lock in claim.locks
                ),
                updated_at=reacquired_at,
            )
            for existing_id, existing in current.items():
                if existing_id == id:
                    continue
                for requested_lock in reacquired.locks:
                    for existing_lock in existing.locks:
                        if requested_lock.conflicts_with(
                            existing_lock, at=reacquired_at
                        ):
                            raise ClaimConflict(
                                f"scope {requested_lock.scope.canonical} is owned by "
                                f"claim {existing.id} until "
                                f"{existing_lock.expires_at.isoformat()}"
                            )
            try:
                self._append(reacquired, snapshot)
                return reacquired
            except LedgerHeadChanged:
                continue
        raise ClaimServiceError("ledger remained busy while reacquiring claim; retry")

    def release(
        self,
        id: str,
        *,
        agent_id: str,
        session_id: str,
        released_at: datetime,
        reason: str,
    ) -> Claim:
        for _ in range(8):
            snapshot = self._ledger.snapshot()
            claim = _require_claim(snapshot, id)
            _require_owner(claim, agent_id=agent_id, session_id=session_id)
            released = Claim(
                id=claim.id,
                change_set_id=claim.change_set_id,
                agent_id=claim.agent_id,
                session_id=claim.session_id,
                locks=tuple(
                    lock.release(at=released_at, reason=reason)
                    for lock in claim.locks
                ),
                updated_at=released_at,
            )
            try:
                self._append(released, snapshot)
                return released
            except LedgerHeadChanged:
                continue
        raise ClaimServiceError("ledger remained busy while releasing claim; retry")

    def get(self, id: str) -> Claim | None:
        return _latest_claims(self._ledger.snapshot()).get(id)

    def list(self, *, change_set_id: str | None = None) -> tuple[Claim, ...]:
        values = tuple(_latest_claims(self._ledger.snapshot()).values())
        if change_set_id is None:
            return values
        return tuple(value for value in values if value.change_set_id == change_set_id)

    def _append(self, claim: Claim, snapshot: tuple[LedgerEntry, ...]) -> None:
        expected = snapshot[-1].digest if snapshot else LEDGER_GENESIS_DIGEST
        self._ledger.record_if_head(
            kind="claim",
            entity_id=claim.id,
            payload=claim_to_payload(claim),
            recorded_at=claim.updated_at,
            expected_digest=expected,
        )


def claim_to_payload(
    claim: Claim, *, observed_at: datetime | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "id": claim.id,
        "change_set_id": claim.change_set_id,
        "agent_id": claim.agent_id,
        "session_id": claim.session_id,
        "updated_at": claim.updated_at.isoformat(),
        "locks": [_lock_to_payload(lock) for lock in claim.locks],
    }
    if observed_at is not None:
        payload["effective_state"] = claim.state_at(observed_at).value
    return payload


def claim_from_payload(payload: Mapping[str, Any]) -> Claim:
    try:
        if payload["schema_version"] != 1:
            raise ValueError("unsupported schema")
        return Claim(
            id=str(payload["id"]),
            change_set_id=str(payload["change_set_id"]),
            agent_id=str(payload["agent_id"]),
            session_id=str(payload["session_id"]),
            locks=tuple(_lock_from_payload(value) for value in payload["locks"]),
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ClaimServiceError("stored claim snapshot is malformed") from error


def _latest_claims(entries: tuple[LedgerEntry, ...]) -> dict[str, Claim]:
    values: dict[str, Claim] = {}
    for entry in entries:
        if entry.kind == "claim":
            values[entry.entity_id] = claim_from_payload(entry.payload)
    return values


def _require_claim(entries: tuple[LedgerEntry, ...], id: str) -> Claim:
    claim = _latest_claims(entries).get(id)
    if claim is None:
        raise ClaimServiceError(f"Claim not found: {id}")
    return claim


def _lock_to_payload(lock: SemanticLock) -> dict[str, Any]:
    return {
        "id": lock.id,
        "scope": lock.scope.to_dict(),
        "agent_id": lock.agent_id,
        "session_id": lock.session_id,
        "change_set_id": lock.change_set_id,
        "acquired_at": lock.acquired_at.isoformat(),
        "expires_at": lock.expires_at.isoformat(),
        "state": lock.state.value,
        "updated_at": None if lock.updated_at is None else lock.updated_at.isoformat(),
        "events": [
            {
                "kind": event.kind.value,
                "occurred_at": event.occurred_at.isoformat(),
                "previous_expires_at": (
                    None
                    if event.previous_expires_at is None
                    else event.previous_expires_at.isoformat()
                ),
                "expires_at": (
                    None if event.expires_at is None else event.expires_at.isoformat()
                ),
                "reason": event.reason,
            }
            for event in lock.events
        ],
    }


def _lock_from_payload(payload: Mapping[str, Any]) -> SemanticLock:
    return SemanticLock(
        id=str(payload["id"]),
        scope=Scope.from_dict(payload["scope"]),
        agent_id=str(payload["agent_id"]),
        session_id=str(payload["session_id"]),
        change_set_id=str(payload["change_set_id"]),
        acquired_at=datetime.fromisoformat(str(payload["acquired_at"])),
        expires_at=datetime.fromisoformat(str(payload["expires_at"])),
        state=LockState(str(payload["state"])),
        updated_at=(
            None
            if payload["updated_at"] is None
            else datetime.fromisoformat(str(payload["updated_at"]))
        ),
        events=tuple(
            LockEvent(
                kind=LockEventKind(str(value["kind"])),
                occurred_at=datetime.fromisoformat(str(value["occurred_at"])),
                previous_expires_at=(
                    None
                    if value["previous_expires_at"] is None
                    else datetime.fromisoformat(str(value["previous_expires_at"]))
                ),
                expires_at=(
                    None
                    if value["expires_at"] is None
                    else datetime.fromisoformat(str(value["expires_at"]))
                ),
                reason=None if value["reason"] is None else str(value["reason"]),
            )
            for value in payload["events"]
        ),
    )


def _require_lease_seconds(value: int) -> None:
    if value < 1 or value > 604_800:
        raise ClaimServiceError("lease duration must be between 1 and 604800 seconds")


def _require_owner(claim: Claim, *, agent_id: str, session_id: str) -> None:
    if claim.agent_id != agent_id or claim.session_id != session_id:
        raise ClaimServiceError("claim operation requires the owning agent and session")
