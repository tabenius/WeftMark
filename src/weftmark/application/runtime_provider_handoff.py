"""Explicit, budgeted handoff between replaceable runtime providers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol, runtime_checkable

from weftmark.application.handoff_context import (
    HandoffMaterialization,
    TokenCounter,
    materialize_handoff_context,
)
from weftmark.application.ports.git import GitObjectId
from weftmark.application.ports.ledger import LedgerDraft, LedgerEntry, LedgerPort
from weftmark.application.ports.runtime import (
    RuntimeAdapterError,
    RuntimeChangeWorkspace,
    RuntimePort,
    RuntimeWorkerState,
    RuntimeWorkerSummary,
)
from weftmark.domain.evidence import Evidence, EvidenceState
from weftmark.domain.handoff import Handoff
from weftmark.domain.handoff_context import (
    DEFAULT_HANDOFF_CONTEXT_VARIANT,
    HandoffContextVariant,
)
from weftmark.domain.review import ReviewDecision


class RuntimeProviderHandoffError(RuntimeError):
    """Raised when a provider switch cannot preserve its declared boundaries."""


@dataclass(frozen=True, slots=True)
class RuntimeAttachmentProvenance:
    provider: str
    workspace_id: str
    change_set_id: str
    task_id: str
    base_sha: str
    worktree_path: str
    agent_id: str | None
    session_id: str | None
    worker_state: RuntimeWorkerState

    @classmethod
    def capture(
        cls,
        change_workspace: RuntimeChangeWorkspace,
        worker: RuntimeWorkerSummary,
    ) -> RuntimeAttachmentProvenance:
        if worker.provider != change_workspace.provider:
            raise RuntimeProviderHandoffError("worker provider does not match runtime workspace")
        if worker.change_set_id != change_workspace.change_set_id:
            raise RuntimeProviderHandoffError("worker belongs to another Change Set")
        if worker.task_id != change_workspace.task_id:
            raise RuntimeProviderHandoffError("worker task does not match runtime workspace")
        return cls(
            provider=change_workspace.provider,
            workspace_id=change_workspace.workspace_id,
            change_set_id=change_workspace.change_set_id,
            task_id=change_workspace.task_id,
            base_sha=str(change_workspace.base),
            worktree_path=change_workspace.worktree_path,
            agent_id=worker.agent_id,
            session_id=worker.session_id,
            worker_state=worker.state,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "workspace_id": self.workspace_id,
            "change_set_id": self.change_set_id,
            "task_id": self.task_id,
            "base_sha": self.base_sha,
            "worktree_path": self.worktree_path,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "worker_state": self.worker_state.value,
        }


@dataclass(frozen=True, slots=True)
class RuntimeProviderHandoffResult:
    switch_id: str
    handoff_id: str
    source: RuntimeAttachmentProvenance
    destination: RuntimeAttachmentProvenance
    context_variant: HandoffContextVariant
    context_tokens: int
    context_token_method: str
    context_sha256: str
    evidence_revalidation_ids: tuple[str, ...]
    requested_entry: LedgerEntry
    completed_entry: LedgerEntry


@runtime_checkable
class EvidenceRevalidationPolicy(Protocol):
    """Policy hook; selection never mutates or promotes Evidence objects."""

    def requires_revalidation(
        self,
        evidence: Evidence,
        *,
        source_provider: str,
        destination_provider: str,
    ) -> bool:
        """Return whether the destination environment should re-run this evidence."""


@dataclass(frozen=True, slots=True)
class EnvironmentBoundEvidencePolicy:
    """Conservative default for provider changes.

    Evidence carrying an environment fingerprint is selected for revalidation
    when the runtime provider changes. Unbound evidence remains valid according
    to its existing WeftMark state; this policy does not mark anything stale.
    Superseded evidence is ignored.
    """

    def requires_revalidation(
        self,
        evidence: Evidence,
        *,
        source_provider: str,
        destination_provider: str,
    ) -> bool:
        return (
            source_provider != destination_provider
            and evidence.environment is not None
            and evidence.state is not EvidenceState.SUPERSEDED
        )


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_time(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeProviderHandoffError("provider-switch timestamps must include a timezone")


def _validate_text(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RuntimeProviderHandoffError(f"{name} must not be empty")
    if "\x00" in normalized:
        raise RuntimeProviderHandoffError(f"{name} must not contain NUL")
    return normalized


def _validate_source(handoff: Handoff, workspace: RuntimeChangeWorkspace) -> None:
    if workspace.change_set_id != handoff.change_set_id:
        raise RuntimeProviderHandoffError("source runtime belongs to another Change Set")
    if workspace.task_id != handoff.change_set_id:
        raise RuntimeProviderHandoffError(
            "runtime task identity must equal the WeftMark Change Set ID"
        )
    if workspace.base != GitObjectId(handoff.base_sha):
        raise RuntimeProviderHandoffError("source runtime base differs from handoff base")


def _validate_destination(handoff: Handoff, workspace: RuntimeChangeWorkspace) -> None:
    if workspace.change_set_id != handoff.change_set_id:
        raise RuntimeProviderHandoffError("destination runtime returned another Change Set")
    if workspace.task_id != handoff.change_set_id:
        raise RuntimeProviderHandoffError(
            "destination task identity must equal the WeftMark Change Set ID"
        )
    if workspace.base != GitObjectId(handoff.base_sha):
        raise RuntimeProviderHandoffError("destination runtime changed the immutable base")


def _failure_payload(error: Exception) -> dict[str, object]:
    if isinstance(error, RuntimeAdapterError):
        return {
            "failure_type": "runtime_adapter",
            "runtime_code": error.code.value,
            "provider": error.provider,
            "operation": error.operation,
        }
    return {"failure_type": type(error).__name__}


class RuntimeProviderHandoffService:
    """Move continuation responsibility between runtime providers.

    This is intentionally not live process migration. The source worker is
    stopped at an explicit boundary, a budgeted Handoff capsule is created, and
    a fresh destination worker is started from the same immutable Change Set
    base. The ledger records request/completion/failure metadata but never the
    materialized prompt itself.
    """

    def __init__(
        self,
        ledger: LedgerPort,
        *,
        evidence_policy: EvidenceRevalidationPolicy | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._ledger = ledger
        self._evidence_policy = evidence_policy or EnvironmentBoundEvidencePolicy()
        self._clock = clock

    def switch(
        self,
        *,
        switch_id: str,
        handoff: Handoff,
        source_runtime: RuntimePort,
        source_workspace: RuntimeChangeWorkspace,
        destination_runtime: RuntimePort,
        destination_repo_path: str,
        destination_agent_id: str,
        evidence_by_id: Mapping[str, Evidence] | None = None,
        decisions_by_id: Mapping[str, ReviewDecision] | None = None,
        changed_paths: tuple[str, ...] = (),
        diff_excerpt: str | None = None,
        variant: HandoffContextVariant | str = DEFAULT_HANDOFF_CONTEXT_VARIANT,
        token_counter: TokenCounter | None = None,
        cols: int | None = None,
        rows: int | None = None,
    ) -> RuntimeProviderHandoffResult:
        switch_id = _validate_text("switch_id", switch_id)
        destination_repo_path = _validate_text("destination_repo_path", destination_repo_path)
        destination_agent_id = _validate_text("destination_agent_id", destination_agent_id)
        _validate_source(handoff, source_workspace)

        # Materialize before stopping the source. A budget failure therefore has
        # no runtime side effects.
        materialized = materialize_handoff_context(
            handoff,
            evidence_by_id=evidence_by_id,
            decisions_by_id=decisions_by_id,
            changed_paths=changed_paths,
            diff_excerpt=diff_excerpt,
            variant=variant,
            token_counter=token_counter,
        )
        context_digest = hashlib.sha256(materialized.content.encode("utf-8")).hexdigest()

        source_summary = source_runtime.worker_summary(source_workspace)
        source = RuntimeAttachmentProvenance.capture(source_workspace, source_summary)
        requested_at = self._clock()
        _validate_time(requested_at)
        requested_entry = self._ledger.append(
            LedgerDraft(
                kind="runtime.provider_switch_requested.v0",
                entity_id=switch_id,
                payload_json=_canonical_json(
                    {
                        "switch_id": switch_id,
                        "handoff_id": handoff.id,
                        "change_set_id": handoff.change_set_id,
                        "source": source.to_dict(),
                        "destination_provider": _validate_text(
                            "destination provider",
                            # Attach only after this durable request exists.
                            # RuntimePort does not expose a provider property, so
                            # the provider is verified from its returned workspace.
                            "pending",
                        ),
                        "destination_agent_id": destination_agent_id,
                        "context_variant": materialized.variant.value,
                        "context_tokens": materialized.token_count,
                        "context_token_method": materialized.token_count_method,
                        "context_sha256": context_digest,
                    }
                ),
                recorded_at=requested_at,
            )
        )

        stage = "stop_source"
        try:
            if source_summary.state in {
                RuntimeWorkerState.RUNNING,
                RuntimeWorkerState.AWAITING_INPUT,
            }:
                source_summary = source_runtime.stop_worker(source_workspace)
                if source_summary.state in {
                    RuntimeWorkerState.RUNNING,
                    RuntimeWorkerState.AWAITING_INPUT,
                }:
                    raise RuntimeProviderHandoffError(
                        "source worker remained active after explicit stop"
                    )
                source = RuntimeAttachmentProvenance.capture(
                    source_workspace, source_summary
                )

            stage = "attach_destination"
            destination_workspace = destination_runtime.attach_workspace(
                destination_repo_path
            )
            destination_change_workspace = destination_runtime.ensure_change_workspace(
                destination_workspace,
                handoff.change_set_id,
                GitObjectId(handoff.base_sha),
            )
            _validate_destination(handoff, destination_change_workspace)

            stage = "start_destination"
            destination_summary = destination_runtime.start_worker(
                destination_change_workspace,
                destination_agent_id,
                materialized.content,
                cols=cols,
                rows=rows,
            )
            destination = RuntimeAttachmentProvenance.capture(
                destination_change_workspace, destination_summary
            )

            evidence_map = evidence_by_id or {}
            revalidation_ids = tuple(
                evidence_id
                for evidence_id in handoff.evidence_ids
                if (evidence := evidence_map.get(evidence_id)) is not None
                and self._evidence_policy.requires_revalidation(
                    evidence,
                    source_provider=source.provider,
                    destination_provider=destination.provider,
                )
            )

            completed_at = self._clock()
            _validate_time(completed_at)
            completed_entry = self._ledger.append(
                LedgerDraft(
                    kind="runtime.provider_switch_completed.v0",
                    entity_id=switch_id,
                    payload_json=_canonical_json(
                        {
                            "switch_id": switch_id,
                            "handoff_id": handoff.id,
                            "change_set_id": handoff.change_set_id,
                            "source": source.to_dict(),
                            "destination": destination.to_dict(),
                            "context_variant": materialized.variant.value,
                            "context_tokens": materialized.token_count,
                            "context_token_method": materialized.token_count_method,
                            "context_sha256": context_digest,
                            "evidence_revalidation_ids": list(revalidation_ids),
                        }
                    ),
                    recorded_at=completed_at,
                )
            )
        except Exception as error:
            failed_at = self._clock()
            _validate_time(failed_at)
            self._ledger.append(
                LedgerDraft(
                    kind="runtime.provider_switch_failed.v0",
                    entity_id=switch_id,
                    payload_json=_canonical_json(
                        {
                            "switch_id": switch_id,
                            "handoff_id": handoff.id,
                            "change_set_id": handoff.change_set_id,
                            "stage": stage,
                            **_failure_payload(error),
                        }
                    ),
                    recorded_at=failed_at,
                )
            )
            if isinstance(error, RuntimeProviderHandoffError):
                raise
            raise RuntimeProviderHandoffError(
                f"provider switch failed during {stage}"
            ) from error

        return RuntimeProviderHandoffResult(
            switch_id=switch_id,
            handoff_id=handoff.id,
            source=source,
            destination=destination,
            context_variant=materialized.variant,
            context_tokens=materialized.token_count,
            context_token_method=materialized.token_count_method,
            context_sha256=context_digest,
            evidence_revalidation_ids=revalidation_ids,
            requested_entry=requested_entry,
            completed_entry=completed_entry,
        )
