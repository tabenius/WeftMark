"""Retryable native task-to-Change-Set claim composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from weftmark.application.claims import Claim, ClaimService, claim_to_payload
from weftmark.application.identifiers import new_id
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import LEDGER_GENESIS_DIGEST, LedgerHeadChanged
from weftmark.application.task_planning import TaskPlanningService
from weftmark.application.tasks import TaskService
from weftmark.application.workspace import WorkspaceService, binding_to_payload
from weftmark.domain.lock import LockState
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskState


class TaskClaimError(ValueError):
    """Raised when native task intent cannot become local work authority."""


@dataclass(frozen=True, slots=True)
class TaskWorkBinding:
    task_id: str
    change_set_id: str
    claim_id: str
    agent_id: str
    session_id: str
    base_revision: str
    created_at: datetime
    completed: bool


@dataclass(frozen=True, slots=True)
class TaskClaimResult:
    binding: TaskWorkBinding
    change_set: Mapping[str, Any]
    claim: Claim
    claimed: bool


class TaskClaimService:
    def __init__(
        self,
        planning: TaskPlanningService,
        tasks: TaskService,
        workspace: WorkspaceService,
        claims: ClaimService,
        ledger: LedgerService,
    ) -> None:
        self._planning = planning
        self._tasks = tasks
        self._workspace = workspace
        self._claims = claims
        self._ledger = ledger

    def claim(
        self,
        task_id: str,
        *,
        change_set_id: str | None,
        claim_id: str | None,
        base_revision: str,
        agent_id: str,
        session_id: str,
        claimed_at: datetime,
        lease_seconds: int,
    ) -> TaskClaimResult:
        _validate_request(
            change_set_id=change_set_id,
            claim_id=claim_id,
            base_revision=base_revision,
            agent_id=agent_id,
            session_id=session_id,
            claimed_at=claimed_at,
            lease_seconds=lease_seconds,
        )
        existing = self.get(task_id)
        if existing is None:
            eligibility = self._planning.eligibility(task_id)
            if not eligibility.eligible:
                raise TaskClaimError(
                    f"Task is not eligible: {task_id}: "
                    + "; ".join(eligibility.reasons)
                )
            task = eligibility.task
            if task.state is not TaskState.TODO:
                raise TaskClaimError("native task must be todo before claim")
            if not task.scopes:
                raise TaskClaimError("native task claim requires declared scopes")
            reservation = TaskWorkBinding(
                task_id,
                change_set_id or new_id("chg", at=claimed_at),
                claim_id or new_id("claim", at=claimed_at),
                agent_id,
                session_id,
                base_revision,
                claimed_at,
                False,
            )
            existing = self._reserve(reservation)
        _require_same_request(
            existing,
            change_set_id=change_set_id,
            claim_id=claim_id,
            base_revision=base_revision,
            agent_id=agent_id,
            session_id=session_id,
        )
        return self._finish(
            existing,
            observed_at=claimed_at,
            lease_seconds=lease_seconds,
        )

    def get(self, task_id: str) -> TaskWorkBinding | None:
        entry = self._ledger.latest(kind="task_work_claim", entity_id=task_id)
        return None if entry is None else _binding_from_payload(entry.payload)

    def _reserve(self, reservation: TaskWorkBinding) -> TaskWorkBinding:
        for _ in range(8):
            entries = self._ledger.snapshot()
            existing = next(
                (
                    entry
                    for entry in reversed(entries)
                    if entry.kind == "task_work_claim"
                    and entry.entity_id == reservation.task_id
                ),
                None,
            )
            if existing is not None:
                return _binding_from_payload(existing.payload)
            expected = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
            try:
                self._ledger.record_if_head(
                    kind="task_work_claim",
                    entity_id=reservation.task_id,
                    payload=_binding_to_payload(reservation),
                    recorded_at=reservation.created_at,
                    expected_digest=expected,
                )
                return reservation
            except LedgerHeadChanged:
                continue
        raise TaskClaimError("ledger remained busy while reserving task claim")

    def _finish(
        self,
        binding: TaskWorkBinding,
        *,
        observed_at: datetime,
        lease_seconds: int,
    ) -> TaskClaimResult:
        task = self._tasks.require(binding.task_id)
        scopes = tuple(Scope.parse(value) for value in task.scopes)
        if not scopes:
            raise TaskClaimError("native task claim requires declared scopes")

        change_set = self._workspace.get_change_set(binding.change_set_id)
        if change_set is None:
            change_set = self._workspace.create_change_set(
                id=binding.change_set_id,
                goal=task.title,
                base_revision=binding.base_revision,
                scopes=scopes,
                created_at=binding.created_at,
            )
        else:
            payload = binding_to_payload(change_set)
            if (
                payload["goal"] != task.title
                or (
                    binding.base_revision != "HEAD"
                    and payload["base_revision"] != binding.base_revision
                )
                or tuple(
                    sorted(Scope.from_dict(value).canonical for value in payload["scopes"])
                )
                != tuple(sorted(scope.canonical for scope in scopes))
            ):
                raise TaskClaimError(
                    "reserved Change Set exists with different native task intent"
                )

        claim = self._claims.get(binding.claim_id)
        claimed = False
        if claim is None:
            claim = self._claims.acquire(
                binding.change_set_id,
                id=binding.claim_id,
                agent_id=binding.agent_id,
                session_id=binding.session_id,
                acquired_at=observed_at,
                lease_seconds=lease_seconds,
            )
            claimed = True
        else:
            _require_matching_active_claim(claim, binding, observed_at=observed_at)

        current = self._tasks.require(binding.task_id)
        if current.state is TaskState.TODO:
            self._tasks.transition(
                binding.task_id,
                state=TaskState.IN_PROGRESS,
                actor_id=binding.agent_id,
                rationale=f"native claim {binding.claim_id} acquired",
                occurred_at=observed_at,
            )
        elif current.state is not TaskState.IN_PROGRESS:
            raise TaskClaimError(
                f"reserved native task has incompatible state: {current.state.value}"
            )

        completed = self._complete(binding, recorded_at=observed_at)
        return TaskClaimResult(
            completed,
            binding_to_payload(change_set),
            claim,
            claimed,
        )

    def _complete(
        self, binding: TaskWorkBinding, *, recorded_at: datetime
    ) -> TaskWorkBinding:
        for _ in range(8):
            entries = self._ledger.snapshot()
            latest = next(
                (
                    entry
                    for entry in reversed(entries)
                    if entry.kind == "task_work_claim"
                    and entry.entity_id == binding.task_id
                ),
                None,
            )
            if latest is None:
                raise TaskClaimError("task claim reservation disappeared")
            current = _binding_from_payload(latest.payload)
            if current.completed:
                return current
            completed = TaskWorkBinding(
                current.task_id,
                current.change_set_id,
                current.claim_id,
                current.agent_id,
                current.session_id,
                current.base_revision,
                current.created_at,
                True,
            )
            expected = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
            try:
                self._ledger.record_if_head(
                    kind="task_work_claim",
                    entity_id=completed.task_id,
                    payload=_binding_to_payload(completed),
                    recorded_at=recorded_at,
                    expected_digest=expected,
                )
                return completed
            except LedgerHeadChanged:
                continue
        raise TaskClaimError("ledger remained busy while completing task claim")


def task_claim_result_to_payload(
    result: TaskClaimResult, *, observed_at: datetime
) -> dict[str, Any]:
    return {
        "claimed": result.claimed,
        "task_id": result.binding.task_id,
        "completed": result.binding.completed,
        "change_set": dict(result.change_set),
        "claim": claim_to_payload(result.claim, observed_at=observed_at),
    }


def _binding_to_payload(value: TaskWorkBinding) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "state": "completed" if value.completed else "reserved",
        "task_id": value.task_id,
        "change_set_id": value.change_set_id,
        "claim_id": value.claim_id,
        "agent_id": value.agent_id,
        "session_id": value.session_id,
        "base_revision": value.base_revision,
        "created_at": value.created_at.isoformat(),
    }


def _binding_from_payload(payload: Mapping[str, Any]) -> TaskWorkBinding:
    try:
        if payload["schema_version"] != 1:
            raise ValueError("unsupported task claim schema")
        state = str(payload["state"])
        if state not in {"reserved", "completed"}:
            raise ValueError("unsupported task claim state")
        values = tuple(
            str(payload[name]).strip()
            for name in (
                "task_id",
                "change_set_id",
                "claim_id",
                "agent_id",
                "session_id",
                "base_revision",
            )
        )
        if not all(values):
            raise ValueError("empty task claim identity")
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("task claim time lacks timezone")
        return TaskWorkBinding(*values, created_at, state == "completed")
    except (KeyError, TypeError, ValueError) as error:
        raise TaskClaimError("stored native task claim is malformed") from error


def _require_same_request(
    binding: TaskWorkBinding,
    *,
    change_set_id: str | None,
    claim_id: str | None,
    base_revision: str,
    agent_id: str,
    session_id: str,
) -> None:
    if (
        (change_set_id is not None and change_set_id != binding.change_set_id)
        or (claim_id is not None and claim_id != binding.claim_id)
        or base_revision != binding.base_revision
        or agent_id != binding.agent_id
        or session_id != binding.session_id
    ):
        raise TaskClaimError(
            f"native task already has different work binding: {binding.task_id}"
        )


def _require_matching_active_claim(
    claim: Claim, binding: TaskWorkBinding, *, observed_at: datetime
) -> None:
    if (
        claim.change_set_id != binding.change_set_id
        or claim.agent_id != binding.agent_id
        or claim.session_id != binding.session_id
    ):
        raise TaskClaimError(f"Claim already exists with different intent: {claim.id}")
    if claim.state_at(observed_at) is not LockState.ACTIVE:
        raise TaskClaimError(f"Claim is no longer active: {claim.id}")


def _validate_request(
    *,
    change_set_id: str | None,
    claim_id: str | None,
    base_revision: str,
    agent_id: str,
    session_id: str,
    claimed_at: datetime,
    lease_seconds: int,
) -> None:
    if claimed_at.tzinfo is None or claimed_at.utcoffset() is None:
        raise TaskClaimError("claimed_at must include a timezone")
    for name, value in (
        ("base revision", base_revision),
        ("agent", agent_id),
        ("session", session_id),
    ):
        if not value.strip():
            raise TaskClaimError(f"{name} must not be empty")
    if change_set_id is not None and not change_set_id.strip():
        raise TaskClaimError("Change Set id must not be empty")
    if claim_id is not None and not claim_id.strip():
        raise TaskClaimError("claim id must not be empty")
    if lease_seconds < 1 or lease_seconds > 604_800:
        raise TaskClaimError(
            "lease duration must be between 1 and 604800 seconds"
        )

