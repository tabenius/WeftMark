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
from weftmark.application.review_service import ReviewerSummary, ReviewService
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
from weftmark.domain.handoff import Handoff, HandoffError
from weftmark.domain.policy import EvidencePolicy, EvidenceRequirement
from weftmark.domain.review import ReviewFinding
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

    def review(
        self,
        change_set_id: str,
        *,
        decision_id: str,
        author_id: str,
        required_kinds: tuple[EvidenceKind, ...],
        optional_kinds: tuple[EvidenceKind, ...] = (),
        semantic_changes: tuple[Scope, ...] = (),
        additional_findings: tuple[ReviewFinding, ...] = (),
        decided_at: datetime,
    ) -> ReviewerSummary:
        if self._ledger.latest(kind="review", entity_id=decision_id) is not None:
            raise LocalWorkflowError(f"Review already exists: {decision_id}")
        if not required_kinds:
            raise LocalWorkflowError("review requires at least one evidence kind")
        all_kinds = (*required_kinds, *optional_kinds)
        if len(set(all_kinds)) != len(all_kinds):
            raise LocalWorkflowError("review evidence kinds must not contain duplicates")
        scope_audit = self.audit_scope(
            change_set_id,
            semantic_changes=semantic_changes,
            audited_at=decided_at,
        )
        binding = self._workspace.require_change_set(change_set_id)
        requirements = tuple(
            EvidenceRequirement(f"require-{kind.value}", kind)
            for kind in required_kinds
        ) + tuple(
            EvidenceRequirement(f"optional-{kind.value}", kind, required=False)
            for kind in optional_kinds
        )
        policy = EvidencePolicy(
            f"policy:{change_set_id}:{decision_id}",
            EvidenceSubject(SubjectKind.CHANGE_SET, change_set_id),
            requirements,
        )
        records = tuple(
            result.evidence
            for result in self.list_evidence(change_set_id=change_set_id)
        )
        summary = ReviewService().summarize(
            binding,
            scope_audit,
            policy,
            records,
            decision_id=decision_id,
            author_id=author_id,
            decided_at=decided_at,
            additional_findings=additional_findings,
        )
        self._ledger.record(
            kind="review",
            entity_id=decision_id,
            payload=review_summary_to_payload(summary),
            recorded_at=decided_at,
        )
        return summary

    def get_review(self, id: str) -> dict[str, Any] | None:
        entry = self._ledger.latest(kind="review", entity_id=id)
        return None if entry is None else entry.payload

    def list_reviews(
        self, *, change_set_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        payloads = tuple(entry.payload for entry in self._ledger.history(kind="review"))
        if change_set_id is None:
            return payloads
        return tuple(
            payload
            for payload in payloads
            if payload["change_set_id"] == change_set_id
        )

    def create_handoff(
        self,
        change_set_id: str,
        *,
        id: str,
        task_id: str,
        next_action: str,
        created_by: str,
        created_at: datetime,
        intended_receiver_id: str | None = None,
        known_failures: tuple[str, ...] = (),
        supersedes_id: str | None = None,
    ) -> Handoff:
        if self._ledger.latest(kind="handoff", entity_id=id) is not None:
            raise LocalWorkflowError(f"Handoff already exists: {id}")
        binding = self._workspace.refresh_change_set(
            change_set_id, observed_at=created_at
        )
        if binding.latest.dirty_paths:
            raise LocalWorkflowError(
                "portable handoff requires a clean worktree; commit or discard changes"
            )
        evidence_ids = tuple(
            result.evidence.id
            for result in self.list_evidence(change_set_id=change_set_id)
        )
        decision_ids = tuple(
            str(payload["decision"]["id"])
            for payload in self.list_reviews(change_set_id=change_set_id)
        )
        scopes = tuple(Scope.parse(value) for value in binding.change_set.scopes)
        if supersedes_id is None:
            handoff = Handoff(
                id=id,
                task_id=task_id,
                change_set_id=change_set_id,
                goal=binding.change_set.goal,
                repository_id=binding.change_set.repository_id,
                base_sha=binding.latest.base_sha,
                head_sha=binding.latest.head_sha,
                branch=binding.latest.branch,
                worktree=binding.latest.worktree,
                source_observation_id=binding.latest.id,
                scopes=scopes,
                evidence_ids=evidence_ids,
                decision_ids=decision_ids,
                known_failures=known_failures,
                next_action=next_action,
                created_by=created_by,
                created_at=created_at,
                intended_receiver_id=intended_receiver_id,
            )
        else:
            previous = self.get_handoff(supersedes_id)
            if previous is None:
                raise LocalWorkflowError(f"Handoff not found: {supersedes_id}")
            if previous.change_set_id != change_set_id or previous.task_id != task_id:
                raise LocalWorkflowError(
                    "superseding handoff must preserve task and Change Set identity"
                )
            handoff = previous.supersede(
                id=id,
                created_by=created_by,
                created_at=created_at,
                source_observation_id=binding.latest.id,
                base_sha=binding.latest.base_sha,
                head_sha=binding.latest.head_sha,
                branch=binding.latest.branch,
                worktree=binding.latest.worktree,
                scopes=scopes,
                evidence_ids=evidence_ids,
                decision_ids=decision_ids,
                known_failures=known_failures,
                next_action=next_action,
                intended_receiver_id=intended_receiver_id,
            )
        self._ledger.record(
            kind="handoff",
            entity_id=id,
            payload=handoff.to_dict(),
            recorded_at=created_at,
        )
        return handoff

    def get_handoff(self, id: str) -> Handoff | None:
        entry = self._ledger.latest(kind="handoff", entity_id=id)
        return None if entry is None else handoff_from_payload(entry.payload)

    def list_handoffs(
        self, *, change_set_id: str | None = None
    ) -> tuple[Handoff, ...]:
        values = tuple(
            handoff_from_payload(entry.payload)
            for entry in self._ledger.history(kind="handoff")
        )
        if change_set_id is None:
            return values
        return tuple(value for value in values if value.change_set_id == change_set_id)


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


def review_summary_to_payload(summary: ReviewerSummary) -> dict[str, Any]:
    decision = summary.decision
    return {
        "decision": {
            "id": decision.id,
            "change_set_id": decision.change_set_id,
            "author_id": decision.author_id,
            "outcome": decision.outcome.value,
            "head_sha": decision.head_sha,
            "evidence_ids": list(decision.evidence_ids),
            "rationale": decision.rationale,
            "created_at": decision.created_at.isoformat(),
        },
        "change_set_id": decision.change_set_id,
        "goal": summary.goal,
        "repository_id": summary.repository_id,
        "base_sha": summary.base_sha,
        "head_sha": summary.head_sha,
        "branch": summary.branch,
        "worktree": summary.worktree,
        "declared_scopes": list(summary.declared_scopes),
        "changed_paths": list(summary.changed_paths),
        "dirty_paths": list(summary.dirty_paths),
        "semantic_changes": list(summary.semantic_changes),
        "lineage_event_count": summary.lineage_event_count,
        "evidence": [
            {
                "id": value.id,
                "kind": value.kind.value,
                "state": value.state.value,
                "bound_commit_sha": value.bound_commit_sha,
                "is_obsolete": value.is_obsolete,
            }
            for value in summary.evidence
        ],
        "policy": {
            "id": summary.policy.policy_id,
            "is_satisfied": summary.policy.is_satisfied,
            "applicable_requirement_ids": list(
                summary.policy.applicable_requirement_ids
            ),
            "satisfied_requirement_ids": list(
                summary.policy.satisfied_requirement_ids
            ),
            "issues": [
                {
                    "requirement_id": issue.requirement_id,
                    "kind": issue.kind.value,
                    "required": issue.required,
                    "problem": issue.problem.value,
                    "explanation": issue.explanation,
                }
                for issue in summary.policy.issues
            ],
        },
        "findings": [
            {
                "id": finding.id,
                "severity": finding.severity.value,
                "scope": finding.scope.to_dict(),
                "rationale": finding.rationale,
                "status": finding.status.value,
            }
            for finding in decision.findings
        ],
        "explanations": list(summary.explanations),
        "is_releasable": summary.is_releasable,
    }


def handoff_from_payload(payload: Mapping[str, Any]) -> Handoff:
    try:
        return Handoff(
            id=str(payload["id"]),
            task_id=str(payload["task_id"]),
            change_set_id=str(payload["change_set_id"]),
            goal=str(payload["goal"]),
            repository_id=str(payload["repository_id"]),
            base_sha=str(payload["base_sha"]),
            head_sha=str(payload["head_sha"]),
            branch=str(payload["branch"]),
            worktree=str(payload["worktree"]),
            source_observation_id=str(payload["source_observation_id"]),
            scopes=tuple(Scope.from_dict(value) for value in payload["scopes"]),
            evidence_ids=tuple(str(value) for value in payload["evidence_ids"]),
            decision_ids=tuple(str(value) for value in payload["decision_ids"]),
            known_failures=tuple(str(value) for value in payload["known_failures"]),
            next_action=str(payload["next_action"]),
            created_by=str(payload["created_by"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            intended_receiver_id=(
                None
                if payload["intended_receiver_id"] is None
                else str(payload["intended_receiver_id"])
            ),
            supersedes_id=(
                None
                if payload["supersedes_id"] is None
                else str(payload["supersedes_id"])
            ),
            generation=int(payload["generation"]),
        )
    except (AttributeError, HandoffError, KeyError, TypeError, ValueError) as error:
        raise LocalWorkflowError("stored handoff snapshot is malformed") from error
