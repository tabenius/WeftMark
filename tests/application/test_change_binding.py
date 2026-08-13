from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import pytest

from weftmark.application.change_binding import (
    ChangeBindingError,
    ChangeBindingService,
)
from weftmark.adapters.git_local import LocalGit
from weftmark.application.ports.git import (
    GitChangeKind,
    GitCommit,
    GitDiff,
    GitDiffEntry,
    GitHead,
    GitObjectFormat,
    GitObjectId,
    GitRef,
    GitRefKind,
    GitRepository,
    GitWorkingTreeStatus,
)
from weftmark.domain.changeset import ChangeSet, LineageEventKind
from weftmark.domain.evidence import (
    Evidence,
    EvidenceKind,
    EvidenceProducer,
    EvidenceSubject,
    ProducerKind,
    SubjectKind,
)


NOW = datetime(2026, 8, 13, 23, 30, tzinfo=timezone.utc)
BASE = GitObjectId("a" * 40)
HEAD = GitObjectId("b" * 40)


class FakeGit:
    def __init__(self) -> None:
        self.repository_value = GitRepository(
            "repo-1",
            GitObjectFormat.SHA1,
            worktree="/work/repo",
            git_dir="/work/repo/.git",
        )
        self.head_value = GitHead(HEAD, "feature")
        self.base = BASE
        self.entries = (GitDiffEntry("src/a.py", GitChangeKind.MODIFIED),)
        self.status_value = GitWorkingTreeStatus(untracked_paths=("notes.txt",))

    def repository(self) -> GitRepository:
        return self.repository_value

    def head(self) -> GitHead:
        return self.head_value

    def resolve_ref(self, revision: str) -> GitRef:
        return GitRef(revision, self.base, GitRefKind.OTHER)

    def list_refs(self) -> tuple[GitRef, ...]:
        return ()

    def commit(self, revision: str) -> GitCommit:
        return GitCommit(self.base, (), NOW)

    def diff(self, base_revision: str, head_revision: str) -> GitDiff:
        return GitDiff(self.base, self.head_value.target, self.entries)

    def status(self) -> GitWorkingTreeStatus:
        return self.status_value

    def is_ancestor(self, ancestor_revision: str, descendant_revision: str) -> bool:
        return True

    def merge_base(self, left_revision: str, right_revision: str) -> GitObjectId | None:
        return self.base


def planned() -> ChangeSet:
    return ChangeSet.plan(
        id="chg-1",
        goal="Bind exact source state",
        repository_id="repo-1",
        base_sha=str(BASE),
        branch="feature",
        worktree="/work/repo",
        scopes=("src/**",),
        at=NOW,
    )


def create(git: FakeGit | None = None):
    adapter = git or FakeGit()
    binding = ChangeBindingService(adapter).create(
        planned(), base_revision="main", observed_at=NOW + timedelta(seconds=1)
    )
    return adapter, binding


def test_create_activates_change_set_and_records_exact_git_context() -> None:
    _, binding = create()

    assert binding.change_set.head_sha == str(HEAD)
    assert binding.change_set.lineage[-1].kind is LineageEventKind.ACTIVATED
    assert binding.latest.base_revision == "main"
    assert binding.latest.changed_paths == ("src/a.py",)
    assert binding.latest.dirty_paths == ("notes.txt",)


def test_moved_head_appends_observation_and_lineage_event() -> None:
    git, binding = create()
    next_head = GitObjectId("c" * 40)
    git.head_value = GitHead(next_head, "feature")

    refreshed = ChangeBindingService(git).refresh(
        binding, observed_at=NOW + timedelta(seconds=2)
    )
    assert len(refreshed.observations) == 2
    assert refreshed.observations[0] == binding.observations[0]
    assert refreshed.latest.head_sha == str(next_head)
    assert refreshed.change_set.lineage[-1].kind is LineageEventKind.HEAD_ADVANCED


def test_rebase_and_branch_movement_are_explicit_events() -> None:
    git, binding = create()
    git.base = GitObjectId("c" * 40)
    git.head_value = GitHead(GitObjectId("d" * 40), "feature-v2")

    refreshed = ChangeBindingService(git).refresh(
        binding,
        base_revision="trunk",
        observed_at=NOW + timedelta(seconds=2),
    )
    kinds = tuple(event.kind for event in refreshed.change_set.lineage)
    assert kinds[-2:] == (LineageEventKind.REBASED, LineageEventKind.BRANCH_MOVED)
    assert refreshed.base_revision == "trunk"
    assert refreshed.change_set.base_sha == "c" * 40
    assert refreshed.change_set.head_sha == "d" * 40


