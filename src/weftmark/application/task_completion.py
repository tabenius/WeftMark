"""Evidence-gated, retry-safe completion of claimed native tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from weftmark.application.claims import (
    Claim,
    ClaimService,
    ClaimServiceError,
    claim_from_payload,
)
from weftmark.application.change_binding import ChangeBinding
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import (
    LEDGER_GENESIS_DIGEST,
    LedgerEntry,
    LedgerHeadChanged,
)
from weftmark.application.task_claims import TaskClaimService, TaskWorkBinding
from weftmark.application.tasks import task_from_payload, task_to_payload
from weftmark.application.workspace import binding_from_payload
from weftmark.domain.changeset import ChangeSetState
from weftmark.domain.lock import LockState
from weftmark.domain.task import TaskError, TaskIntent, TaskState


class TaskCompletionError(ValueError):
    """Raised when durable proof does not permit native task completion."""


@dataclass(frozen=True, slots=True)
class TaskCompletionResult:
    task: TaskIntent
    binding: TaskWorkBinding
    review_id: str
    head_sha: str
    completed: bool
    claim_released: bool


class TaskCompletionService:
    """Close a native task only after its bound work is demonstrably releasable."""

    def __init__(
        self,
        task_claims: TaskClaimService,
        claims: ClaimService,
        ledger: LedgerService,
    ) -> None:
        self._task_claims = task_claims
        self._claims = claims
        self._ledger = ledger

    def complete(
        self,
        task_id: str,
        *,
        actor_id: str,
        reason: str,
        completed_at: datetime,
    ) -> TaskCompletionResult:
        _require_text("task id", task_id)
        _require_text("actor", actor_id)
        _require_text("reason", reason)
        _require_aware("completed_at", completed_at)

        binding = self._task_claims.get(task_id)
        if binding is None or not binding.completed:
            raise TaskCompletionError(
                "task completion requires a completed native work binding"
            )
        current, change_set, claim, review = _proof_from_entries(
            self._ledger.snapshot(),
            binding=binding,
            observed_at=completed_at,
        )
        review_id = str(review["decision"]["id"])
        if current.state is TaskState.DONE:
            _require_same_retry(current, actor_id=actor_id, reason=reason)
            return TaskCompletionResult(
                current,
                binding,
                review_id,
                change_set.latest.head_sha,
                False,
                False,
            )
        if completed_at < max(
            current.updated_at,
            change_set.change_set.updated_at,
            claim.updated_at,
            datetime.fromisoformat(str(review["decision"]["created_at"])),
        ):
            raise TaskCompletionError("completion time predates required durable proof")

        claim_state = _claim_state(claim, completed_at)
        claim_released = False
        if claim_state is LockState.ACTIVE:
            self._claims.release(
                binding.claim_id,
                agent_id=binding.agent_id,
                session_id=binding.session_id,
                released_at=completed_at,
                reason=f"native task {task_id} completed",
            )
            claim_released = True

        for _ in range(8):
            entries = self._ledger.snapshot()
            current, change_set, claim, review = _proof_from_entries(
                entries,
                binding=binding,
                observed_at=completed_at,
                require_released=True,
            )
            review_id = str(review["decision"]["id"])
            if current.state is TaskState.DONE:
                _require_same_retry(current, actor_id=actor_id, reason=reason)
                return TaskCompletionResult(
                    current,
                    binding,
                    review_id,
                    change_set.latest.head_sha,
                    False,
                    claim_released,
                )
            if current.state is not TaskState.IN_PROGRESS:
                raise TaskCompletionError(
                    "native task state changed while completion was in progress"
                )
            try:
                completed = current.transition(
                    TaskState.DONE,
                    actor_id=actor_id,
                    rationale=reason,
                    occurred_at=completed_at,
                )
            except TaskError as error:
                raise TaskCompletionError(str(error)) from error
            expected = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
            try:
                self._ledger.record_if_head(
                    kind="task",
                    entity_id=task_id,
                    payload=task_to_payload(completed),
                    recorded_at=completed_at,
                    expected_digest=expected,
                )
                return TaskCompletionResult(
                    completed,
                    binding,
                    review_id,
                    change_set.latest.head_sha,
                    True,
                    claim_released,
                )
            except LedgerHeadChanged:
                continue
        raise TaskCompletionError(
            "ledger remained busy while completing task; retry is safe"
        )


def task_completion_result_to_payload(result: TaskCompletionResult) -> dict[str, Any]:
    return {
        "task": task_to_payload(result.task),
        "task_id": result.task.id,
        "change_set_id": result.binding.change_set_id,
        "claim_id": result.binding.claim_id,
        "review_id": result.review_id,
        "head_sha": result.head_sha,
        "completed": result.completed,
        "claim_released": result.claim_released,
    }


def _current_releasable_review(
    reviews: tuple[Mapping[str, Any], ...],
    *,
    change_set_id: str,
    head_sha: str,
) -> Mapping[str, Any]:
    if not reviews:
        raise TaskCompletionError("task completion requires a review")
    review = reviews[-1]
    try:
        decision = review["decision"]
        policy = review["policy"]
        if (
            review["change_set_id"] != change_set_id
            or decision["change_set_id"] != change_set_id
        ):
            raise ValueError("review subject mismatch")
        if review["head_sha"] != head_sha or decision["head_sha"] != head_sha:
            raise TaskCompletionError(
                "task completion requires an exact-head review"
            )
        if not review["is_releasable"] or not policy["is_satisfied"]:
            raise TaskCompletionError(
                "task completion requires a releasable review with current required evidence"
            )
        if decision["outcome"] not in {"ready", "ready_with_follow_up"}:
            raise ValueError("releasable review outcome mismatch")
        _require_text("review id", str(decision["id"]))
        created_at = datetime.fromisoformat(str(decision["created_at"]))
        _require_aware("review created_at", created_at)
    except TaskCompletionError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise TaskCompletionError("stored completion review is malformed") from error
    return review


def _proof_from_entries(
    entries: tuple[LedgerEntry, ...],
    *,
    binding: TaskWorkBinding,
    observed_at: datetime,
    require_released: bool = False,
) -> tuple[TaskIntent, ChangeBinding, Claim, Mapping[str, Any]]:
    _require_current_binding(entries, binding)
    task_entry = _latest_task_entry(entries, binding.task_id)
    current = _task_from_entry(task_entry, binding.task_id)
    change_set_entry = _latest_entry(entries, "changeset", binding.change_set_id)
    claim_entry = _latest_entry(entries, "claim", binding.claim_id)
    try:
        change_set = binding_from_payload(change_set_entry.payload)
        claim = claim_from_payload(claim_entry.payload)
    except ValueError as error:
        raise TaskCompletionError(str(error)) from error
    if current.state is TaskState.DONE:
        allowed_states = {ChangeSetState.MERGED, ChangeSetState.CLOSED}
        review_sequence = task_entry.sequence
    else:
        allowed_states = {ChangeSetState.MERGED}
        review_sequence = None
        if current.state is not TaskState.IN_PROGRESS:
            raise TaskCompletionError(
                f"native task must be in progress before completion: {current.state.value}"
            )
    if change_set.change_set.state not in allowed_states:
        raise TaskCompletionError(
            "task completion requires the bound Change Set to be merged"
        )
    if (
        claim.change_set_id != binding.change_set_id
        or claim.agent_id != binding.agent_id
        or claim.session_id != binding.session_id
    ):
        raise TaskCompletionError("bound claim identity does not match native work")
    state = _claim_state(
        claim, current.updated_at if current.state is TaskState.DONE else observed_at
    )
    if state is LockState.EXPIRED:
        raise TaskCompletionError("expired bound claim cannot authorize task completion")
    if require_released and state is not LockState.RELEASED:
        raise TaskCompletionError("bound claim must be released before task completion")
    reviews = tuple(
        entry.payload
        for entry in entries
        if entry.kind == "review"
        and entry.payload.get("change_set_id") == binding.change_set_id
        and (review_sequence is None or entry.sequence < review_sequence)
    )
    review = _current_releasable_review(
        reviews,
        change_set_id=binding.change_set_id,
        head_sha=change_set.latest.head_sha,
    )
    return current, change_set, claim, review


def _latest_task_entry(
    entries: tuple[LedgerEntry, ...], task_id: str
) -> LedgerEntry:
    latest = next(
        (
            entry
            for entry in reversed(entries)
            if entry.kind == "task" and entry.entity_id == task_id
        ),
        None,
    )
    if latest is None:
        raise TaskCompletionError(f"Task not found: {task_id}")
    return latest


def _task_from_entry(entry: LedgerEntry, task_id: str) -> TaskIntent:
    try:
        task = task_from_payload(entry.payload)
    except ValueError as error:
        raise TaskCompletionError(str(error)) from error
    if task.id != task_id:
        raise TaskCompletionError("stored Task Intent identity mismatch")
    return task


def _latest_entry(
    entries: tuple[LedgerEntry, ...], kind: str, entity_id: str
) -> LedgerEntry:
    entry = next(
        (
            value
            for value in reversed(entries)
            if value.kind == kind and value.entity_id == entity_id
        ),
        None,
    )
    if entry is None:
        raise TaskCompletionError(f"task completion requires the bound {kind}")
    return entry


def _require_current_binding(
    entries: tuple[LedgerEntry, ...], binding: TaskWorkBinding
) -> None:
    entry = _latest_entry(entries, "task_work_claim", binding.task_id)
    expected = {
        "schema_version": 1,
        "state": "completed",
        "task_id": binding.task_id,
        "change_set_id": binding.change_set_id,
        "claim_id": binding.claim_id,
        "agent_id": binding.agent_id,
        "session_id": binding.session_id,
        "base_revision": binding.base_revision,
        "created_at": binding.created_at.isoformat(),
    }
    if dict(entry.payload) != expected:
        raise TaskCompletionError("native work binding changed or is malformed")


def _claim_state(claim: Claim, observed_at: datetime) -> LockState:
    try:
        return claim.state_at(observed_at)
    except ClaimServiceError as error:
        raise TaskCompletionError(str(error)) from error


def _require_same_retry(task: TaskIntent, *, actor_id: str, reason: str) -> None:
    event = task.state_events[-1] if task.state_events else None
    if (
        event is None
        or event.state is not TaskState.DONE
        or event.actor_id != actor_id
        or event.rationale != reason
    ):
        raise TaskCompletionError(
            "native task is already done with different completion intent"
        )


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise TaskCompletionError(f"{name} must not be empty")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TaskCompletionError(f"{name} must include a timezone")
