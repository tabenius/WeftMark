"""Read-only local subprocess implementation of the Git application port."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from weftmark.application.ports.git import (
    GitChangeKind,
    GitCommit,
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


class LocalGitError(RuntimeError):
    """Base class for local Git observation failures."""


class NotGitRepository(LocalGitError):
    """Raised when the configured path is not inside a Git repository."""


class GitObjectNotFound(LocalGitError):
    """Raised when a requested revision cannot be resolved."""


class GitObservationError(LocalGitError):
    """Raised when a read-only Git operation fails."""


_READ_ONLY_SUBCOMMANDS = frozenset(
    {"diff", "for-each-ref", "merge-base", "rev-parse", "show", "status"}
)


def _require_revision(revision: str) -> str:
    if not revision or not revision.strip() or revision.startswith("-") or "\x00" in revision:
        raise GitObservationError("revision must be non-empty and must not be an option")
    return revision


def _normalize_untracked_path(path: str) -> str:
    """Remove only Git's terminal marker for an untracked directory."""

    return path[:-1] if path.endswith("/") else path


def _ref_kind(name: str) -> GitRefKind:
    if name.startswith("refs/heads/"):
        return GitRefKind.LOCAL_BRANCH
    if name.startswith("refs/remotes/"):
        return GitRefKind.REMOTE_BRANCH
    if name.startswith("refs/tags/"):
        return GitRefKind.TAG
    return GitRefKind.OTHER


