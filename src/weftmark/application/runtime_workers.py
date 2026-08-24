"""Claim-gated, ledger-recorded workers over a replaceable RuntimePort."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from weftmark.application.claims import ClaimService, ClaimServiceError
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.git import GitObjectId
from weftmark.application.ports.ledger import LEDGER_GENESIS_DIGEST, LedgerHeadChanged
from weftmark.application.ports.runtime import (
    RuntimeAdapterError,
    RuntimeChangeWorkspace,
    RuntimePort,
    RuntimeWorkerState,
)
from weftmark.application.runtime_registry import (
    RuntimeProviderRegistry,
    RuntimeRegistryError,
)
from weftmark.application.task_claims import TaskClaimService, TaskWorkBinding
from weftmark.domain.lock import LockState


class RuntimeWorkerError(ValueError):
    """Raised when runtime authority or durable state is invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeWorkerRecord:
    task_id: str
    change_set_id: str
    provider: str
    state: RuntimeWorkerState
    updated_at: datetime
    agent_id: str | None = None
    session_id: str | None = None
    pid: int | None = None
    exit_code: int | None = None


class RuntimeWorkerService:
    def __init__(
        self,
        task_claims: TaskClaimService,
        claims: ClaimService,
        registry: RuntimeProviderRegistry,
        adapter_factory: Callable[[str], RuntimePort],
        repo_path: str,
        ledger: LedgerService,
        change_set_base: Callable[[str], GitObjectId],
    ) -> None:
        self._task_claims = task_claims
        self._claims = claims
        self._registry = registry
        self._adapter_factory = adapter_factory
        self._repo_path = repo_path
        self._ledger = ledger
        self._change_set_base = change_set_base

    def start(
        self,
        task_id: str,
        *,
        provider: str,
        prompt: str,
        started_at: datetime,
    ) -> RuntimeWorkerRecord:
        _require_aware(started_at)
        if not prompt.strip():
            raise RuntimeWorkerError("runtime prompt must not be empty")
        binding = self._require_active_claim(task_id, at=started_at)
        self._require_provider(provider)
        existing = self._latest(task_id)
        if existing is not None and existing.state not in {
            RuntimeWorkerState.EXITED,
            RuntimeWorkerState.FAILED,
        }:
            if existing.provider != provider:
                raise RuntimeWorkerError("active runtime session uses a different provider")
            return self._observe(binding, existing.provider, at=started_at)
        adapter, change_workspace = self._ensure_workspace(binding, provider)
        try:
            summary = adapter.start_worker(
                change_workspace, binding.agent_id, prompt
            )
        except RuntimeAdapterError as error:
            raise _adapter_error(error) from error
        return self._record(_from_summary(summary, updated_at=started_at))

    def send_input(
        self, task_id: str, data: str, *, observed_at: datetime
    ) -> RuntimeWorkerRecord:
        _require_aware(observed_at)
        if not data.strip():
            raise RuntimeWorkerError("runtime input must not be empty")
        binding = self._require_active_claim(task_id, at=observed_at)
        current = self._require_started(task_id)
        if current.state in {RuntimeWorkerState.EXITED, RuntimeWorkerState.FAILED}:
            raise RuntimeWorkerError("runtime worker is not accepting input")
        adapter, workspace = self._existing_workspace(binding, current.provider)
        try:
            summary = adapter.send_worker_input(workspace, data)
        except RuntimeAdapterError as error:
            raise _adapter_error(error) from error
        return self._record(_from_summary(summary, updated_at=observed_at))

    def status(self, task_id: str, *, observed_at: datetime) -> RuntimeWorkerRecord:
        _require_aware(observed_at)
        binding = self._require_binding(task_id)
        current = self._require_started(task_id)
        if current.state in {RuntimeWorkerState.EXITED, RuntimeWorkerState.FAILED}:
            return current
        return self._observe(binding, current.provider, at=observed_at)

    def stop(self, task_id: str, *, observed_at: datetime) -> RuntimeWorkerRecord:
        """Stop is allowed after lease expiry as a containment operation."""

        _require_aware(observed_at)
        binding = self._require_binding(task_id)
        current = self._require_started(task_id)
        if current.state is RuntimeWorkerState.EXITED:
            return current
        adapter, workspace = self._existing_workspace(binding, current.provider)
        try:
            summary = adapter.stop_worker(workspace)
        except RuntimeAdapterError as error:
            raise _adapter_error(error) from error
        return self._record(_from_summary(summary, updated_at=observed_at))

    def _observe(
        self, binding: TaskWorkBinding, provider: str, *, at: datetime
    ) -> RuntimeWorkerRecord:
        adapter, workspace = self._existing_workspace(binding, provider)
        try:
            summary = adapter.worker_summary(workspace)
        except RuntimeAdapterError as error:
            raise _adapter_error(error) from error
        if summary.state is RuntimeWorkerState.UNKNOWN:
            current = self._require_started(binding.task_id)
            return current
        return self._record(_from_summary(summary, updated_at=at))

    def _require_binding(self, task_id: str) -> TaskWorkBinding:
        binding = self._task_claims.get(task_id)
        if binding is None or not binding.completed:
            raise RuntimeWorkerError(f"no completed native claim for task: {task_id}")
        return binding

    def _require_active_claim(self, task_id: str, *, at: datetime) -> TaskWorkBinding:
        binding = self._require_binding(task_id)
        try:
            claim = self._claims.get(binding.claim_id)
            if claim is None or claim.state_at(at) is not LockState.ACTIVE:
                raise RuntimeWorkerError(f"no active native claim for task: {task_id}")
        except ClaimServiceError as error:
            raise RuntimeWorkerError("native claim state is inconsistent") from error
        if (
            claim.change_set_id != binding.change_set_id
            or claim.agent_id != binding.agent_id
            or claim.session_id != binding.session_id
        ):
            raise RuntimeWorkerError("native claim identity does not match task binding")
        return binding

    def _require_started(self, task_id: str) -> RuntimeWorkerRecord:
        current = self._latest(task_id)
        if current is None:
            raise RuntimeWorkerError(f"no runtime worker recorded for task: {task_id}")
        self._require_provider(current.provider)
        return current

    def _require_provider(self, provider: str) -> None:
        try:
            self._registry.get(provider)
        except RuntimeRegistryError as error:
            raise RuntimeWorkerError(str(error)) from error

    def _ensure_workspace(
        self, binding: TaskWorkBinding, provider: str
    ) -> tuple[RuntimePort, RuntimeChangeWorkspace]:
        adapter = self._adapter_factory(provider)
        workspace = adapter.attach_workspace(self._repo_path)
        try:
            change = adapter.ensure_change_workspace(
                workspace,
                binding.change_set_id,
                self._change_set_base(binding.change_set_id),
            )
        except RuntimeAdapterError as error:
            raise _adapter_error(error) from error
        return adapter, _with_task_id(change, binding.task_id)

    def _existing_workspace(
        self, binding: TaskWorkBinding, provider: str
    ) -> tuple[RuntimePort, RuntimeChangeWorkspace]:
        adapter = self._adapter_factory(provider)
        workspace = adapter.attach_workspace(self._repo_path)
        try:
            change = adapter.get_change_workspace(
                workspace,
                binding.change_set_id,
                self._change_set_base(binding.change_set_id),
            )
        except RuntimeAdapterError as error:
            raise _adapter_error(error) from error
        if change is None:
            raise RuntimeWorkerError(
                f"runtime worktree not found for change set: {binding.change_set_id}"
            )
        return adapter, _with_task_id(change, binding.task_id)

    def _record(self, record: RuntimeWorkerRecord) -> RuntimeWorkerRecord:
        payload = runtime_worker_record_to_payload(record)
        for _ in range(8):
            entries = self._ledger.snapshot()
            expected = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
            try:
                self._ledger.record_if_head(
                    kind="runtime_worker_session",
                    entity_id=record.task_id,
                    payload=payload,
                    recorded_at=record.updated_at,
                    expected_digest=expected,
                )
                return record
            except LedgerHeadChanged:
                continue
        raise RuntimeWorkerError("ledger remained busy while recording runtime worker")

    def _latest(self, task_id: str) -> RuntimeWorkerRecord | None:
        entry = self._ledger.latest(kind="runtime_worker_session", entity_id=task_id)
        return None if entry is None else _record_from_payload(
            entry.payload, entry.draft.recorded_at
        )


