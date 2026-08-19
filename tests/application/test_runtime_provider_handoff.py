from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from weftmark.application.ports.git import GitObjectId
from weftmark.application.ports.ledger import (
    LEDGER_GENESIS_DIGEST,
    LedgerDraft,
    LedgerEntry,
    LedgerHeadChanged,
)
from weftmark.application.ports.runtime import (
    RuntimeChangeWorkspace,
    RuntimeChanges,
    RuntimeChangesMode,
    RuntimeFileChange,
    RuntimePort,
    RuntimeWorkerState,
    RuntimeWorkerSummary,
    RuntimeWorkspace,
)
from weftmark.application.runtime_provider_handoff import (
    RuntimeProviderHandoffError,
    RuntimeProviderHandoffService,
)
from weftmark.domain.evidence import (
    Environment,
    Evidence,
    EvidenceKind,
    EvidenceProducer,
    EvidenceState,
    EvidenceSubject,
    ProducerKind,
    SubjectKind,
)
from weftmark.domain.handoff import Handoff
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)
BASE = GitObjectId("a" * 40)


def handoff(**changes: object) -> Handoff:
    values = {
        "id": "handoff-1",
        "task_id": "chg-1",
        "change_set_id": "chg-1",
        "goal": "Continue runtime provider integration",
        "repository_id": "git:/repo/.git",
        "base_sha": str(BASE),
        "head_sha": "b" * 40,
        "branch": "feature/provider-switch",
        "worktree": "/repo",
        "source_observation_id": "chg-1:git:7",
        "scopes": (Scope.contract("runtime-provider-v0"),),
        "evidence_ids": (),
        "decision_ids": (),
        "known_failures": (),
        "next_action": "Continue with the destination worker",
        "created_by": "worker-source",
        "created_at": NOW,
        "intended_receiver_id": "worker-destination",
    }
    values.update(changes)
    return Handoff(**values)  # type: ignore[arg-type]


class MemoryLedger:
    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def append(self, draft: LedgerDraft) -> LedgerEntry:
        previous = self._entries[-1].digest if self._entries else LEDGER_GENESIS_DIGEST
        sequence = len(self._entries) + 1
        digest = hashlib.sha256(
            f"{sequence}:{previous}:{draft.kind}:{draft.entity_id}:{draft.payload_json}".encode()
        ).hexdigest()
        entry = LedgerEntry(
            sequence=sequence,
            previous_digest=previous,
            digest=digest,
            draft=draft,
        )
        self._entries.append(entry)
        return entry

    def append_if_head(self, draft: LedgerDraft, *, expected_digest: str) -> LedgerEntry:
        current = self._entries[-1].digest if self._entries else LEDGER_GENESIS_DIGEST
        if current != expected_digest:
            raise LedgerHeadChanged
        return self.append(draft)

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)


