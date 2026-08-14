"""Application-owned durable Change Set workflows and ledger codec."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from weftmark.application.change_binding import (
    ChangeBinding,
    ChangeBindingService,
    GitLineageObservation,
)
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.git import GitPort
from weftmark.domain.changeset import (
    ChangeSet,
    ChangeSetState,
    LineageEvent,
    LineageEventKind,
)
from weftmark.domain.scope import Scope


class WorkspaceError(ValueError):
    """Raised when durable workspace state is missing or malformed."""


class WorkspaceService:
    def __init__(self, git: GitPort, ledger: LedgerService) -> None:
        self._git = git
        self._ledger = ledger

    def create_change_set(
        self,
        *,
        id: str,
        goal: str,
        base_revision: str,
        scopes: tuple[Scope, ...],
        created_at: datetime,
    ) -> ChangeBinding:
        if self._ledger.latest(kind="changeset", entity_id=id) is not None:
            raise WorkspaceError(f"Change Set already exists: {id}")
        repository = self._git.repository()
        head = self._git.head()
        base = self._git.commit(base_revision).id
        tracked_base_revision = str(base) if base_revision == "HEAD" else base_revision
        if head.branch is None or repository.worktree is None:
            raise WorkspaceError("Change Set creation requires an attached working tree")
        planned = ChangeSet.plan(
            id=id,
            goal=goal,
            repository_id=repository.id,
            base_sha=str(base),
            branch=head.branch,
            worktree=repository.worktree,
            scopes=tuple(scope.canonical for scope in scopes),
            at=created_at,
        )
        binding = ChangeBindingService(self._git).create(
            planned,
            base_revision=tracked_base_revision,
            observed_at=created_at,
        )
        self._record(binding, recorded_at=created_at)
        return binding

    def get_change_set(self, id: str) -> ChangeBinding | None:
        entry = self._ledger.latest(kind="changeset", entity_id=id)
        return None if entry is None else binding_from_payload(entry.payload)

    def require_change_set(self, id: str) -> ChangeBinding:
        binding = self.get_change_set(id)
        if binding is None:
            raise WorkspaceError(f"Change Set not found: {id}")
        return binding

    def list_change_sets(self) -> tuple[ChangeBinding, ...]:
        latest: dict[str, Mapping[str, Any]] = {}
        for entry in self._ledger.history(kind="changeset"):
            latest[entry.entity_id] = entry.payload
        return tuple(binding_from_payload(latest[id]) for id in sorted(latest))

    def refresh_change_set(
        self,
        id: str,
        *,
        observed_at: datetime,
        base_revision: str | None = None,
    ) -> ChangeBinding:
        current = self.require_change_set(id)
        refreshed = ChangeBindingService(self._git).refresh(
            current,
            observed_at=observed_at,
            base_revision=base_revision,
        )
        self._record(refreshed, recorded_at=observed_at)
        return refreshed

    def _record(self, binding: ChangeBinding, *, recorded_at: datetime) -> None:
        self._ledger.record(
            kind="changeset",
            entity_id=binding.change_set.id,
            payload=binding_to_payload(binding),
            recorded_at=recorded_at,
        )


def binding_to_payload(binding: ChangeBinding) -> dict[str, Any]:
    latest = binding.latest
    scopes = tuple(Scope.parse(value) for value in binding.change_set.scopes)
    return {
        "schema_version": 1,
        "id": binding.change_set.id,
        "goal": binding.change_set.goal,
        "repository_id": binding.change_set.repository_id,
        "base_sha": latest.base_sha,
        "head_sha": latest.head_sha,
        "base_revision": binding.base_revision,
        "branch": latest.branch,
        "worktree": latest.worktree,
        "scopes": [scope.to_dict() for scope in scopes],
        "state": binding.change_set.state.value,
        "observation_id": latest.id,
        "changed_paths": list(latest.changed_paths),
        "dirty_paths": list(latest.dirty_paths),
        "created_at": binding.change_set.created_at.isoformat(),
        "updated_at": binding.change_set.updated_at.isoformat(),
        "lineage": [_lineage_to_dict(event) for event in binding.change_set.lineage],
        "observations": [_observation_to_dict(value) for value in binding.observations],
    }


def binding_from_payload(payload: Mapping[str, Any]) -> ChangeBinding:
    try:
        scopes = tuple(Scope.from_dict(value) for value in payload["scopes"])
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        updated_at = datetime.fromisoformat(str(payload["updated_at"]))
        if payload.get("schema_version") == 1:
            lineage = tuple(_lineage_from_dict(value) for value in payload["lineage"])
            observations = tuple(
                _observation_from_dict(value) for value in payload["observations"]
            )
        else:
            lineage = (_legacy_activation(payload, updated_at),)
            observations = (_legacy_observation(payload, updated_at),)
        change_set = ChangeSet(
            id=str(payload["id"]),
            goal=str(payload["goal"]),
            repository_id=str(payload["repository_id"]),
            base_sha=str(payload["base_sha"]),
            head_sha=str(payload["head_sha"]),
            branch=str(payload["branch"]),
            worktree=str(payload["worktree"]),
            scopes=tuple(scope.canonical for scope in scopes),
            state=ChangeSetState(str(payload["state"])),
            created_at=created_at,
            updated_at=updated_at,
            lineage=lineage,
        )
        return ChangeBinding(change_set, str(payload["base_revision"]), observations)
    except (KeyError, TypeError, ValueError) as error:
        raise WorkspaceError("stored Change Set snapshot is malformed") from error


def _lineage_to_dict(event: LineageEvent) -> dict[str, str]:
    return {
        "kind": event.kind.value,
        "occurred_at": event.occurred_at.isoformat(),
        "previous_base_sha": event.previous_base_sha,
        "base_sha": event.base_sha,
        "previous_head_sha": event.previous_head_sha,
        "head_sha": event.head_sha,
        "previous_branch": event.previous_branch,
        "branch": event.branch,
    }


def _lineage_from_dict(value: Mapping[str, Any]) -> LineageEvent:
    return LineageEvent(
        kind=LineageEventKind(str(value["kind"])),
        occurred_at=datetime.fromisoformat(str(value["occurred_at"])),
        previous_base_sha=str(value["previous_base_sha"]),
        base_sha=str(value["base_sha"]),
        previous_head_sha=str(value["previous_head_sha"]),
        head_sha=str(value["head_sha"]),
        previous_branch=str(value["previous_branch"]),
        branch=str(value["branch"]),
    )


def _observation_to_dict(value: GitLineageObservation) -> dict[str, Any]:
    return {
        "id": value.id,
        "repository_id": value.repository_id,
        "base_revision": value.base_revision,
        "base_sha": value.base_sha,
        "head_sha": value.head_sha,
        "branch": value.branch,
        "worktree": value.worktree,
        "changed_paths": list(value.changed_paths),
        "dirty_paths": list(value.dirty_paths),
        "observed_at": value.observed_at.isoformat(),
    }


def _observation_from_dict(value: Mapping[str, Any]) -> GitLineageObservation:
    return GitLineageObservation(
        id=str(value["id"]),
        repository_id=str(value["repository_id"]),
        base_revision=str(value["base_revision"]),
        base_sha=str(value["base_sha"]),
        head_sha=str(value["head_sha"]),
        branch=str(value["branch"]),
        worktree=str(value["worktree"]),
        changed_paths=tuple(str(path) for path in value["changed_paths"]),
        dirty_paths=tuple(str(path) for path in value["dirty_paths"]),
        observed_at=datetime.fromisoformat(str(value["observed_at"])),
    )


def _legacy_activation(payload: Mapping[str, Any], at: datetime) -> LineageEvent:
    return LineageEvent(
        kind=LineageEventKind.ACTIVATED,
        occurred_at=at,
        previous_base_sha=str(payload["base_sha"]),
        base_sha=str(payload["base_sha"]),
        previous_head_sha=str(payload["base_sha"]),
        head_sha=str(payload["head_sha"]),
        previous_branch=str(payload["branch"]),
        branch=str(payload["branch"]),
    )


def _legacy_observation(
    payload: Mapping[str, Any], at: datetime
) -> GitLineageObservation:
    return GitLineageObservation(
        id=str(payload["observation_id"]),
        repository_id=str(payload["repository_id"]),
        base_revision=str(payload["base_revision"]),
        base_sha=str(payload["base_sha"]),
        head_sha=str(payload["head_sha"]),
        branch=str(payload["branch"]),
        worktree=str(payload["worktree"]),
        changed_paths=tuple(str(path) for path in payload["changed_paths"]),
        dirty_paths=tuple(str(path) for path in payload["dirty_paths"]),
        observed_at=at,
    )