def runtime_worker_record_to_payload(record: RuntimeWorkerRecord) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": record.task_id,
        "change_set_id": record.change_set_id,
        "provider": record.provider,
        "state": record.state.value,
        "updated_at": record.updated_at.isoformat(),
        "agent_id": record.agent_id,
        "session_id": record.session_id,
        "pid": record.pid,
        "exit_code": record.exit_code,
        "authority": "operational_observation_only",
    }


def _record_from_payload(payload: Mapping[str, Any], recorded_at: datetime) -> RuntimeWorkerRecord:
    try:
        if payload["schema_version"] != 1:
            raise ValueError("unsupported schema")
        if payload.get("authority") != "operational_observation_only":
            raise ValueError("invalid authority marker")
        updated_at = datetime.fromisoformat(str(payload["updated_at"]))
        _require_aware(updated_at)
        if updated_at != recorded_at:
            raise ValueError("record time mismatch")
        pid = payload.get("pid")
        exit_code = payload.get("exit_code")
        return RuntimeWorkerRecord(
            task_id=_text(payload, "task_id"),
            change_set_id=_text(payload, "change_set_id"),
            provider=_text(payload, "provider"),
            state=RuntimeWorkerState(str(payload["state"])),
            updated_at=updated_at,
            agent_id=_optional_text(payload.get("agent_id")),
            session_id=_optional_text(payload.get("session_id")),
            pid=None if pid is None else int(pid),
            exit_code=None if exit_code is None else int(exit_code),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeWorkerError("stored runtime worker record is malformed") from error


def _from_summary(summary: Any, *, updated_at: datetime) -> RuntimeWorkerRecord:
    return RuntimeWorkerRecord(
        summary.task_id,
        summary.change_set_id,
        summary.provider,
        summary.state,
        updated_at,
        summary.agent_id,
        summary.session_id,
        summary.pid,
        summary.exit_code,
    )


def _with_task_id(value: RuntimeChangeWorkspace, task_id: str) -> RuntimeChangeWorkspace:
    return RuntimeChangeWorkspace(
        value.provider,
        value.workspace_id,
        value.change_set_id,
        task_id,
        value.base,
        value.worktree_path,
    )


def _adapter_error(error: RuntimeAdapterError) -> RuntimeWorkerError:
    return RuntimeWorkerError(f"{error.code.value}: {error.detail}")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeWorkerError("runtime observation time must include a timezone")


def _text(value: Mapping[str, Any], name: str) -> str:
    text = str(value[name]).strip()
    if not text:
        raise ValueError(f"empty {name}")
    return text


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value).strip() or None