@dataclass
class FakeRuntime(RuntimePort):
    provider: str
    workspace_id: str
    log: list[str]
    initial_state: RuntimeWorkerState = RuntimeWorkerState.RUNNING
    start_state: RuntimeWorkerState = RuntimeWorkerState.RUNNING
    returned_provider: str | None = None
    prompt: str | None = None
    stop_calls: int = 0

    def _provider(self) -> str:
        return self.returned_provider or self.provider

    def attach_workspace(self, repo_path: str) -> RuntimeWorkspace:
        self.log.append(f"{self.provider}:attach")
        return RuntimeWorkspace(
            provider=self._provider(),
            workspace_id=self.workspace_id,
            repo_path=repo_path,
        )

    def ensure_change_workspace(
        self,
        workspace: RuntimeWorkspace,
        change_set_id: str,
        base: GitObjectId,
    ) -> RuntimeChangeWorkspace:
        self.log.append(f"{self.provider}:ensure")
        return RuntimeChangeWorkspace(
            provider=workspace.provider,
            workspace_id=workspace.workspace_id,
            change_set_id=change_set_id,
            task_id=change_set_id,
            base=base,
            worktree_path=f"/runtime/{self.provider}/{change_set_id}",
        )

    def get_change_workspace(
        self,
        workspace: RuntimeWorkspace,
        change_set_id: str,
        base: GitObjectId,
    ) -> RuntimeChangeWorkspace | None:
        return self.ensure_change_workspace(workspace, change_set_id, base)

    def start_worker(
        self,
        change_workspace: RuntimeChangeWorkspace,
        agent_id: str,
        prompt: str,
        *,
        cols: int | None = None,
        rows: int | None = None,
    ) -> RuntimeWorkerSummary:
        self.log.append(f"{self.provider}:start")
        self.prompt = prompt
        return RuntimeWorkerSummary(
            provider=change_workspace.provider,
            change_set_id=change_workspace.change_set_id,
            task_id=change_workspace.task_id,
            state=self.start_state,
            agent_id=agent_id,
            session_id=f"{self.provider}-session-new",
            started_at=NOW,
            updated_at=NOW,
        )

    def stop_worker(self, change_workspace: RuntimeChangeWorkspace) -> RuntimeWorkerSummary:
        self.log.append(f"{self.provider}:stop")
        self.stop_calls += 1
        return RuntimeWorkerSummary(
            provider=change_workspace.provider,
            change_set_id=change_workspace.change_set_id,
            task_id=change_workspace.task_id,
            state=RuntimeWorkerState.EXITED,
            agent_id="codex",
            session_id=f"{self.provider}-session-old",
            exit_code=0,
            started_at=NOW - timedelta(minutes=5),
            updated_at=NOW,
        )

    def send_worker_input(
        self, change_workspace: RuntimeChangeWorkspace, data: str
    ) -> RuntimeWorkerSummary:
        raise NotImplementedError

    def worker_summary(self, change_workspace: RuntimeChangeWorkspace) -> RuntimeWorkerSummary:
        self.log.append(f"{self.provider}:summary")
        return RuntimeWorkerSummary(
            provider=change_workspace.provider,
            change_set_id=change_workspace.change_set_id,
            task_id=change_workspace.task_id,
            state=self.initial_state,
            agent_id="codex",
            session_id=f"{self.provider}-session-old",
            started_at=NOW - timedelta(minutes=5),
            updated_at=NOW,
        )

    def changes(
        self,
        change_workspace: RuntimeChangeWorkspace,
        mode: RuntimeChangesMode = RuntimeChangesMode.WORKING_COPY,
    ) -> RuntimeChanges:
        return RuntimeChanges(
            provider=change_workspace.provider,
            change_set_id=change_workspace.change_set_id,
            mode=mode,
            files=(),
            base=change_workspace.base,
            head=None,
        )

    def cleanup_change_workspace(self, change_workspace: RuntimeChangeWorkspace) -> None:
        self.log.append(f"{self.provider}:cleanup")


def source_workspace(provider: str = "cline-kanban") -> RuntimeChangeWorkspace:
    return RuntimeChangeWorkspace(
        provider=provider,
        workspace_id="source-workspace",
        change_set_id="chg-1",
        task_id="chg-1",
        base=BASE,
        worktree_path="/runtime/source/chg-1",
    )


@dataclass
class StepClock:
    current: datetime = NOW

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def environment_evidence(id: str = "ev-env") -> Evidence:
    record = Evidence.declare(
        id=id,
        kind=EvidenceKind.TEST,
        producer=EvidenceProducer(ProducerKind.CI, "ci"),
        subject=EvidenceSubject(SubjectKind.CHANGE_SET, "chg-1"),
        bound_commit_sha="b" * 40,
        environment=Environment("env-source"),
        at=NOW - timedelta(minutes=3),
    )
    return record.start(at=NOW - timedelta(minutes=2)).pass_(
        at=NOW - timedelta(minutes=1)
    )


def test_switch_stops_source_then_starts_destination_with_budgeted_context() -> None:
    log: list[str] = []
    ledger = MemoryLedger()
    source = FakeRuntime("cline-kanban", "source-workspace", log)
    destination = FakeRuntime("openhands", "destination-workspace", log)
    service = RuntimeProviderHandoffService(ledger, clock=StepClock())

    result = service.switch(
        switch_id="switch-1",
        handoff=handoff(),
        source_runtime=source,
        source_workspace=source_workspace(),
        destination_runtime=destination,
        destination_provider_id="openhands",
        destination_repo_path="/repo",
        destination_agent_id="claude-code",
        variant="standard",
    )

    assert source.stop_calls == 1
    assert log.index("cline-kanban:stop") < log.index("openhands:start")
    assert destination.prompt is not None
    assert "Continue runtime provider integration" in destination.prompt
    assert result.context_variant.value == "standard"
    assert result.destination.provider == "openhands"
    assert result.destination.agent_id == "claude-code"
    assert result.source.worker_state is RuntimeWorkerState.EXITED
    assert result.destination.worker_state is RuntimeWorkerState.RUNNING

    entries = ledger.entries()
    assert [entry.kind for entry in entries] == [
        "runtime.provider_switch_requested.v0",
        "runtime.provider_switch_completed.v0",
    ]
    assert entries[0].payload["destination_provider"] == "openhands"
    assert "content" not in entries[0].payload
    assert "content" not in entries[1].payload
    assert result.context_sha256 == hashlib.sha256(destination.prompt.encode()).hexdigest()


