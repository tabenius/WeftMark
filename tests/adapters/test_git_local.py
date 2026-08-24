from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from weftmark.application.ports.git import GitChangeKind, GitObjectFormat, GitPort
from weftmark.adapters.git_local import (
    GitObjectNotFound,
    GitObservationError,
    LocalGit,
    NotGitRepository,
)


def run(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write(path: Path, relative: str, contents: str) -> None:
    target = path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    run(tmp_path, "init", "--initial-branch=main")
    run(tmp_path, "config", "user.name", "WeftMark Tests")
    run(tmp_path, "config", "user.email", "weftmark@example.invalid")
    write(tmp_path, "README.md", "base\n")
    run(tmp_path, "add", "README.md")
    run(tmp_path, "commit", "-m", "base")
    return tmp_path


def test_adapter_satisfies_port_and_reads_repository_head_commit_and_refs(
    repository: Path,
) -> None:
    adapter = LocalGit(repository)
    identity = adapter.repository()
    head = adapter.head()
    commit = adapter.commit("HEAD")

    assert isinstance(adapter, GitPort)
    assert identity.object_format is GitObjectFormat.SHA1
    assert identity.worktree == str(repository)
    assert identity.git_dir == str(repository / ".git")
    assert not identity.is_bare
    assert head.branch == "main"
    assert commit.id == head.target
    assert adapter.resolve_ref("main").target == head.target
    assert any(ref.name == "refs/heads/main" for ref in adapter.list_refs())


def test_ref_resolution_supports_non_commit_git_objects(repository: Path) -> None:
    blob = run(repository, "hash-object", "README.md")
    run(repository, "tag", "blob-tag", blob)

    resolved = LocalGit(repository).resolve_ref("refs/tags/blob-tag")
    assert str(resolved.target) == blob
    with pytest.raises(GitObjectNotFound, match="commit not found"):
        LocalGit(repository).commit("refs/tags/blob-tag")


def test_diff_reports_add_modify_delete_and_rename(repository: Path) -> None:
    write(repository, "modify.txt", "before\n")
    write(repository, "delete.txt", "delete\n")
    write(repository, "old.txt", "rename me\n")
    run(repository, "add", ".")
    run(repository, "commit", "-m", "fixtures")
    base = run(repository, "rev-parse", "HEAD")

    write(repository, "modify.txt", "after\n")
    (repository / "delete.txt").unlink()
    run(repository, "mv", "old.txt", "new.txt")
    write(repository, "added.txt", "new\n")
    run(repository, "add", ".")
    run(repository, "commit", "-m", "changes")

    diff = LocalGit(repository).diff(base, "HEAD")
    by_path = {entry.path: entry for entry in diff.entries}
    assert by_path["added.txt"].kind is GitChangeKind.ADDED
    assert by_path["modify.txt"].kind is GitChangeKind.MODIFIED
    assert by_path["delete.txt"].kind is GitChangeKind.DELETED
    assert by_path["new.txt"].kind is GitChangeKind.RENAMED
    assert by_path["new.txt"].old_path == "old.txt"


def test_status_preserves_staged_unstaged_untracked_and_conflicted_paths(
    repository: Path,
) -> None:
    write(repository, "staged.txt", "staged\n")
    run(repository, "add", "staged.txt")
    write(repository, "README.md", "dirty\n")
    write(repository, "untracked.txt", "untracked\n")

    status = LocalGit(repository).status()
    assert status.is_dirty
    assert status.staged_paths == ("staged.txt",)
    assert status.unstaged_paths == ("README.md",)
    assert status.untracked_paths == ("untracked.txt",)


def test_status_normalizes_untracked_nested_repository_directory_marker(
    repository: Path,
) -> None:
    nested = repository / "nested"
    nested.mkdir()
    run(nested, "init")

    status = LocalGit(repository).status()

    assert status.untracked_paths == ("nested",)
    assert status.paths == ("nested",)


def test_status_preserves_both_paths_of_a_staged_rename(repository: Path) -> None:
    write(repository, "old.txt", "rename\n")
    run(repository, "add", "old.txt")
    run(repository, "commit", "-m", "rename fixture")
    run(repository, "mv", "old.txt", "new.txt")

    status = LocalGit(repository).status()
    assert status.staged_paths == ("new.txt", "old.txt")
    assert status.paths == ("new.txt", "old.txt")


def test_detached_head_is_explicit(repository: Path) -> None:
    run(repository, "checkout", "--detach", "HEAD")

    head = LocalGit(repository).head()
    assert head.is_detached
    assert head.branch is None


def test_linked_worktree_has_shared_identity_and_distinct_git_dir(
    repository: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    linked = tmp_path_factory.mktemp("linked")
    run(repository, "worktree", "add", "--detach", str(linked), "HEAD")

    primary = LocalGit(repository).repository()
    worktree = LocalGit(linked).repository()
    assert worktree.id == primary.id
    assert worktree.worktree == str(linked)
    assert worktree.git_dir != primary.git_dir


def test_ancestry_and_merge_base_distinguish_related_and_unrelated_history(
    repository: Path,
) -> None:
    base = run(repository, "rev-parse", "HEAD")
    write(repository, "next.txt", "next\n")
    run(repository, "add", ".")
    run(repository, "commit", "-m", "next")
    head = run(repository, "rev-parse", "HEAD")
    adapter = LocalGit(repository)

    assert adapter.is_ancestor(base, head)
    assert not adapter.is_ancestor(head, base)
    assert str(adapter.merge_base(base, head)) == base

    run(repository, "checkout", "--orphan", "unrelated")
    run(repository, "rm", "-rf", ".")
    write(repository, "orphan.txt", "orphan\n")
    run(repository, "add", ".")
    run(repository, "commit", "-m", "orphan")
    assert adapter.merge_base(base, "HEAD") is None


def test_missing_refs_and_non_repository_are_typed(tmp_path: Path) -> None:
    with pytest.raises(NotGitRepository):
        LocalGit(tmp_path).repository()

    run(tmp_path, "init", "--initial-branch=main")
    with pytest.raises(GitObjectNotFound, match="missing"):
        LocalGit(tmp_path).resolve_ref("missing")


def test_revision_arguments_cannot_inject_options(repository: Path) -> None:
    adapter = LocalGit(repository)
    with pytest.raises(GitObservationError, match="must not be an option"):
        adapter.commit("--all")


def test_command_runner_rejects_mutating_subcommands(repository: Path) -> None:
    adapter = LocalGit(repository)
    with pytest.raises(GitObservationError, match="not read-only"):
        adapter._run("reset", "--hard")


def test_timeout_must_be_positive(repository: Path) -> None:
    with pytest.raises(GitObservationError, match="positive"):
        LocalGit(repository, timeout_seconds=0)
