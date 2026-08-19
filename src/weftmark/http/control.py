"""Strict HTTP-facing adapter over the application ControlService."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import unquote

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.claims import ClaimService
from weftmark.application.control import ControlResult, ControlService
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.task_claims import TaskClaimService
from weftmark.application.task_planning import TaskPlanningService
from weftmark.application.tasks import TaskService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceProducer, ProducerKind


class ControlCapability(StrEnum):
    CLAIM = "claim"
    RELEASE = "release"
    HANDOFF = "handoff"


class ControlHttpError(ValueError):
    """Raised when a remote mutation payload is malformed."""


@dataclass(frozen=True, slots=True)
class ControlRoute:
    capability: ControlCapability
    operation: str
    target_id: str


# dataclass import kept adjacent to the public route type for a compact module API.
from dataclasses import dataclass


@runtime_checkable
class ControlProvider(Protocol):
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
    ) -> ControlResult: ...

    def release_claim(
        self,
        claim_id: str,
        *,
        idempotency_key: str,
        agent_id: str,
        session_id: str,
        reason: str,
        requested_at: datetime,
    ) -> ControlResult: ...

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
    ) -> ControlResult: ...


class LocalControlProvider:
    """Compose the same local services used by CLI workflows."""

    def __init__(self, repo: str, ledger_path: Path) -> None:
        git = LocalGit(repo)
        ledger = LedgerService(JsonlLedger(ledger_path))
        workspace = WorkspaceService(git, ledger)
        claims = ClaimService(workspace, ledger)
        tasks = TaskService(ledger)
        task_claims = TaskClaimService(
            TaskPlanningService(tasks), tasks, workspace, claims, ledger
        )
        workflow = LocalWorkflowService(
            workspace,
            ledger,
            EvidenceProducer(ProducerKind.WORKER, "weftmark-http-control"),
        )
        self._service = ControlService(task_claims, claims, workflow, ledger)

    def claim_task(self, *args: object, **kwargs: object) -> ControlResult:
        return self._service.claim_task(*args, **kwargs)  # type: ignore[arg-type]

    def release_claim(self, *args: object, **kwargs: object) -> ControlResult:
        return self._service.release_claim(*args, **kwargs)  # type: ignore[arg-type]

    def create_handoff(self, *args: object, **kwargs: object) -> ControlResult:
        return self._service.create_handoff(*args, **kwargs)  # type: ignore[arg-type]


def parse_control_route(path: str) -> ControlRoute | None:
    segments = tuple(segment for segment in path.split("/") if segment)
    if len(segments) != 5 or segments[:2] != ("v0", "control"):
        return None
    collection, raw_id, action = segments[2:]
    target_id = unquote(raw_id).strip()
    if not target_id or "/" in target_id or "\x00" in target_id:
        return None
    if collection == "tasks" and action == "claim":
        return ControlRoute(ControlCapability.CLAIM, "claim_task", target_id)
    if collection == "claims" and action == "release":
        return ControlRoute(ControlCapability.RELEASE, "release_claim", target_id)
    if collection == "changes" and action == "handoffs":
        return ControlRoute(ControlCapability.HANDOFF, "create_handoff", target_id)
    return None


def dispatch_control(
    provider: ControlProvider,
    route: ControlRoute,
    *,
    payload: Mapping[str, Any],
    idempotency_key: str,
    requested_at: datetime,
) -> ControlResult:
    if route.operation == "claim_task":
        values = _strict_fields(
            payload,
            required=(
                "change_set_id",
                "claim_id",
                "base_revision",
                "agent_id",
                "session_id",
                "lease_seconds",
            ),
        )
        lease_seconds = values["lease_seconds"]
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise ControlHttpError("lease_seconds must be an integer")
        return provider.claim_task(
            route.target_id,
            idempotency_key=idempotency_key,
            change_set_id=_text(values, "change_set_id"),
            claim_id=_text(values, "claim_id"),
            base_revision=_text(values, "base_revision"),
            agent_id=_text(values, "agent_id"),
            session_id=_text(values, "session_id"),
            lease_seconds=lease_seconds,
            requested_at=requested_at,
        )

    if route.operation == "release_claim":
        values = _strict_fields(
            payload,
            required=("agent_id", "session_id", "reason"),
        )
        return provider.release_claim(
            route.target_id,
            idempotency_key=idempotency_key,
            agent_id=_text(values, "agent_id"),
            session_id=_text(values, "session_id"),
            reason=_text(values, "reason"),
            requested_at=requested_at,
        )

    if route.operation == "create_handoff":
        values = _strict_fields(
            payload,
            required=("handoff_id", "task_id", "next_action", "created_by"),
            optional=(
                "intended_receiver_id",
                "known_failures",
                "supersedes_id",
            ),
        )
        known = values.get("known_failures", [])
        if not isinstance(known, list) or any(not isinstance(value, str) for value in known):
            raise ControlHttpError("known_failures must be an array of strings")
        return provider.create_handoff(
            route.target_id,
            idempotency_key=idempotency_key,
            handoff_id=_text(values, "handoff_id"),
            task_id=_text(values, "task_id"),
            next_action=_text(values, "next_action"),
            created_by=_text(values, "created_by"),
            requested_at=requested_at,
            intended_receiver_id=_optional_text(values, "intended_receiver_id"),
            known_failures=tuple(known),
            supersedes_id=_optional_text(values, "supersedes_id"),
        )

    raise ControlHttpError("unsupported control operation")


def _strict_fields(
    payload: Mapping[str, Any],
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> Mapping[str, Any]:
    allowed = frozenset((*required, *optional))
    unknown = sorted(set(payload) - allowed)
    missing = sorted(set(required) - set(payload))
    if unknown:
        raise ControlHttpError("unknown fields: " + ", ".join(unknown))
    if missing:
        raise ControlHttpError("missing fields: " + ", ".join(missing))
    return payload


def _text(payload: Mapping[str, Any], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str) or not value.strip():
        raise ControlHttpError(f"{name} must be a non-empty string")
    return value


def _optional_text(payload: Mapping[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ControlHttpError(f"{name} must be null or a non-empty string")
    return value
