"""Read-only, provider-neutral contract for Git lineage facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class GitContractError(ValueError):
    """Raised when an adapter returns an invalid Git contract value."""


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise GitContractError(f"{name} must not be empty")


def _require_repo_path(name: str, value: str) -> None:
    _require_text(name, value)
    parts = value.split("/")
    if value.startswith("/") or "\x00" in value or any(part in {"", ".", ".."} for part in parts):
        raise GitContractError(f"{name} must be a normalized repository-relative path")


@dataclass(frozen=True, slots=True)
class GitObjectId:
    """A fully resolved Git object identifier (SHA-1 or SHA-256)."""

    value: str

    def __post_init__(self) -> None:
        normalized = self.value.lower()
        if len(normalized) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise GitContractError("object id must be a full SHA-1 or SHA-256 hex value")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value


class GitObjectFormat(StrEnum):
    SHA1 = "sha1"
    SHA256 = "sha256"


@dataclass(frozen=True, slots=True)
class GitRepository:
    """Stable repository identity plus optional local checkout locations."""

    id: str
    object_format: GitObjectFormat
    worktree: str | None = None
    git_dir: str | None = None
    is_bare: bool = False

    def __post_init__(self) -> None:
        _require_text("repository id", self.id)
        if self.worktree is not None:
            _require_text("worktree", self.worktree)
        if self.git_dir is not None:
            _require_text("git_dir", self.git_dir)
        if self.is_bare and self.worktree is not None:
            raise GitContractError("a bare repository cannot expose a worktree")


class GitRefKind(StrEnum):
    LOCAL_BRANCH = "local_branch"
    REMOTE_BRANCH = "remote_branch"
    TAG = "tag"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class GitRef:
    name: str
    target: GitObjectId
    kind: GitRefKind

    def __post_init__(self) -> None:
        _require_text("ref name", self.name)


@dataclass(frozen=True, slots=True)
class GitHead:
    target: GitObjectId
    branch: str | None

    def __post_init__(self) -> None:
        if self.branch is not None:
            _require_text("head branch", self.branch)

    @property
    def is_detached(self) -> bool:
        return self.branch is None


@dataclass(frozen=True, slots=True)
class GitCommit:
    id: GitObjectId
    parents: tuple[GitObjectId, ...]
    committed_at: datetime

    def __post_init__(self) -> None:
        if self.committed_at.tzinfo is None or self.committed_at.utcoffset() is None:
            raise GitContractError("committed_at must include a timezone")
        if len(set(self.parents)) != len(self.parents):
            raise GitContractError("commit parents must not contain duplicates")


class GitChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"
    TYPE_CHANGED = "type_changed"
    UNMERGED = "unmerged"


@dataclass(frozen=True, slots=True)
class GitDiffEntry:
    path: str
    kind: GitChangeKind
    old_path: str | None = None

    def __post_init__(self) -> None:
        _require_repo_path("diff path", self.path)
        if self.old_path is not None:
            _require_repo_path("old diff path", self.old_path)
        if self.kind in {GitChangeKind.RENAMED, GitChangeKind.COPIED}:
            if self.old_path is None:
                raise GitContractError(f"{self.kind.value} entries require old_path")
        elif self.old_path is not None:
            raise GitContractError("old_path is only valid for renamed or copied entries")


@dataclass(frozen=True, slots=True)
class GitDiff:
    base: GitObjectId
    head: GitObjectId
    entries: tuple[GitDiffEntry, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        paths = {
            path
            for entry in self.entries
            for path in (entry.old_path, entry.path)
            if path is not None
        }
        return tuple(sorted(paths))


@dataclass(frozen=True, slots=True)
class GitWorkingTreeStatus:
    staged_paths: tuple[str, ...] = ()
    unstaged_paths: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    conflicted_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "staged_paths",
            "unstaged_paths",
            "untracked_paths",
            "conflicted_paths",
        ):
            paths = getattr(self, field_name)
            if len(set(paths)) != len(paths):
                raise GitContractError(f"{field_name} must not contain duplicates")
            for path in paths:
                _require_repo_path(field_name, path)

    @property
    def is_dirty(self) -> bool:
        return any(
            (
                self.staged_paths,
                self.unstaged_paths,
                self.untracked_paths,
                self.conflicted_paths,
            )
        )

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    path
                    for paths in (
                        self.staged_paths,
                        self.unstaged_paths,
                        self.untracked_paths,
                        self.conflicted_paths,
                    )
                    for path in paths
                }
            )
        )


@runtime_checkable
class GitPort(Protocol):
    """Facts required by Change Set and evidence application services.

    Every operation is observational. Implementations may read a local checkout,
    a bare repository, or another Git transport, but mutation belongs elsewhere.
    Revisions accepted by methods are adapter-resolved Git revision expressions;
    returned identifiers are always complete object IDs.
    """

    def repository(self) -> GitRepository:
        """Return stable repository identity and available checkout context."""

    def head(self) -> GitHead:
        """Return the current commit and branch, or a detached head."""

    def resolve_ref(self, revision: str) -> GitRef:
        """Resolve a revision to a named ref and full object identifier."""

    def list_refs(self) -> tuple[GitRef, ...]:
        """List refs visible to the repository implementation."""

    def commit(self, revision: str) -> GitCommit:
        """Read immutable commit identity, parentage, and commit time."""

    def diff(self, base_revision: str, head_revision: str) -> GitDiff:
        """Return changed repository paths between two revisions."""

    def status(self) -> GitWorkingTreeStatus:
        """Return staged, unstaged, untracked, and conflicted paths."""

    def is_ancestor(self, ancestor_revision: str, descendant_revision: str) -> bool:
        """Return whether the first revision is an ancestor of the second."""

    def merge_base(self, left_revision: str, right_revision: str) -> GitObjectId | None:
        """Return a common ancestor, or None when no common ancestor exists."""
