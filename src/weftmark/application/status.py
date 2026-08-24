"""Compact workspace orientation from durable application records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from weftmark.application.claims import Claim, ClaimService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import LedgerEntry
from weftmark.application.tasks import TaskService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceState, SubjectKind
from weftmark.domain.lock import LockState, scopes_overlap
from weftmark.domain.scope import Scope


@dataclass(frozen=True, slots=True)
class ScopeCollision:
    """A declared Change Set scope blocked by another active claim."""

    claim_id: str
    competing_change_set_id: str
    requested_scope: Scope
    owned_scope: Scope


@dataclass(frozen=True, slots=True)
class ChangeSetStatus:
    id: str
    goal: str
    lifecycle_state: str
    branch: str
    observed_head_sha: str
    observed_at: datetime
    dirty_paths: tuple[str, ...]
    active_claim_ids: tuple[str, ...]
    evidence_count: int
    current_evidence_count: int
    obsolete_evidence_count: int
    failed_evidence_count: int
    unavailable_evidence_count: int
    latest_review_id: str | None
    latest_review_outcome: str | None
    latest_review_head_sha: str | None
    latest_review_is_current: bool
    latest_handoff_id: str | None
    latest_handoff_head_sha: str | None
    latest_handoff_is_current: bool
    scope_collisions: tuple[ScopeCollision, ...] = ()

    @property
    def readiness(self) -> str:
        if self.latest_review_id is None:
            return "unreviewed"
        if not self.latest_review_is_current:
            return "stale"
        return self.latest_review_outcome or "unreviewed"


@dataclass(frozen=True, slots=True)
class TaskSource:
    kind: str
    label: str
    digest: str | None


@dataclass(frozen=True, slots=True)
class TaskStatus:
    id: str
    title: str
    state: str
    priority: str
    created_at: datetime
    updated_at: datetime
    dependencies: tuple[str, ...]
    conflicts: tuple[str, ...]
    sources: tuple[TaskSource, ...]


@dataclass(frozen=True, slots=True)
class TaskChangeSetLink:
    task_id: str
    change_set_id: str
    claim_id: str
    binding_state: str


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    generated_at: datetime
    change_sets: tuple[ChangeSetStatus, ...]
    active_claim_count: int
    expired_claim_count: int
    released_claim_count: int
    tasks: tuple[TaskStatus, ...] = ()
    task_change_set_links: tuple[TaskChangeSetLink, ...] = ()


def _scope_collisions(
    *,
    change_set_id: str,
    declared_scopes: tuple[str, ...],
    claims: tuple[Claim, ...],
    observed_at: datetime,
) -> tuple[ScopeCollision, ...]:
    """Find active ownership that would prevent this Change Set from claiming scope.

    This is intentionally asymmetric: a Change Set is not reported as colliding
    with its own active claim. The result answers what currently blocks this
    Change Set, rather than reconstructing an impossible pair of conflicting
    successful claims.
    """

    requested = tuple(Scope.parse(value) for value in declared_scopes)
    collisions: list[ScopeCollision] = []
    for claim in claims:
        if claim.change_set_id == change_set_id:
            continue
        if claim.state_at(observed_at) is not LockState.ACTIVE:
            continue
        for requested_scope in requested:
            for lock in claim.locks:
                if not lock.owns_scope_at(observed_at):
                    continue
                if scopes_overlap(requested_scope, lock.scope):
                    collisions.append(
                        ScopeCollision(
                            claim_id=claim.id,
                            competing_change_set_id=claim.change_set_id,
                            requested_scope=requested_scope,
                            owned_scope=lock.scope,
                        )
                    )
    return tuple(
        sorted(
            collisions,
            key=lambda value: (
                value.competing_change_set_id,
                value.claim_id,
                value.requested_scope.canonical,
                value.owned_scope.canonical,
            ),
        )
    )


class StatusService:
    def __init__(
        self,
        workspace: WorkspaceService,
        claims: ClaimService,
        workflow: LocalWorkflowService,
        *,
        tasks: TaskService | None = None,
        ledger: LedgerService | None = None,
    ) -> None:
        self._workspace = workspace
        self._claims = claims
        self._workflow = workflow
        if (tasks is None) != (ledger is None):
            raise ValueError("native task status requires both TaskService and ledger")
        self._tasks = tasks
        self._ledger = ledger

    def summarize(self, *, observed_at: datetime) -> WorkspaceStatus:
        claims = self._claims.list()
        evidence = self._workflow.list_evidence()
        reviews = self._workflow.list_reviews()
        handoffs = self._workflow.list_handoffs()
        values: list[ChangeSetStatus] = []
        for binding in self._workspace.list_change_sets():
            change_set_id = binding.change_set.id
            matching_claims = tuple(
                claim for claim in claims if claim.change_set_id == change_set_id
            )
            active_claim_ids = tuple(
                claim.id
                for claim in matching_claims
                if claim.state_at(observed_at) is LockState.ACTIVE
            )
            matching_evidence = tuple(
                result.evidence
                for result in evidence
                if result.evidence.subject.kind is SubjectKind.CHANGE_SET
                and result.evidence.subject.id == change_set_id
            )
            current_evidence = tuple(
                item
                for item in matching_evidence
                if item.bound_commit_sha == binding.latest.head_sha
                and item.state
                not in {EvidenceState.STALE, EvidenceState.SUPERSEDED}
            )
            matching_reviews = tuple(
                payload
                for payload in reviews
                if payload["change_set_id"] == change_set_id
            )
            latest_review = matching_reviews[-1] if matching_reviews else None
            decision = None if latest_review is None else latest_review["decision"]
            matching_handoffs = tuple(
                handoff for handoff in handoffs if handoff.change_set_id == change_set_id
            )
            latest_handoff = matching_handoffs[-1] if matching_handoffs else None
            terminal = binding.change_set.state.value in {"merged", "closed", "abandoned"}
            values.append(
                ChangeSetStatus(
                    id=change_set_id,
                    goal=binding.change_set.goal,
                    lifecycle_state=binding.change_set.state.value,
                    branch=binding.latest.branch,
                    observed_head_sha=binding.latest.head_sha,
                    observed_at=binding.latest.observed_at,
                    dirty_paths=binding.latest.dirty_paths,
                    active_claim_ids=active_claim_ids,
                    evidence_count=len(matching_evidence),
                    current_evidence_count=len(current_evidence),
                    obsolete_evidence_count=(
                        len(matching_evidence) - len(current_evidence)
                    ),
                    failed_evidence_count=sum(
                        item.state is EvidenceState.FAILED
                        for item in matching_evidence
                    ),
                    unavailable_evidence_count=sum(
                        item.state is EvidenceState.UNAVAILABLE
                        for item in matching_evidence
                    ),
                    latest_review_id=(
                        None if decision is None else str(decision["id"])
                    ),
                    latest_review_outcome=(
                        None if decision is None else str(decision["outcome"])
                    ),
                    latest_review_head_sha=(
                        None if decision is None else str(decision["head_sha"])
                    ),
                    latest_review_is_current=(
                        decision is not None
                        and decision["head_sha"] == binding.latest.head_sha
                    ),
                    latest_handoff_id=(
                        None if latest_handoff is None else latest_handoff.id
                    ),
                    latest_handoff_head_sha=(
                        None if latest_handoff is None else latest_handoff.head_sha
                    ),
                    latest_handoff_is_current=(
                        latest_handoff is not None
                        and latest_handoff.head_sha == binding.latest.head_sha
                    ),
                    scope_collisions=(
                        ()
                        if terminal
                        else _scope_collisions(
                            change_set_id=change_set_id,
                            declared_scopes=binding.change_set.scopes,
                            claims=claims,
                            observed_at=observed_at,
                        )
                    ),
                )
            )
        states = tuple(claim.state_at(observed_at) for claim in claims)
        task_values, task_links = self._task_status()
        return WorkspaceStatus(
            generated_at=observed_at,
            change_sets=tuple(values),
            active_claim_count=states.count(LockState.ACTIVE),
            expired_claim_count=states.count(LockState.EXPIRED),
            released_claim_count=states.count(LockState.RELEASED),
            tasks=task_values,
            task_change_set_links=task_links,
        )

    def _task_status(
        self,
    ) -> tuple[tuple[TaskStatus, ...], tuple[TaskChangeSetLink, ...]]:
        if self._tasks is None or self._ledger is None:
            return (), ()
        entries = self._ledger.snapshot()
        sources = _task_sources(entries)
        dependencies: dict[str, list[str]] = {}
        for value in self._tasks.dependencies():
            dependencies.setdefault(value.task_id, []).append(value.depends_on_task_id)
        conflicts: dict[str, list[str]] = {}
        for value in self._tasks.conflicts():
            conflicts.setdefault(value.first_task_id, []).append(value.second_task_id)
            conflicts.setdefault(value.second_task_id, []).append(value.first_task_id)
        tasks = tuple(
            TaskStatus(
                id=value.id,
                title=value.title,
                state=value.state.value,
                priority=value.priority.value,
                created_at=value.created_at,
                updated_at=value.updated_at,
                dependencies=tuple(sorted(dependencies.get(value.id, ()))),
                conflicts=tuple(sorted(conflicts.get(value.id, ()))),
                sources=sources.get(
                    value.id, (TaskSource("native", "native-ledger", None),)
                ),
            )
            for value in self._tasks.list()
        )
        latest_bindings: dict[str, Mapping[str, Any]] = {}
        for entry in entries:
            if entry.kind == "task_work_claim":
                latest_bindings[entry.entity_id] = entry.payload
        links = tuple(
            _task_link(task_id, payload)
            for task_id, payload in sorted(latest_bindings.items())
        )
        return tasks, links


def _task_sources(
    entries: tuple[LedgerEntry, ...],
) -> dict[str, tuple[TaskSource, ...]]:
    values: dict[str, set[tuple[str, str, str | None]]] = {}
    for entry in entries:
        payload = entry.payload
        if entry.kind == "source_plan_import":
            ids = payload.get("native_task_ids")
            kind = "source_plan"
            digest = payload.get("source_digest")
        elif entry.kind in {"frog_native_task_import", "frog_task_import"}:
            native = payload.get("native_tasks")
            ids = tuple(native) if isinstance(native, Mapping) else None
            kind = "frog_snapshot"
            digest = payload.get("source_snapshot_digest")
        else:
            continue
        label = payload.get("source_label")
        if (
            not isinstance(ids, (list, tuple))
            or not isinstance(label, str)
            or not label.strip()
            or not isinstance(digest, str)
            or not digest.strip()
        ):
            continue
        for task_id in ids:
            if isinstance(task_id, str) and task_id:
                values.setdefault(task_id, set()).add((kind, label, digest))
    return {
        task_id: tuple(TaskSource(*value) for value in sorted(records))
        for task_id, records in values.items()
    }


def _task_link(task_id: str, payload: Mapping[str, Any]) -> TaskChangeSetLink:
    try:
        if (
            payload["schema_version"] != 1
            or payload["task_id"] != task_id
            or payload["state"] not in {"reserved", "completed"}
        ):
            raise ValueError("binding contract mismatch")
        change_set_id = str(payload["change_set_id"]).strip()
        claim_id = str(payload["claim_id"]).strip()
        if not change_set_id or not claim_id:
            raise ValueError("empty binding identity")
        return TaskChangeSetLink(
            task_id,
            change_set_id,
            claim_id,
            str(payload["state"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("stored native task work binding is malformed") from error


def _scope_collision_to_payload(value: ScopeCollision) -> dict[str, Any]:
    return {
        "claim_id": value.claim_id,
        "competing_change_set_id": value.competing_change_set_id,
        "requested_scope": value.requested_scope.to_dict(),
        "owned_scope": value.owned_scope.to_dict(),
    }


def status_to_payload(status: WorkspaceStatus) -> dict[str, Any]:
    return {
        "generated_at": status.generated_at.isoformat(),
        "counts": {
            "change_sets": len(status.change_sets),
            **(
                {"tasks": len(status.tasks)}
                if status.tasks or status.task_change_set_links
                else {}
            ),
            "active_claims": status.active_claim_count,
            "expired_claims": status.expired_claim_count,
            "released_claims": status.released_claim_count,
        },
        "tasks": [
            {
                "id": value.id,
                "title": value.title,
                "state": value.state,
                "priority": value.priority,
                "created_at": value.created_at.isoformat(),
                "updated_at": value.updated_at.isoformat(),
                "dependencies": list(value.dependencies),
                "conflicts": list(value.conflicts),
                "sources": [
                    {"kind": source.kind, "label": source.label, "digest": source.digest}
                    for source in value.sources
                ],
            }
            for value in status.tasks
        ],
        "task_change_set_links": [
            {
                "task_id": value.task_id,
                "change_set_id": value.change_set_id,
                "claim_id": value.claim_id,
                "binding_state": value.binding_state,
            }
            for value in status.task_change_set_links
        ],
        "change_sets": [
            {
                "id": value.id,
                "goal": value.goal,
                "lifecycle_state": value.lifecycle_state,
                "branch": value.branch,
                "observed_head_sha": value.observed_head_sha,
                "observed_at": value.observed_at.isoformat(),
                "dirty_paths": list(value.dirty_paths),
                "active_claim_ids": list(value.active_claim_ids),
                "scope_collisions": [
                    _scope_collision_to_payload(collision)
                    for collision in value.scope_collisions
                ],
                "evidence": {
                    "total": value.evidence_count,
                    "current": value.current_evidence_count,
                    "obsolete": value.obsolete_evidence_count,
                    "failed": value.failed_evidence_count,
                    "unavailable": value.unavailable_evidence_count,
                },
                "readiness": value.readiness,
                "latest_review": (
                    None
                    if value.latest_review_id is None
                    else {
                        "id": value.latest_review_id,
                        "outcome": value.latest_review_outcome,
                        "head_sha": value.latest_review_head_sha,
                        "is_current": value.latest_review_is_current,
                    }
                ),
                "latest_handoff": (
                    None
                    if value.latest_handoff_id is None
                    else {
                        "id": value.latest_handoff_id,
                        "head_sha": value.latest_handoff_head_sha,
                        "is_current": value.latest_handoff_is_current,
                    }
                ),
            }
            for value in status.change_sets
        ],
    }
