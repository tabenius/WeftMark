"""Provider-neutral contract for disposable worker/runtime integrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from weftmark.application.ports.git import GitChangeKind, GitObjectId


class RuntimeContractError(ValueError):
    """Raised when a runtime adapter returns an invalid contract value."""


def _require_text(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RuntimeContractError(f"{name} must not be empty")
    if "\x00" in normalized:
        raise RuntimeContractError(f"{name} must not contain NUL")
    return normalized


def _require_repo_path(name: str, value: str) -> str:
    normalized = _require_text(name, value).replace("\\", "/")
    parts = normalized.split("/")
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise RuntimeContractError(f"{name} must be a normalized repository-relative path")
    return normalized


def _require_aware_datetime(name: str, value: datetime | None) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise RuntimeContractError(f"{name} must include a timezone")


class RuntimeErrorCode(StrEnum):
    NOT_AVAILABLE = "not_available"
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    CONFLICT = "conflict"
    AGENT_UNAVAILABLE = "agent_unavailable"
    RUNTIME_FAILED = "runtime_failed"
    TRANSPORT_FAILED = "transport_failed"
    PERMISSION_DENIED = "permission_denied"


class RuntimeAdapterError(RuntimeError):
    """Normalized infrastructure failure from a runtime provider."""

    def __init__(self, code: RuntimeErrorCode, provider: str, operation: str, detail: str) -> None:
        self.code = code
        self.provider = _require_text("provider", provider)
        self.operation = _require_text("operation", operation)
        self.detail = _require_text("detail", detail)
        super().__init__(f"{self.provider}:{self.operation}: {self.code.value}: {self.detail}")


class RuntimeWorkerState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    EXITED = "exited"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RuntimeChangesMode(StrEnum):
    WORKING_COPY = "working_copy"
    LAST_TURN = "last_turn"


@dataclass(frozen=True, slots=True)
class RuntimeWorkspace:
    """Provider attachment for one repository."""

    provider: str
    workspace_id: str
    repo_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _require_text("provider", self.provider))
        object.__setattr__(self, "workspace_id", _require_text("workspace_id", self.workspace_id))
        object.__setattr__(self, "repo_path", _require_text("repo_path", self.repo_path))


@dataclass(frozen=True, slots=True)
class RuntimeChangeWorkspace:
    """Runtime-owned worktree attached to a WeftMark Change Set."""

    provider: str
    workspace_id: str
    change_set_id: str
    task_id: str
    base: GitObjectId
    worktree_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _require_text("provider", self.provider))
        object.__setattr__(self, "workspace_id", _require_text("workspace_id", self.workspace_id))
        object.__setattr__(self, "change_set_id", _require_text("change_set_id", self.change_set_id))
        object.__setattr__(self, "task_id", _require_text("task_id", self.task_id))
        object.__setattr__(self, "worktree_path", _require_text("worktree_path", self.worktree_path))


@dataclass(frozen=True, slots=True)
class RuntimeWorkerSummary:
    """Operational worker telemetry; never a review/readiness verdict."""

    provider: str
    change_set_id: str
    task_id: str
    state: RuntimeWorkerState
    agent_id: str | None = None
    session_id: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _require_text("provider", self.provider))
        object.__setattr__(self, "change_set_id", _require_text("change_set_id", self.change_set_id))
        object.__setattr__(self, "task_id", _require_text("task_id", self.task_id))
        if self.agent_id is not None:
            object.__setattr__(self, "agent_id", _require_text("agent_id", self.agent_id))
        if self.session_id is not None:
            object.__setattr__(self, "session_id", _require_text("session_id", self.session_id))
        if self.pid is not None and self.pid <= 0:
            raise RuntimeContractError("pid must be positive")
        _require_aware_datetime("started_at", self.started_at)
        _require_aware_datetime("updated_at", self.updated_at)


@dataclass(frozen=True, slots=True)
class RuntimeFileChange:
    path: str
    kind: GitChangeKind
    old_path: str | None = None
    additions: int | None = None
    deletions: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _require_repo_path("path", self.path))
        if self.old_path is not None:
            object.__setattr__(self, "old_path", _require_repo_path("old_path", self.old_path))
        if self.kind in {GitChangeKind.RENAMED, GitChangeKind.COPIED}:
            if self.old_path is None:
                raise RuntimeContractError(f"{self.kind.value} runtime changes require old_path")
        elif self.old_path is not None:
            raise RuntimeContractError("old_path is only valid for renamed or copied runtime changes")
        for name, value in (("additions", self.additions), ("deletions", self.deletions)):
            if value is not None and value < 0:
                raise RuntimeContractError(f"{name} must not be negative")


@dataclass(frozen=True, slots=True)
class RuntimeChanges:
    """Provider-reported working-copy/turn changes for review presentation."""

    provider: str
    change_set_id: str
    mode: RuntimeChangesMode
    files: tuple[RuntimeFileChange, ...]
    base: GitObjectId | None = None
    head: GitObjectId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _require_text("provider", self.provider))
        object.__setattr__(self, "change_set_id", _require_text("change_set_id", self.change_set_id))
        paths = [(entry.old_path, entry.path) for entry in self.files]
        if len(paths) != len(set(paths)):
            raise RuntimeContractError("runtime changes must not contain duplicate file entries")


@runtime_checkable
class RuntimePort(Protocol):
    """Replaceable execution provider used only through WeftMark application services.

    Implementations may own disposable worktrees, PTYs and agent processes. They
    do not own WeftMark lifecycle, claims, evidence, review, handoff or readiness.
    Browser/UI code must not receive the provider object directly.
    """

    def attach_workspace(self, repo_path: str) -> RuntimeWorkspace:
        """Discover or attach the provider's workspace for a repository."""

    def ensure_change_workspace(
        self,
        workspace: RuntimeWorkspace,
        change_set_id: str,
        base: GitObjectId,
    ) -> RuntimeChangeWorkspace:
        """Ensure a disposable worktree for a Change Set and immutable base."""

    def get_change_workspace(
        self,
        workspace: RuntimeWorkspace,
        change_set_id: str,
        base: GitObjectId,
    ) -> RuntimeChangeWorkspace | None:
        """Return the existing worktree attachment without creating one."""

    def start_worker(
        self,
        change_workspace: RuntimeChangeWorkspace,
        agent_id: str,
        prompt: str,
        *,
        cols: int | None = None,
        rows: int | None = None,
    ) -> RuntimeWorkerSummary:
        """Start/resume a provider worker after WeftMark has authorized the action."""

    def stop_worker(self, change_workspace: RuntimeChangeWorkspace) -> RuntimeWorkerSummary:
        """Stop the provider worker without changing WeftMark lifecycle state."""

    def send_worker_input(self, change_workspace: RuntimeChangeWorkspace, data: str) -> RuntimeWorkerSummary:
        """Send interactive input to a running worker."""

    def worker_summary(self, change_workspace: RuntimeChangeWorkspace) -> RuntimeWorkerSummary:
        """Return operational telemetry for the current/last worker session."""

    def changes(
        self,
        change_workspace: RuntimeChangeWorkspace,
        mode: RuntimeChangesMode = RuntimeChangesMode.WORKING_COPY,
    ) -> RuntimeChanges:
        """Return provider diff metadata for presentation/review assistance."""

    def cleanup_change_workspace(self, change_workspace: RuntimeChangeWorkspace) -> None:
        """Remove provider-owned disposable worktree state after explicit authorization."""