class LocalGit(GitPort):
    """Observe a local checkout or bare repository without mutating it."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 30.0) -> None:
        self._path = Path(path).resolve()
        if timeout_seconds <= 0:
            raise GitObservationError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def repository(self) -> GitRepository:
        bare = self._run("rev-parse", "--is-bare-repository").strip() == "true"
        git_dir = self._absolute_path(
            self._run("rev-parse", "--absolute-git-dir").strip()
        )
        common_dir = self._absolute_path(
            self._run("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        )
        object_format = GitObjectFormat(
            self._run("rev-parse", "--show-object-format").strip()
        )
        worktree = None
        if not bare:
            worktree = self._absolute_path(
                self._run("rev-parse", "--show-toplevel").strip()
            )
        return GitRepository(
            id=f"git:{common_dir}",
            object_format=object_format,
            worktree=worktree,
            git_dir=git_dir,
            is_bare=bare,
        )

    def head(self) -> GitHead:
        target = self._resolve_commit("HEAD")
        result = self._run_result(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "HEAD"
        )
        branch = result.stdout.strip() if result.returncode == 0 else ""
        return GitHead(target, None if branch in {"", "HEAD"} else branch)

    def resolve_ref(self, revision: str) -> GitRef:
        revision = _require_revision(revision)
        target = self._resolve_revision(revision)
        symbolic = self._run_result(
            "rev-parse", "--symbolic-full-name", "--verify", revision
        )
        name = symbolic.stdout.strip() if symbolic.returncode == 0 else ""
        resolved_name = name or revision
        return GitRef(resolved_name, target, _ref_kind(resolved_name))

    def list_refs(self) -> tuple[GitRef, ...]:
        output = self._run(
            "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/"
        )
        refs: list[GitRef] = []
        for line in output.splitlines():
            if not line:
                continue
            name, target = line.split("\x00", 1)
            refs.append(GitRef(name, GitObjectId(target), _ref_kind(name)))
        return tuple(refs)

    def commit(self, revision: str) -> GitCommit:
        revision = _require_revision(revision)
        object_id = self._resolve_commit(revision)
        output = self._run_object(
            "show", "-s", "--format=%H%x00%P%x00%cI", str(object_id), "--"
        ).strip()
        object_id, parents, committed_at = output.split("\x00", 2)
        return GitCommit(
            GitObjectId(object_id),
            tuple(GitObjectId(parent) for parent in parents.split() if parent),
            datetime.fromisoformat(committed_at),
        )

    def diff(self, base_revision: str, head_revision: str) -> GitDiff:
        base_revision = _require_revision(base_revision)
        head_revision = _require_revision(head_revision)
        base = self._resolve_commit(base_revision)
        head = self._resolve_commit(head_revision)
        output = self._run_object(
            "diff",
            "--name-status",
            "-z",
            "--no-ext-diff",
            "--find-renames",
            "--find-copies",
            base_revision,
            head_revision,
            "--",
        )
        return GitDiff(base, head, self._parse_diff(output))

    def status(self) -> GitWorkingTreeStatus:
        output = self._run(
            "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []
        conflicted: list[str] = []
        records = iter(output.split("\x00"))
        for record in records:
            if not record:
                continue
            code, path = record[:2], record[3:]
            if code == "??":
                untracked.append(_normalize_untracked_path(path))
                continue
            if code in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
                conflicted.append(path)
                continue
            source_path = next(records, None) if code[0] in {"R", "C"} else None
            if code[0] not in {" ", "?"}:
                staged.append(path)
                if source_path:
                    staged.append(source_path)
            if code[1] not in {" ", "?"}:
                unstaged.append(path)
        return GitWorkingTreeStatus(
            tuple(staged), tuple(unstaged), tuple(untracked), tuple(conflicted)
        )

    def is_ancestor(self, ancestor_revision: str, descendant_revision: str) -> bool:
        ancestor_revision = _require_revision(ancestor_revision)
        descendant_revision = _require_revision(descendant_revision)
        result = self._run_result(
            "merge-base", "--is-ancestor", ancestor_revision, descendant_revision
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise self._error(result)

    def merge_base(self, left_revision: str, right_revision: str) -> GitObjectId | None:
        left_revision = _require_revision(left_revision)
        right_revision = _require_revision(right_revision)
        result = self._run_result("merge-base", left_revision, right_revision)
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            raise self._error(result)
        return GitObjectId(result.stdout.strip())

    def _resolve_revision(self, revision: str) -> GitObjectId:
        result = self._run_result(
            "rev-parse", "--verify", "--end-of-options", revision
        )
        if result.returncode != 0:
            raise GitObjectNotFound(f"Git revision not found: {revision}")
        return GitObjectId(result.stdout.strip())

    def _resolve_commit(self, revision: str) -> GitObjectId:
        result = self._run_result(
            "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"
        )
        if result.returncode != 0:
            raise GitObjectNotFound(f"Git commit not found: {revision}")
        return GitObjectId(result.stdout.strip())

    def _parse_diff(self, output: str) -> tuple[GitDiffEntry, ...]:
        tokens = iter(output.split("\x00"))
        entries: list[GitDiffEntry] = []
        for status in tokens:
            if not status:
                continue
            kind = _change_kind(status[0])
            first_path = next(tokens)
            if kind in {GitChangeKind.RENAMED, GitChangeKind.COPIED}:
                entries.append(GitDiffEntry(next(tokens), kind, old_path=first_path))
            else:
                entries.append(GitDiffEntry(first_path, kind))
        return tuple(entries)

    def _absolute_path(self, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            path = self._path / path
        return str(path.resolve())

    def _run_object(self, subcommand: str, *args: str) -> str:
        result = self._run_result(subcommand, *args)
        if result.returncode != 0:
            raise self._error(result)
        return result.stdout

    def _run(self, subcommand: str, *args: str) -> str:
        result = self._run_result(subcommand, *args)
        if result.returncode != 0:
            raise self._error(result)
        return result.stdout

    def _run_result(self, subcommand: str, *args: str) -> subprocess.CompletedProcess[str]:
        if subcommand not in _READ_ONLY_SUBCOMMANDS:
            raise GitObservationError(f"Git subcommand is not read-only: {subcommand}")
        try:
            return subprocess.run(
                (
                    "git",
                    "--no-pager",
                    "--no-optional-locks",
                    "-C",
                    str(self._path),
                    subcommand,
                    *args,
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as error:
            raise GitObservationError("Git executable was not found") from error
        except subprocess.TimeoutExpired as error:
            raise GitObservationError("Git observation timed out") from error

    def _error(self, result: subprocess.CompletedProcess[str]) -> LocalGitError:
        detail = result.stderr.strip() or "Git observation failed"
        if "not a git repository" in detail.lower():
            return NotGitRepository(detail)
        return GitObservationError(detail)


def _change_kind(code: str) -> GitChangeKind:
    kinds = {
        "A": GitChangeKind.ADDED,
        "M": GitChangeKind.MODIFIED,
        "D": GitChangeKind.DELETED,
        "R": GitChangeKind.RENAMED,
        "C": GitChangeKind.COPIED,
        "T": GitChangeKind.TYPE_CHANGED,
        "U": GitChangeKind.UNMERGED,
    }
    try:
        return kinds[code]
    except KeyError as error:
        raise GitObservationError(f"unsupported Git change status: {code}") from error
