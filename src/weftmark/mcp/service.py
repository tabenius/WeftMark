"""Provider-neutral application facade for the optional MCP server.

This module intentionally contains no MCP SDK imports. It can be tested as a
normal application adapter, while ``weftmark.mcp.server`` maps these operations
to the protocol when the optional ``mcp`` dependency is installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.claims import ClaimService, claim_to_payload
from weftmark.application.control import ControlService
from weftmark.application.evidence_runner import CommandEvidenceRequest
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import (
    LocalWorkflowService,
    evidence_result_to_payload,
    scope_audit_to_payload,
)
from weftmark.application.status import StatusService, status_to_payload
from weftmark.application.task_claims import TaskClaimService
from weftmark.application.task_planning import (
    TaskPlanningService,
    task_selection_to_payload,
)
from weftmark.application.tasks import TaskService, task_to_payload
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceKind, EvidenceProducer, ProducerKind
from weftmark.domain.scope import Scope
from weftmark.domain.task import TaskState


class McpSurfaceError(ValueError):
    """Raised when an MCP-facing application request is invalid."""


class McpPermissionError(McpSurfaceError):
    """Raised when a write capability was not granted to this MCP process."""


class McpWriteCapability(StrEnum):
    CLAIM = "claim"
    RELEASE = "release"
    HANDOFF = "handoff"
    SCOPE_AUDIT = "scope-audit"
    EVIDENCE_EXEC = "evidence-exec"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class McpToolService:
    tasks: TaskService
    planning: TaskPlanningService
    status: StatusService
    claims: ClaimService
    workflow: LocalWorkflowService
    control: ControlService
    write_capabilities: frozenset[McpWriteCapability] = frozenset()
    clock: Callable[[], datetime] = _now

    @classmethod
    def from_local(
        cls,
        repo: str,
        *,
        ledger_path: str | Path | None = None,
        write_capabilities: Iterable[McpWriteCapability] = (),
        clock: Callable[[], datetime] = _now,
    ) -> McpToolService:
        git = LocalGit(repo)
        repository = git.repository()
        if ledger_path is None:
            if not repository.id.startswith("git:"):
                raise McpSurfaceError("local repository identity cannot select a ledger")
            ledger_path = (
                Path(repository.id.removeprefix("git:"))
                / "weftmark"
                / "ledger.jsonl"
            )
        ledger = LedgerService(JsonlLedger(Path(ledger_path)))
        workspace = WorkspaceService(git, ledger)
        claims = ClaimService(workspace, ledger)
        tasks = TaskService(ledger)
        planning = TaskPlanningService(tasks)
        task_claims = TaskClaimService(planning, tasks, workspace, claims, ledger)
        workflow = LocalWorkflowService(
            workspace,
            ledger,
            EvidenceProducer(ProducerKind.WORKER, "weftmark-mcp"),
        )
        status = StatusService(workspace, claims, workflow)
        control = ControlService(task_claims, claims, workflow, ledger)
        return cls(
            tasks,
            planning,
            status,
            claims,
            workflow,
            control,
            frozenset(write_capabilities),
            clock,
        )

    # ---- Read-only tools -------------------------------------------------

    def workspace_status(self) -> dict[str, Any]:
        """Return current durable Change Set, evidence, review and claim status."""
        return status_to_payload(self.status.summarize(observed_at=self.clock()))

    def task_list(self, *, state: str | None = None) -> dict[str, Any]:
        """List native task intent, optionally filtered by exact task state."""
        selected_state: TaskState | None = None
        if state is not None:
            try:
                selected_state = TaskState(state)
            except ValueError as error:
                raise McpSurfaceError(f"unknown task state: {state}") from error
        values = tuple(
            task
            for task in self.tasks.list()
            if selected_state is None or task.state is selected_state
        )
        return {
            "count": len(values),
            "tasks": [task_to_payload(task) for task in values],
        }

    def task_next(self, *, limit: int = 1) -> dict[str, Any]:
        """Return advisory next eligible native tasks without claiming them."""
        return task_selection_to_payload(self.planning.next(limit=limit))

    def task_eligibility(self, task_id: str) -> dict[str, Any]:
        """Explain whether one native task is currently eligible to claim."""
        value = self.planning.eligibility(task_id)
        return {
            "task": task_to_payload(value.task),
            "eligible": value.eligible,
            "reasons": list(value.reasons),
            "authority": "advisory only; no claim or Change Set was created",
        }

    def change_show(self, change_set_id: str) -> dict[str, Any]:
        """Show one Change Set through the same status read model as CLI/web."""
        payload = self.workspace_status()
        matches = tuple(
            value
            for value in payload["change_sets"]
            if value["id"] == change_set_id
        )
        if len(matches) != 1:
            raise McpSurfaceError(f"Change Set not found: {change_set_id}")
        return {
            "generated_at": payload["generated_at"],
            "change_set": matches[0],
        }

    def evidence_list(self, *, change_set_id: str | None = None) -> dict[str, Any]:
        """List durable command evidence, optionally for one Change Set."""
        values = self.workflow.list_evidence(change_set_id=change_set_id)
        return {
            "count": len(values),
            "evidence": [evidence_result_to_payload(value) for value in values],
        }

    def review_list(self, *, change_set_id: str | None = None) -> dict[str, Any]:
        """List durable review summaries without creating a new review."""
        values = self.workflow.list_reviews(change_set_id=change_set_id)
        return {"count": len(values), "reviews": [dict(value) for value in values]}

    def handoff_list(self, *, change_set_id: str | None = None) -> dict[str, Any]:
        """List durable handoffs without expanding chat or terminal history."""
        values = self.workflow.list_handoffs(change_set_id=change_set_id)
        return {"count": len(values), "handoffs": [value.to_dict() for value in values]}

    # ---- Capability-gated write tools ----------------------------------

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
        lease_seconds: int = 900,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Claim an eligible native task through the same ControlService as HTTP."""
        self._require(McpWriteCapability.CLAIM)
        if dry_run:
            eligibility = self.planning.eligibility(task_id)
            return {
                "dry_run": True,
                "eligible": eligibility.eligible,
                "reasons": list(eligibility.reasons),
                "task": task_to_payload(eligibility.task),
                "proposed": {
                    "change_set_id": change_set_id,
                    "claim_id": claim_id,
                    "base_revision": base_revision,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "lease_seconds": lease_seconds,
                },
            }
        result = self.control.claim_task(
            task_id,
            idempotency_key=idempotency_key,
            change_set_id=change_set_id,
            claim_id=claim_id,
            base_revision=base_revision,
            agent_id=agent_id,
            session_id=session_id,
            lease_seconds=lease_seconds,
            requested_at=self.clock(),
        )
        return result.to_dict()

    def release_claim(
        self,
        claim_id: str,
        *,
        idempotency_key: str,
        agent_id: str,
        session_id: str,
        reason: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Release an owned semantic claim through the shared control boundary."""
        self._require(McpWriteCapability.RELEASE)
        if dry_run:
            claim = self.claims.get(claim_id)
            if claim is None:
                raise McpSurfaceError(f"Claim not found: {claim_id}")
            return {
                "dry_run": True,
                "claim": claim_to_payload(claim, observed_at=self.clock()),
                "requested_owner": {
                    "agent_id": agent_id,
                    "session_id": session_id,
                },
                "reason": reason,
            }
        result = self.control.release_claim(
            claim_id,
            idempotency_key=idempotency_key,
            agent_id=agent_id,
            session_id=session_id,
            reason=reason,
            requested_at=self.clock(),
        )
        return result.to_dict()

    def create_handoff(
        self,
        change_set_id: str,
        *,
        idempotency_key: str,
        handoff_id: str,
        task_id: str,
        next_action: str,
        created_by: str,
        intended_receiver_id: str | None = None,
        known_failures: tuple[str, ...] = (),
        supersedes_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a clean-head handoff without replaying prior agent chat."""
        self._require(McpWriteCapability.HANDOFF)
        result = self.control.create_handoff(
            change_set_id,
            idempotency_key=idempotency_key,
            handoff_id=handoff_id,
            task_id=task_id,
            next_action=next_action,
            created_by=created_by,
            intended_receiver_id=intended_receiver_id,
            known_failures=known_failures,
            supersedes_id=supersedes_id,
            requested_at=self.clock(),
        )
        return result.to_dict()

    def audit_scope(
        self,
        change_set_id: str,
        *,
        semantic_changes: tuple[str, ...] = (),
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Record an audit of actual files plus caller-declared semantic changes."""
        self._require(McpWriteCapability.SCOPE_AUDIT)
        parsed = tuple(Scope.parse(value) for value in semantic_changes)
        if dry_run:
            return {
                "dry_run": True,
                "change_set_id": change_set_id,
                "semantic_changes": [value.to_dict() for value in parsed],
                "note": "no Git refresh or scope-audit record was written",
            }
        result = self.workflow.audit_scope(
            change_set_id,
            semantic_changes=parsed,
            audited_at=self.clock(),
        )
        return scope_audit_to_payload(result, change_set_id=change_set_id)

    def run_evidence(
        self,
        change_set_id: str,
        *,
        evidence_id: str,
        kind: str,
        argv: tuple[str, ...],
        cwd: str,
        timeout_seconds: float = 300.0,
        redact_argv_indexes: tuple[int, ...] = (),
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Run command evidence only when the high-risk evidence capability is granted.

        V0 deliberately does not accept environment-variable injection. Full
        source/terminal/chat context is unrelated to command evidence and is not
        transferred by this tool.
        """
        self._require(McpWriteCapability.EVIDENCE_EXEC)
        try:
            evidence_kind = EvidenceKind(kind)
        except ValueError as error:
            raise McpSurfaceError(f"unknown evidence kind: {kind}") from error
        request = CommandEvidenceRequest(
            id=evidence_id,
            kind=evidence_kind,
            argv=argv,
            cwd=cwd,
            redact_argv_indexes=frozenset(redact_argv_indexes),
            timeout_seconds=timeout_seconds,
        )
        if dry_run:
            return {
                "dry_run": True,
                "change_set_id": change_set_id,
                "evidence_id": evidence_id,
                "kind": evidence_kind.value,
                "argv": list(argv),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "redact_argv_indexes": sorted(request.redact_argv_indexes),
                "warning": "evidence-exec can run arbitrary local commands when dry_run=false",
            }
        result = self.workflow.run_evidence(
            change_set_id,
            request,
            observed_at=self.clock(),
        )
        return evidence_result_to_payload(result)

    def _require(self, capability: McpWriteCapability) -> None:
        if capability not in self.write_capabilities:
            raise McpPermissionError(
                f"MCP write capability not granted: {capability.value}"
            )