def test_budget_failure_has_no_runtime_or_ledger_side_effects() -> None:
    log: list[str] = []
    ledger = MemoryLedger()
    source = FakeRuntime("cline-kanban", "source-workspace", log)
    destination = FakeRuntime("openhands", "destination-workspace", log)
    service = RuntimeProviderHandoffService(ledger, clock=StepClock())
    huge_failure = "x" * 10_000

    with pytest.raises(ValueError, match="mandatory handoff context"):
        service.switch(
            switch_id="switch-budget-fail",
            handoff=handoff(known_failures=(huge_failure,)),
            source_runtime=source,
            source_workspace=source_workspace(),
            destination_runtime=destination,
            destination_provider_id="openhands",
            destination_repo_path="/repo",
            destination_agent_id="claude-code",
            variant="compact",
        )

    assert log == []
    assert ledger.entries() == ()


def test_environment_bound_evidence_is_selected_not_mutated_for_revalidation() -> None:
    log: list[str] = []
    ledger = MemoryLedger()
    source = FakeRuntime("cline-kanban", "source-workspace", log)
    destination = FakeRuntime("openhands", "destination-workspace", log)
    record = environment_evidence()
    record_before = record
    service = RuntimeProviderHandoffService(ledger, clock=StepClock())

    result = service.switch(
        switch_id="switch-env",
        handoff=handoff(evidence_ids=(record.id,)),
        source_runtime=source,
        source_workspace=source_workspace(),
        destination_runtime=destination,
        destination_provider_id="openhands",
        destination_repo_path="/repo",
        destination_agent_id="claude-code",
        evidence_by_id={record.id: record},
    )

    assert result.evidence_revalidation_ids == (record.id,)
    assert record is record_before
    assert record.state is EvidenceState.PASSED
    assert ledger.entries()[-1].payload["evidence_revalidation_ids"] == [record.id]


def test_same_provider_switch_does_not_require_environment_revalidation_by_default() -> None:
    log: list[str] = []
    ledger = MemoryLedger()
    source = FakeRuntime("cline-kanban", "source-workspace", log)
    destination = FakeRuntime("cline-kanban", "destination-workspace", log)
    record = environment_evidence()

    result = RuntimeProviderHandoffService(ledger, clock=StepClock()).switch(
        switch_id="switch-agent-only",
        handoff=handoff(evidence_ids=(record.id,)),
        source_runtime=source,
        source_workspace=source_workspace(),
        destination_runtime=destination,
        destination_provider_id="cline-kanban",
        destination_repo_path="/repo",
        destination_agent_id="claude-code",
        evidence_by_id={record.id: record},
    )

    assert result.evidence_revalidation_ids == ()


def test_destination_provider_mismatch_is_audited_as_failed_switch() -> None:
    log: list[str] = []
    ledger = MemoryLedger()
    source = FakeRuntime("cline-kanban", "source-workspace", log)
    destination = FakeRuntime(
        "openhands",
        "destination-workspace",
        log,
        returned_provider="unexpected-provider",
    )

    with pytest.raises(RuntimeProviderHandoffError, match="provider"):
        RuntimeProviderHandoffService(ledger, clock=StepClock()).switch(
            switch_id="switch-mismatch",
            handoff=handoff(),
            source_runtime=source,
            source_workspace=source_workspace(),
            destination_runtime=destination,
            destination_provider_id="openhands",
            destination_repo_path="/repo",
            destination_agent_id="claude-code",
        )

    assert [entry.kind for entry in ledger.entries()] == [
        "runtime.provider_switch_requested.v0",
        "runtime.provider_switch_failed.v0",
    ]
    assert ledger.entries()[-1].payload["stage"] == "attach_destination"


def test_destination_must_become_active_or_switch_fails() -> None:
    log: list[str] = []
    ledger = MemoryLedger()
    source = FakeRuntime("cline-kanban", "source-workspace", log)
    destination = FakeRuntime(
        "openhands",
        "destination-workspace",
        log,
        start_state=RuntimeWorkerState.FAILED,
    )

    with pytest.raises(RuntimeProviderHandoffError, match="active state"):
        RuntimeProviderHandoffService(ledger, clock=StepClock()).switch(
            switch_id="switch-start-fail",
            handoff=handoff(),
            source_runtime=source,
            source_workspace=source_workspace(),
            destination_runtime=destination,
            destination_provider_id="openhands",
            destination_repo_path="/repo",
            destination_agent_id="claude-code",
        )

    assert ledger.entries()[-1].kind == "runtime.provider_switch_failed.v0"
    assert ledger.entries()[-1].payload["stage"] == "start_destination"
