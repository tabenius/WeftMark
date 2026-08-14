"""Portable, immutable continuation records without credential transfer."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from weftmark.domain.scope import Scope


class HandoffError(ValueError):
    """Raised when a handoff is incomplete, inconsistent, or unsafe."""


_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)
_TOKEN_PREFIX = re.compile(
    r"(?:github_pat_|gh[oprsu]_|(?<![a-z0-9])sk-[a-z0-9]|AKIA[0-9A-Z]{12})",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|credential)\s*[:=]\s*([^\s,;]+)",
    re.IGNORECASE,
)
_URL_CREDENTIAL = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE)
_SAFE_PLACEHOLDERS = frozenset(
    {"<redacted>", "redacted", "***", "xxxxx", "placeholder"}
)


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise HandoffError(f"{name} must not be empty")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HandoffError(f"{name} must include a timezone")


def _contains_secret(value: str) -> bool:
    if _PRIVATE_KEY.search(value) or _TOKEN_PREFIX.search(value) or _URL_CREDENTIAL.search(value):
        return True
    for match in _SECRET_ASSIGNMENT.finditer(value):
        candidate = match.group(1).strip("'\"").casefold()
        if candidate not in _SAFE_PLACEHOLDERS and not candidate.startswith("${"):
            return True
    return False


def _require_object_id(name: str, value: str) -> None:
    normalized = value.casefold()
    if len(normalized) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise HandoffError(f"{name} must be a full Git object id")


@dataclass(frozen=True, slots=True)
class Handoff:
    id: str
    task_id: str
    change_set_id: str
    goal: str
    repository_id: str
    base_sha: str
    head_sha: str
    branch: str
    worktree: str
    source_observation_id: str
    scopes: tuple[Scope, ...]
    evidence_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    known_failures: tuple[str, ...]
    next_action: str
    created_by: str
    created_at: datetime
    intended_receiver_id: str | None = None
    supersedes_id: str | None = None
    generation: int = 1

    def __post_init__(self) -> None:
        for name in (
            "id",
            "task_id",
            "change_set_id",
            "goal",
            "repository_id",
            "branch",
            "worktree",
            "source_observation_id",
            "next_action",
            "created_by",
        ):
            _require_text(name, getattr(self, name))
        if self.intended_receiver_id is not None:
            _require_text("intended_receiver_id", self.intended_receiver_id)
        if self.supersedes_id is not None:
            _require_text("supersedes_id", self.supersedes_id)
        _require_object_id("base_sha", self.base_sha)
        _require_object_id("head_sha", self.head_sha)
        _require_aware("created_at", self.created_at)
        if not self.scopes:
            raise HandoffError("handoff must contain at least one scope")
        if len({scope.identity for scope in self.scopes}) != len(self.scopes):
            raise HandoffError("handoff scopes must not contain duplicates")
        for name, values in (
            ("evidence_ids", self.evidence_ids),
            ("decision_ids", self.decision_ids),
            ("known_failures", self.known_failures),
        ):
            if len(set(values)) != len(values):
                raise HandoffError(f"{name} must not contain duplicates")
            if any(not value or not value.strip() for value in values):
                raise HandoffError(f"{name} must not contain empty values")
        if self.generation < 1:
            raise HandoffError("generation must be positive")
        if (self.generation == 1) != (self.supersedes_id is None):
            raise HandoffError("generation and supersedes_id are inconsistent")

        text_values = (
            self.id,
            self.task_id,
            self.change_set_id,
            self.goal,
            self.repository_id,
            self.branch,
            self.worktree,
            self.source_observation_id,
            *(scope.canonical for scope in self.scopes),
            *self.evidence_ids,
            *self.decision_ids,
            *self.known_failures,
            self.next_action,
            self.created_by,
            self.intended_receiver_id or "",
        )
        if any(_contains_secret(value) for value in text_values):
            raise HandoffError("handoff appears to contain credential or secret material")

    def supersede(
        self,
        *,
        id: str,
        created_by: str,
        created_at: datetime,
        source_observation_id: str,
        base_sha: str | None = None,
        head_sha: str | None = None,
        branch: str | None = None,
        worktree: str | None = None,
        scopes: tuple[Scope, ...] | None = None,
        evidence_ids: tuple[str, ...] | None = None,
        decision_ids: tuple[str, ...] | None = None,
        known_failures: tuple[str, ...] | None = None,
        next_action: str | None = None,
        intended_receiver_id: str | None = None,
    ) -> Handoff:
        _require_aware("created_at", created_at)
        if created_at <= self.created_at:
            raise HandoffError("superseding handoff must be created later")
        return replace(
            self,
            id=id,
            base_sha=self.base_sha if base_sha is None else base_sha,
            head_sha=self.head_sha if head_sha is None else head_sha,
            branch=self.branch if branch is None else branch,
            worktree=self.worktree if worktree is None else worktree,
            source_observation_id=source_observation_id,
            scopes=self.scopes if scopes is None else scopes,
            evidence_ids=self.evidence_ids if evidence_ids is None else evidence_ids,
            decision_ids=self.decision_ids if decision_ids is None else decision_ids,
            known_failures=(
                self.known_failures if known_failures is None else known_failures
            ),
            next_action=self.next_action if next_action is None else next_action,
            created_by=created_by,
            created_at=created_at,
            intended_receiver_id=(
                intended_receiver_id or self.intended_receiver_id
            ),
            supersedes_id=self.id,
            generation=self.generation + 1,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a serialization-safe record for file, CLI, or MCP adapters."""

        return {
            "id": self.id,
            "task_id": self.task_id,
            "change_set_id": self.change_set_id,
            "goal": self.goal,
            "repository_id": self.repository_id,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "branch": self.branch,
            "worktree": self.worktree,
            "source_observation_id": self.source_observation_id,
            "scopes": [scope.to_dict() for scope in self.scopes],
            "evidence_ids": list(self.evidence_ids),
            "decision_ids": list(self.decision_ids),
            "known_failures": list(self.known_failures),
            "next_action": self.next_action,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "intended_receiver_id": self.intended_receiver_id,
            "supersedes_id": self.supersedes_id,
            "generation": self.generation,
        }
