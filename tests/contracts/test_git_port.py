from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from weftmark.application.ports import git
from weftmark.application.ports.git import (
    GitChangeKind,
    GitCommit,
    GitContractError,
    GitDiff,
    GitDiffEntry,
    GitHead,
    GitObjectFormat,
    GitObjectId,
    GitPort,
    GitRef,
    GitRefKind,
    GitRepository,
    GitWorkingTreeStatus,
)


SHA_A = GitObjectId("a" * 40)
SHA_B = GitObjectId("b" * 40)
NOW = datetime(2026, 8, 13, 23, 15, tzinfo=timezone.utc)


class InMemoryGit:
    """Contract fixture showing that the port needs no subprocess or forge types."""

    def repository(self) -> GitRepository:
        return GitRepository("repo-1", GitObjectFormat.SHA1, worktree="/src/repo")

    def head(self) -> GitHead:
        return GitHead(SHA_B, "main")

    def resolve_ref(self, revision: str) -> GitRef:
        return GitRef(revision, SHA_B, GitRefKind.LOCAL_BRANCH)

    def list_refs(self) -> tuple[GitRef, ...]:
        return (self.resolve_ref("refs/heads/main"),)

    def commit(self, revision: str) -> GitCommit:
        return GitCommit(SHA_B, (SHA_A,), NOW)

    def diff(self, base_revision: str, head_revision: str) -> GitDiff:
        return GitDiff(
            SHA_A,
            SHA_B,
            (GitDiffEntry("src/weftmark.py", GitChangeKind.MODIFIED),),
        )

    def status(self) -> GitWorkingTreeStatus:
        return GitWorkingTreeStatus()

    def is_ancestor(self, ancestor_revision: str, descendant_revision: str) -> bool:
        return ancestor_revision == str(SHA_A) and descendant_revision == str(SHA_B)

    def merge_base(self, left_revision: str, right_revision: str) -> GitObjectId | None:
        return SHA_A


def test_structural_port_supports_repository_refs_commits_diff_status_and_ancestry() -> None:
    adapter = InMemoryGit()

    assert isinstance(adapter, GitPort)
    assert adapter.repository().id == "repo-1"
    assert adapter.resolve_ref("refs/heads/main").target == SHA_B
    assert adapter.list_refs()[0].kind is GitRefKind.LOCAL_BRANCH
    assert adapter.commit("HEAD").parents == (SHA_A,)
    assert adapter.diff(str(SHA_A), str(SHA_B)).paths == ("src/weftmark.py",)
    assert not adapter.status().is_dirty
    assert adapter.is_ancestor(str(SHA_A), str(SHA_B))
    assert adapter.merge_base("main", "topic") == SHA_A


def test_repository_identity_supports_checkouts_bare_and_nonlocal_adapters() -> None:
    checkout = GitRepository(
        "repo-local",
        GitObjectFormat.SHA1,
        worktree="/src/repo",
        git_dir="/src/repo/.git",
    )
    bare = GitRepository(
        "repo-bare", GitObjectFormat.SHA256, git_dir="/srv/repo.git", is_bare=True
    )
    nonlocal_repository = GitRepository("repo-remote", GitObjectFormat.SHA1)

    assert checkout.worktree == "/src/repo"
    assert bare.worktree is None
    assert nonlocal_repository.git_dir is None
    with pytest.raises(GitContractError, match="bare repository"):
        GitRepository(
            "bad", GitObjectFormat.SHA1, worktree="/src/repo", is_bare=True
        )


def test_head_represents_attached_and_detached_states_without_guessing() -> None:
    assert not GitHead(SHA_A, "main").is_detached
    assert GitHead(SHA_A, None).is_detached


def test_diff_preserves_both_sides_of_rename_for_scope_audits() -> None:
    diff = GitDiff(
        SHA_A,
        SHA_B,
        (
            GitDiffEntry(
                "docs/current.md", GitChangeKind.RENAMED, old_path="docs/old.md"
            ),
            GitDiffEntry("src/new.py", GitChangeKind.ADDED),
        ),
    )

    assert diff.paths == ("docs/current.md", "docs/old.md", "src/new.py")
    with pytest.raises(GitContractError, match="require old_path"):
        GitDiffEntry("docs/current.md", GitChangeKind.RENAMED)


def test_dirty_state_keeps_categories_and_exposes_deduplicated_paths() -> None:
    status = GitWorkingTreeStatus(
        staged_paths=("src/a.py",),
        unstaged_paths=("src/a.py", "src/b.py"),
        untracked_paths=("notes/todo.md",),
        conflicted_paths=("src/conflict.py",),
    )

    assert status.is_dirty
    assert status.paths == (
        "notes/todo.md",
        "src/a.py",
        "src/b.py",
        "src/conflict.py",
    )


@pytest.mark.parametrize("value", ("abc", "g" * 40, "", "a" * 41))
def test_object_identifiers_must_be_full_supported_hashes(value: str) -> None:
    with pytest.raises(GitContractError, match="full SHA-1 or SHA-256"):
        GitObjectId(value)


@pytest.mark.parametrize("path", ("/etc/passwd", "../secret", "src//file.py", ""))
def test_changed_paths_must_be_normalized_and_repository_relative(path: str) -> None:
    with pytest.raises(GitContractError, match="empty|repository-relative"):
        GitDiffEntry(path, GitChangeKind.MODIFIED)


def test_contract_values_are_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        SHA_A.value = "b" * 40  # type: ignore[misc]


def test_git_port_contains_no_code_forge_or_change_request_vocabulary() -> None:
    source = inspect.getsource(git).lower()
    forbidden = ("github", "gitlab", "pull_request", "merge_request")

    assert all(term not in source for term in forbidden)
