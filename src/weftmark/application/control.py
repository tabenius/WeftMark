"""Idempotent application-level mutations for remote/board control surfaces.

The HTTP/MCP layers may expose these operations, but they do not own claim,
handoff, ledger, or lifecycle semantics. Every successful mutation is recorded
with a request digest so reconnecting clients can safely retry the same command.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Callable, Mapping

from weftmark.application.claims import (
    Claim,
    ClaimService,
    ClaimServiceError,
    claim_to_payload,
)
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import LocalWorkflowError, LocalWorkflowService
from weftmark.application.task_claims import (
    TaskClaimError,
    TaskClaimService,
    task_claim_result_to_payload,
)
from weftmark.domain.lock import LockEventKind, LockState


class ControlServiceError(ValueError):
    """Base class for invalid control requests."""


class ControlConflict(ControlServiceError):
    """Raised when an idempotency key or durable result conflicts with a retry."""


@dataclass(frozen=True, slots=True)
class ControlResult:
    operation: str
    target_id: str
    idempotency_key: str
    replayed: bool
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "target_id": self.target_id,
            "idempotency_key": self.idempotency_key,
            "replayed": self.replayed,
            "result": dict(self.payload),
        }


class ControlService:
    """Compose existing services behind a retry-safe mutation boundary.

    A process lock closes the small in-process race between checking the
    idempotency ledger and recording a result. Cross-process correctness still
    comes from the existing optimistic/application services and the file-locked
    ledger. Crash recovery is operation-specific and never grants new authority.
    """

    _IDEMPOTENCY_KIND = "control_idempotency_v0"

    def __init__(
        self,
        task_claims: TaskClaimService,
        claims: ClaimService,
        workflow: LocalWorkflowService,
        ledger: LedgerService,
    ) -> None:
        self._task_claims = task_claims
        self._claims = claims
        self._workflow = workflow
        self._ledger = ledger
        self._lock = Lock()

    def claim_task(
        self,
        task_id: str,
        *,
        idempotency_key: str,
        change_set_id: str,
        claim_id: str,
        base_revision: str,
        agent_id: str,
        session_id: str,
        lease_seconds: int,
        requested_at: datetime,
    ) -> ControlResult:
        task_id = _require_text("task_id", task_id)
        change_set_id = _require_text("change_set_id", change_set_id)
        claim_id = _require_text("claim_id", claim_id)
        base_revision = _require_text("base_revision", base_revision)
        agent_id = _require_text("agent_id", agent_id)
        session_id = _require_text("session_id", session_id)
        _require_time(requested_at)
        if lease_seconds < 1 or lease_seconds > 604_800:
            raise ControlServiceError(
                "lease_seconds must be between 1 and 604800"
            )
        request = {
            "change_set_id": change_set_id,
            "claim_id": claim_id,
            "base_revision": base_revision,
            "agent_id": agent_id,
            "session_id": session_id,
            "lease_seconds": lease_seconds,
        }

        def execute() -> Mapping[str, Any]:
            result = self._task_claims.claim(
                task_id,
                change_set_id=change_set_id,
                claim_id=claim_id,
                base_revision=base_revision,
                agent_id=agent_id,
                session_id=session_id,
                claimed_at=requested_at,
                lease_seconds=lease_seconds,
            )
            return task_claim_result_to_payload(result, observed_at=requested_at)

        return self._execute(
            operation="claim_task",
            target_id=task_id,
            idempotency_key=idempotency_key,
            request=request,
            requested_at=requested_at,
            execute=execute,
        )

    def release_claim(
        self,
        claim_id: str,
        *,
        idempotency_key: str,
        agent_id: str,
        session_id: str,
        reason: str,
        requested_at: datetime,
    ) -> ControlResult:
        claim_id = _require_text("claim_id", claim_id)
        agent_id = _require_text("agent_id", agent_id)
        session_id = _require_text("session_id", session_id)
        reason = _require_text("reason", reason)
        _require_time(requested_at)
        request = {
            "agent_id": agent_id,
            "session_id": session_id,
            "reason": reason,
        }

        def execute() -> Mapping[str, Any]:
            current = self._claims.get(claim_id)
            if current is not None and _same_release(
                current,
                agent_id=agent_id,
                session_id=session_id,
                reason=reason,
                observed_at=requested_at,
            ):
                # Crash recovery: the release may have succeeded before its
                # idempotency result was appended.
                return claim_to_payload(current, observed_at=requested_at)
            released = self._claims.release(
                claim_id,
                agent_id=agent_id,
                session_id=session_id,
                released_at=requested_at,
                reason=reason,
            )
            return claim_to_payload(released, observed_at=requested_at)

        return self._execute(
            operation="release_claim",
            target_id=claim_id,
            idempotency_key=idempotency_key,
            request=request,
            requested_at=requested_at,
            execute=execute,
        )

    def create_handoff(
        self,
        change_set_id: str,
        *,
        idempotency_key: str,
        handoff_id: str,
        task_id: str,
        next_action: str,
        created_by: str,
        requested_at: datetime,
        intended_receiver_id: str | None = None,
        known_failures: tuple[str, ...] = (),
        supersedes_id: str | None = None,
    ) -> ControlResult:
        change_set_id = _require_text("change_set_id", change_set_id)
        handoff_id = _require_text("handoff_id", handoff_id)
        task_id = _require_text("task_id", task_id)
        next_action = _require_text("next_action", next_action)
        created_by = _require_text("created_by", created_by)
        intended_receiver_id = _optional_text(
            "intended_receiver_id", intended_receiver_id
        )
        supersedes_id = _optional_text("supersedes_id", supersedes_id)
        failures = tuple(_require_text("known_failure", value) for value in known_failures)
        _require_time(requested_at)
        request = {
            "handoff_id": handoff_id,
            "task_id": task_id,
            "next_action": next_action,
            "created_by": created_by,
            "intended_receiver_id": intended_receiver_id,
            "known_failures": list(failures),
            "supersedes_id": supersedes_id,
        }

        def execute() -> Mapping[str, Any]:
            current = self._workflow.get_handoff(handoff_id)
            if current is not None:
                if not _same_handoff_request(
                    current.to_dict(),
                    change_set_id=change_set_id,
                    task_id=task_id,
                    next_action=next_action,
                    created_by=created_by,
                    intended_receiver_id=intended_receiver_id,
                    known_failures=failures,
                    supersedes_id=supersedes_id,
                ):
                    raise ControlConflict(
                        f"handoff id already exists with different intent: {handoff_id}"
                    )
                # Crash recovery after durable handoff creation but before the
                # control idempotency record.
                return current.to_dict()
            created = self._workflow.create_handoff(
                change_set_id,
                id=handoff_id,
                task_id=task_id,
                next_action=next_action,
                created_by=created_by,
                created_at=requested_at,
                intended_receiver_id=intended_receiver_id,
                known_failures=failures,
                supersedes_id=supersedes_id,
            )
            return created.to_dict()

        return self._execute(
            operation="create_handoff",
            target_id=change_set_id,
            idempotency_key=idempotency_key,
            request=request,
            requested_at=requested_at,
            execute=execute,
        )

    def _execute(
        self,
        *,
        operation: str,
        target_id: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        requested_at: datetime,
        execute: Callable[[], Mapping[str, Any]],
    ) -> ControlResult:
        key = _require_idempotency_key(idempotency_key)
        request_sha256 = hashlib.sha256(_canonical_json(request).encode()).hexdigest()
        with self._lock:
            prior = self._ledger.latest(kind=self._IDEMPOTENCY_KIND, entity_id=key)
            if prior is not None:
                payload = prior.payload
                if (
                    payload.get("schema_version") != 1
                    or payload.get("operation") != operation
                    or payload.get("target_id") != target_id
                    or payload.get("request_sha256") != request_sha256
                ):
                    raise ControlConflict(
                        "idempotency key was already used for a different request"
                    )
                response = payload.get("response")
                if not isinstance(response, Mapping):
                    raise ControlServiceError(
                        "stored control idempotency result is malformed"
                    )
                return ControlResult(operation, target_id, key, True, response)

            response = dict(execute())
            self._ledger.record(
                kind=self._IDEMPOTENCY_KIND,
                entity_id=key,
                payload={
                    "schema_version": 1,
                    "operation": operation,
                    "target_id": target_id,
                    "request_sha256": request_sha256,
                    "response": response,
                },
                recorded_at=requested_at,
            )
            return ControlResult(operation, target_id, key, False, response)


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ControlServiceError("control request must be JSON-safe") from error


def _require_text(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ControlServiceError(f"{name} must not be empty")
    if "\x00" in normalized:
        raise ControlServiceError(f"{name} must not contain NUL")
    return normalized


def _optional_text(name: str, value: str | None) -> str | None:
    return None if value is None else _require_text(name, value)


def _require_time(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ControlServiceError("requested_at must include a timezone")


def _require_idempotency_key(value: str) -> str:
    normalized = _require_text("idempotency_key", value)
    if len(normalized) > 200:
        raise ControlServiceError("idempotency_key must not exceed 200 characters")
    return normalized


def _same_release(
    claim: Claim,
    *,
    agent_id: str,
    session_id: str,
    reason: str,
    observed_at: datetime,
) -> bool:
    if claim.agent_id != agent_id or claim.session_id != session_id:
        return False
    if claim.state_at(observed_at) is not LockState.RELEASED:
        return False
    for lock in claim.locks:
        if not lock.events:
            return False
        event = lock.events[-1]
        if event.kind is not LockEventKind.RELEASED or event.reason != reason:
            return False
    return True


def _same_handoff_request(
    payload: Mapping[str, Any],
    *,
    change_set_id: str,
    task_id: str,
    next_action: str,
    created_by: str,
    intended_receiver_id: str | None,
    known_failures: tuple[str, ...],
    supersedes_id: str | None,
) -> bool:
    return (
        payload.get("change_set_id") == change_set_id
        and payload.get("task_id") == task_id
        and payload.get("next_action") == next_action
        and payload.get("created_by") == created_by
        and payload.get("intended_receiver_id") == intended_receiver_id
        and tuple(payload.get("known_failures", ())) == known_failures
        and payload.get("supersedes_id") == supersedes_id
    )


# Re-exported exception types make interface layers able to map failures without
# importing deeper implementation modules solely for exception classification.
CONTROL_MUTATION_ERRORS = (
    ControlServiceError,
    ControlConflict,
    TaskClaimError,
    ClaimServiceError,
    LocalWorkflowError,
)