def test_unchanged_refresh_still_records_observation_without_fake_lineage() -> None:
    git, binding = create()

    refreshed = ChangeBindingService(git).refresh(
        binding, observed_at=NOW + timedelta(seconds=2)
    )
    assert len(refreshed.observations) == 2
    assert refreshed.change_set.lineage == binding.change_set.lineage


@pytest.mark.parametrize("mismatch", ("repository", "worktree", "branch", "base"))
def test_initial_binding_rejects_mismatched_planned_context(mismatch: str) -> None:
    git = FakeGit()
    change_set = planned()
    if mismatch == "repository":
        git.repository_value = GitRepository(
            "other", GitObjectFormat.SHA1, worktree="/work/repo"
        )
    elif mismatch == "worktree":
        git.repository_value = GitRepository(
            "repo-1", GitObjectFormat.SHA1, worktree="/work/other"
        )
    elif mismatch == "branch":
        git.head_value = GitHead(HEAD, "other")
    else:
        git.base = GitObjectId("f" * 40)

    with pytest.raises(ChangeBindingError, match=mismatch):
        ChangeBindingService(git).create(
            change_set, base_revision="main", observed_at=NOW + timedelta(seconds=1)
        )


def test_detached_head_and_bare_repository_fail_closed() -> None:
    git = FakeGit()
    git.head_value = GitHead(HEAD, None)
    with pytest.raises(ChangeBindingError, match="detached"):
        ChangeBindingService(git).create(
            planned(), base_revision="main", observed_at=NOW + timedelta(seconds=1)
        )

    git = FakeGit()
    git.repository_value = GitRepository(
        "repo-1", GitObjectFormat.SHA1, git_dir="/work/repo.git", is_bare=True
    )
    with pytest.raises(ChangeBindingError, match="working tree"):
        ChangeBindingService(git).create(
            planned(), base_revision="main", observed_at=NOW + timedelta(seconds=1)
        )


def test_obsolete_or_unbound_change_set_evidence_is_identified() -> None:
    _, binding = create()
    producer = EvidenceProducer(ProducerKind.CI, "local")
    subject = EvidenceSubject(SubjectKind.CHANGE_SET, "chg-1")

    def evidence(id: str, sha: str | None) -> Evidence:
        return Evidence.declare(
            id=id,
            kind=EvidenceKind.TEST,
            producer=producer,
            subject=subject,
            bound_commit_sha=sha,
            at=NOW,
        )

    records = (
        evidence("current", str(HEAD)),
        evidence("obsolete", str(BASE)),
        evidence("unbound", None),
        Evidence.declare(
            id="other-subject",
            kind=EvidenceKind.TEST,
            producer=producer,
            subject=EvidenceSubject(SubjectKind.CHANGE_SET, "chg-2"),
            bound_commit_sha=str(BASE),
            at=NOW,
        ),
    )
    assert binding.obsolete_evidence_ids(records) == ("obsolete", "unbound")


def test_observation_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(ChangeBindingError, match="timezone"):
        ChangeBindingService(FakeGit()).create(
            planned(),
            base_revision="main",
            observed_at=NOW.replace(tzinfo=None),
        )


def test_service_binds_a_real_local_repository(tmp_path: Path) -> None:
    subprocess.run(
        ("git", "-C", str(tmp_path), "init", "--initial-branch=main"),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "-C", str(tmp_path), "config", "user.name", "WeftMark Tests"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(tmp_path),
            "config",
            "user.email",
            "weftmark@example.invalid",
        ),
        check=True,
    )
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(tmp_path), "add", "README.md"), check=True)
    subprocess.run(
        ("git", "-C", str(tmp_path), "commit", "-m", "base"),
        check=True,
        capture_output=True,
    )
    adapter = LocalGit(tmp_path)
    repository = adapter.repository()
    head = adapter.head()
    change_set = ChangeSet.plan(
        id="chg-real",
        goal="Observe a real checkout",
        repository_id=repository.id,
        base_sha=str(head.target),
        branch="main",
        worktree=str(tmp_path),
        at=NOW,
    )

    binding = ChangeBindingService(adapter).create(
        change_set,
        base_revision="HEAD",
        observed_at=NOW + timedelta(seconds=1),
    )
    assert binding.latest.changed_paths == ()
    assert binding.latest.dirty_paths == ()
    assert binding.change_set.repository_id == repository.id
