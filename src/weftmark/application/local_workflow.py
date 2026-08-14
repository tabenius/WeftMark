"""Durable local orchestration for scope audits and command evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from weftmark.application.evidence_runner import (
    CommandEvidenceRequest,
    CommandEvidenceResult,
    LocalEvidenceRunner,
)
from weftmark.application.ledger import LedgerService
from weftmark.application.scope_audit import ScopeAuditResult, ScopeAuditService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import (
    ArtifactReference,
    Command,
    Environment,
    Evidence,
    EvidenceKind,
    EvidenceProducer,
    EvidenceState,
    EvidenceSubject,
    ProducerKind,
    StaleReason,
    SubjectKind,
)
from weftmark.domain.scope import Scope


class LocalWorkflowError(ValueError):
    """Raised when a durable local workflow request is invalid."""


class LocalWorkflowService:
    def __init__(
        self,
        workspace: WorkspaceService,
        ledger: LedgerService,
        producer: EvidenceProducer,
    ) -> None:
        self._workspace = workspace
        self._ledger = ledger
        self._runner = LocalEvidenceRunner(producer)

    def audit_scope(
        self,
        change_set_id: str,
        *,
        semantic_changes: tuple[Scope, ...],
        audited_at: datetime,
    ) -> ScopeAuditResult:
        binding = self._workspace.refresh_change_set(
            change_set_id, observed_at=audited_at
        )
        declared = tuple(Scope.parse(value) for value in binding.change_set.scopes)
        result = ScopeAuditService().audit(
            binding,
            declared_scopes=declared,
            semantic_changes=semantic_changes,
            audited_at=audited_at,
        )
        self._ledger.record(
            kind="scope_audit",
            entity_id=result.observation_id,
            payload=scope_audit_to_payload(result, change_set_id=change_set_id),
            recorded_at=audited_at,
        )
        return result

    def run_evidence(
        self,
        change_set_id: str,
        request: CommandEvidenceRequest,
        *,
        observed_at: datetime,
    ) -> CommandEvidenceResult:
        if self._ledger.latest(kind="evidence", entity_id=request.id) is not None:
            raise LocalWorkflowError(f"Evidence already exists: {request.id}")
        binding = self._workspace.refresh_change_set(
            change_set_id, observed_at=observed_at
        )
        result = self._runner.run(binding, request)
        self._ledger.record(
            kind="evidence",
            entity_id=request.id,
            payload=evidence_result_to_payload(result),
            recorded_at=result.evidence.updated_at,
        )
        return result

    def get_evidence(self, id: str) -> CommandEvidenceResult | None:
        entry = self._ledger.latest(kind="evidence", entity_id=id)
        return None if entry is None else evidence_result_from_payload(entry.payload)

    def list_evidence(
        self, *, change_set_id: str | None = None
    ) -> tuple[CommandEvidenceResult, ...]:
        values = tuple(
            evidence_result_from_payload(entry.payload)
            for entry in self._ledger.history(kind="evidence")
        )
        if change_set_id is None:
            return values
        return tuple(
            value
            for value in values
            if value.evidence.subject.kind is SubjectKind.CHANGE_SET
            and value.evidence.subject.id == change_set_id
        )


def scope_audit_to_payload(
    result: ScopeAuditResult, *, change_set_id: str
) -> dict[str, Any]:
    return {
        "change_set_id": change_set_id,
        "observation_id": result.observation_id,
        "declared_scopes": [scope.to_dict() for scope in result.declared_scopes],
        "actual_paths": list(result.actual_paths),
        "semantic_changes": [scope.to_dict() for scope in result.semantic_changes],
        "findings": [
            {
                "id": finding.id,
                "severity": finding.severity.value,
                "scope": finding.scope.to_dict(),
                "rationale": finding.rationale,
                "status": finding.status.value,
            }
            for finding in result.findings
        ],
        "is_within_scope": result.is_within_scope,
        "audited_at": result.audited_at.isoformat(),
    }


def evidence_result_to_payload(result: CommandEvidenceResult) -> dict[str, Any]:
    evidence = result.evidence
    return {
        "id": evidence.id,
        "kind": evidence.kind.value,
        "producer": {"kind": evidence.producer.kind.value, "id": evidence.producer.id},
        "subject": {"kind": evidence.subject.kind.value, "id": evidence.subject.id},
        "bound_commit_sha": evidence.bound_commit_sha,
        "environment": (
            None
            if evidence.environment is None
            else {
                "fingerprint": evidence.environment.fingerprint,
                "description": evidence.environment.description,
            }
        ),
        "command": (
            None
            if evidence.command is None
            else {"argv": list(evidence.command.argv), "cwd": evidence.command.cwd}
        ),
        "artifacts": [
            {"uri": artifact.uri, "digest": artifact.digest}
            for artifact in evidence.artifacts
        ],
        "state": evidence.state.value,
        "detail": evidence.detail,
        "stale_reasons": sorted(reason.value for reason in evidence.stale_reasons),
        "created_at": evidence.created_at.isoformat(),
        "updated_at": evidence.updated_at.isoformat(),
        "started_at": None if evidence.started_at is None else evidence.started_at.isoformat(),
        "completed_at": None if evidence.completed_at is None else evidence.completed_at.isoformat(),
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "stdout_digest": result.stdout_digest,
        "stderr_digest": result.stderr_digest,
        "timed_out": result.timed_out,
    }


def evidence_result_from_payload(payload: Mapping[str, Any]) -> CommandEvidenceResult:
    try:
        producer = payload["producer"]
        subject = payload["subject"]
        environment_value = payload["environment"]
        command_value = payload["command"]
        evidence = Evidence(
            id=str(payload["id"]),
            kind=EvidenceKind(str(payload["kind"])),
            producer=EvidenceProducer(
                ProducerKind(str(producer["kind"])), str(producer["id"])
            ),
            subject=EvidenceSubject(
                SubjectKind(str(subject["kind"])), str(subject["id"])
            ),
            bound_commit_sha=(
                None
                if payload["bound_commit_sha"] is None
                else str(payload["bound_commit_sha"])
            ),
            environment=(
                None
                if environment_value is None
                else Environment(
                    str(environment_value["fingerprint"]),
                    environment_value["description"],
                )
            ),
            command=(
                None
                if command_value is None
                else Command(
                    tuple(str(value) for value in command_value["argv"]),
                    str(command_value["cwd"]),
                )
            ),
            artifacts=tuple(
                ArtifactReference(str(value["uri"]), value["digest"])
                for value in payload["artifacts"]
            ),
            state=EvidenceState(str(payload["state"])),
            detail=None if payload["detail"] is None else str(payload["detail"]),
            stale_reasons=frozenset(
                StaleReason(str(value)) for value in payload["stale_reasons"]
            ),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            started_at=_optional_datetime(payload["started_at"]),
            completed_at=_optional_datetime(payload["completed_at"]),
        )
        return CommandEvidenceResult(
            evidence=evidence,
            exit_code=(None if payload["exit_code"] is None else int(payload["exit_code"])),
            duration_seconds=float(payload["duration_seconds"]),
            stdout_digest=str(payload["stdout_digest"]),
            stderr_digest=str(payload["stderr_digest"]),
            timed_out=bool(payload["timed_out"]),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise LocalWorkflowError("stored evidence snapshot is malformed") from error


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))
