"""Bind immutable Change Sets to append-only observations from a Git port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from weftmark.application.ports.git import GitHead, GitPort, GitRepository
from weftmark.domain.changeset import ChangeSet, ChangeSetState
from weftmark.domain.evidence import Evidence, EvidenceState, SubjectKind


class ChangeBindingError(ValueError):
    """Raised when live Git context cannot safely bind to a Change Set."""


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ChangeBindingError(f"{name} must include a timezone")


@dataclass(frozen=True, slots=True)
class GitLineageObservation:
    id: str
    repository_id: str
    base_revision: str
    base_sha: str
    head_sha: str
    branch: str
    worktree: str
    changed_paths: tuple[str, ...]
    dirty_paths: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "id",
            "repository_id",
            "base_revision",
            "base_sha",
            "head_sha",
            "branch",
            "worktree",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ChangeBindingError(f"{name} must not be empty")
        _require_aware("observed_at", self.observed_at)
        if len(set(self.changed_paths)) != len(self.changed_paths):
            raise ChangeBindingError("changed_paths must not contain duplicates")
        if len(set(self.dirty_paths)) != len(self.dirty_paths):
            raise ChangeBindingError("dirty_paths must not contain duplicates")


@dataclass(frozen=True, slots=True)
class ChangeBinding:
    change_set: ChangeSet
    base_revision: str
    observations: tuple[GitLineageObservation, ...]

    def __post_init__(self) -> None:
        if not self.base_revision or not self.base_revision.strip():
            raise ChangeBindingError("base_revision must not be empty")
        if not self.observations:
            raise ChangeBindingError("binding must contain an observation")
        if any(
            observation.repository_id != self.change_set.repository_id
            for observation in self.observations
        ):
            raise ChangeBindingError("observation repository identity changed")
        if len({observation.id for observation in self.observations}) != len(
            self.observations
        ):
            raise ChangeBindingError("observation ids must be unique")
        if any(
            current.observed_at < previous.observed_at
            for previous, current in zip(self.observations, self.observations[1:])
        ):
            raise ChangeBindingError("observations must be chronological")
        latest = self.observations[-1]
        if self.base_revision != latest.base_revision:
            raise ChangeBindingError("base_revision does not match latest observation")
        expected = (
            self.change_set.base_sha,
            self.change_set.head_sha,
            self.change_set.branch,
            str(Path(self.change_set.worktree).resolve()),
        )
        observed = (
            latest.base_sha,
            latest.head_sha,
            latest.branch,
            str(Path(latest.worktree).resolve()),
        )
        if expected != observed:
            raise ChangeBindingError("latest observation does not match Change Set lineage")

    @property
    def latest(self) -> GitLineageObservation:
        return self.observations[-1]

    def obsolete_evidence_ids(self, evidence: Iterable[Evidence]) -> tuple[str, ...]:
        """Return current Change Set evidence not bound to the latest head."""

        ids = {
            record.id
            for record in evidence
            if record.subject.kind is SubjectKind.CHANGE_SET
            and record.subject.id == self.change_set.id
            and record.state is not EvidenceState.SUPERSEDED
            and record.bound_commit_sha != self.latest.head_sha
        }
        return tuple(sorted(ids))


class ChangeBindingService:
    def __init__(self, git: GitPort) -> None:
        self._git = git

    def create(
        self,
        change_set: ChangeSet,
        *,
        base_revision: str,
        observed_at: datetime,
    ) -> ChangeBinding:
        if change_set.state is not ChangeSetState.PLANNED:
            raise ChangeBindingError("only a planned Change Set can create a binding")
        observation = self._observe(
            change_set,
            base_revision=base_revision,
            sequence=1,
            observed_at=observed_at,
        )
        if change_set.base_sha != observation.base_sha:
            raise ChangeBindingError("planned base SHA does not match observed Git base")
        if change_set.branch != observation.branch:
            raise ChangeBindingError("planned branch does not match observed Git branch")
        active = change_set.activate(
            head_sha=observation.head_sha,
            at=observed_at,
        )
        return ChangeBinding(active, base_revision, (observation,))

    def refresh(
        self,
        binding: ChangeBinding,
        *,
        observed_at: datetime,
        base_revision: str | None = None,
    ) -> ChangeBinding:
        revision = base_revision or binding.base_revision
        observation = self._observe(
            binding.change_set,
            base_revision=revision,
            sequence=len(binding.observations) + 1,
            observed_at=observed_at,
        )
        change_set = binding.change_set
        base_changed = observation.base_sha != change_set.base_sha
        branch_changed = observation.branch != change_set.branch
        head_changed = observation.head_sha != change_set.head_sha

        if base_changed:
            change_set = change_set.rebase(
                observation.base_sha,
                head_sha=observation.head_sha,
                at=observed_at,
            )
        if branch_changed:
            change_set = change_set.move_branch(
                observation.branch,
                head_sha=observation.head_sha,
                at=observed_at,
            )
        elif head_changed and not base_changed:
            change_set = change_set.advance_head(
                observation.head_sha,
                at=observed_at,
            )

        return ChangeBinding(
            change_set,
            revision,
            (*binding.observations, observation),
        )

    def _observe(
        self,
        change_set: ChangeSet,
        *,
        base_revision: str,
        sequence: int,
        observed_at: datetime,
    ) -> GitLineageObservation:
        _require_aware("observed_at", observed_at)
        repository = self._git.repository()
        head = self._git.head()
        self._validate_context(change_set, repository, head)
        base = self._git.commit(base_revision).id
        diff = self._git.diff(str(base), str(head.target))
        status = self._git.status()
        assert repository.worktree is not None
        assert head.branch is not None
        return GitLineageObservation(
            id=f"{change_set.id}:git:{sequence}",
            repository_id=repository.id,
            base_revision=base_revision,
            base_sha=str(base),
            head_sha=str(head.target),
            branch=head.branch,
            worktree=repository.worktree,
            changed_paths=diff.paths,
            dirty_paths=status.paths,
            observed_at=observed_at,
        )

    def _validate_context(
        self,
        change_set: ChangeSet,
        repository: GitRepository,
        head: GitHead,
    ) -> None:
        if repository.id != change_set.repository_id:
            raise ChangeBindingError("repository identity does not match Change Set")
        if repository.worktree is None:
            raise ChangeBindingError("Change Set binding requires a working tree")
        if Path(repository.worktree).resolve() != Path(change_set.worktree).resolve():
            raise ChangeBindingError("worktree does not match Change Set")
        if head.is_detached:
            raise ChangeBindingError("detached HEAD cannot supply a Change Set branch")
