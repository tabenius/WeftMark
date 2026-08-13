from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from weftmark.domain.handoff import Handoff, HandoffError
from weftmark.domain.scope import Scope


NOW = datetime(2026, 8, 14, 0, 30, tzinfo=timezone.utc)


def handoff(**changes: object) -> Handoff:
    values = {
        "id": "handoff-1",
        "task_id": "review-service",
        "change_set_id": "chg-1",
        "goal": "Continue the reviewer workflow",
        "repository_id": "git:/work/repo/.git",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "branch": "feature/review",
        "worktree": "/work/repo",
        "source_observation_id": "chg-1:git:1",
        "scopes": (Scope.file("src/**"), Scope.contract("review-v0")),
        "evidence_ids": ("ev-tests",),
        "decision_ids": ("decision-1",),
        "known_failures": ("documentation example still needs review",),
        "next_action": "Run the CLI acceptance test",
        "created_by": "worker-1",
        "created_at": NOW,
        "intended_receiver_id": "worker-2",
    }
    values.update(changes)
    return Handoff(**values)  # type: ignore[arg-type]


def test_handoff_carries_exact_continuation_context_without_chat() -> None:
    record = handoff()

    assert record.task_id == "review-service"
    assert record.change_set_id == "chg-1"
    assert record.repository_id == "git:/work/repo/.git"
    assert record.base_sha == "a" * 40
    assert record.head_sha == "b" * 40
    assert record.branch == "feature/review"
    assert record.worktree == "/work/repo"
    assert record.scopes[1] == Scope.contract("review-v0")
    assert record.next_action == "Run the CLI acceptance test"


def test_handoff_is_immutable_and_serialization_safe() -> None:
    record = handoff()
    with pytest.raises(FrozenInstanceError):
        record.head_sha = "c" * 40  # type: ignore[misc]

    serialized = record.to_dict()
    assert serialized["head_sha"] == "b" * 40
    assert serialized["scopes"][1] == {"kind": "contract", "key": "review-v0"}
    assert serialized["created_at"] == NOW.isoformat()


def test_superseding_handoff_preserves_chain_and_updates_snapshot() -> None:
    first = handoff()
    second = first.supersede(
        id="handoff-2",
        created_by="worker-2",
        created_at=NOW + timedelta(minutes=5),
        source_observation_id="chg-1:git:2",
        head_sha="c" * 40,
        evidence_ids=("ev-tests-2",),
        known_failures=(),
        next_action="Request final review",
        intended_receiver_id="human-reviewer",
    )

    assert first.head_sha == "b" * 40
    assert second.head_sha == "c" * 40
    assert second.supersedes_id == first.id
    assert second.generation == 2
    assert second.created_by == "worker-2"
    assert second.evidence_ids == ("ev-tests-2",)


def test_supersedes_chain_cannot_move_backward_or_lose_source_observation() -> None:
    first = handoff()
    with pytest.raises(HandoffError, match="created later"):
        first.supersede(
            id="handoff-2",
            created_by="worker-2",
            created_at=NOW,
            source_observation_id="chg-1:git:2",
        )
    with pytest.raises(HandoffError, match="source_observation_id"):
        first.supersede(
            id="handoff-2",
            created_by="worker-2",
            created_at=NOW + timedelta(minutes=1),
            source_observation_id="",
        )


@pytest.mark.parametrize(
    "unsafe",
    (
        "password=hunter2",
        "API_TOKEN: abcdef123456",
        "github_pat_1234567890abcdef",
        "postgres://alice:secretvalue@database/repo",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    ),
)
def test_handoff_refuses_credentials_and_secret_material(unsafe: str) -> None:
    with pytest.raises(HandoffError, match="credential or secret"):
        handoff(next_action=unsafe)


def test_handoff_refuses_secret_material_disguised_as_scope_identity() -> None:
    with pytest.raises(HandoffError, match="credential or secret"):
        handoff(scopes=(Scope.contract("github_pat_1234567890abcdef"),))


def test_secret_placeholders_and_key_names_without_values_are_allowed() -> None:
    record = handoff(
        known_failures=("AUTH_SECRET is missing",),
        next_action="Set token=<redacted> through the operator secret store",
    )
    assert record.known_failures == ("AUTH_SECRET is missing",)


def test_invalid_git_context_and_duplicate_references_fail_closed() -> None:
    with pytest.raises(HandoffError, match="full Git object"):
        handoff(head_sha="abc")
    with pytest.raises(HandoffError, match="evidence_ids"):
        handoff(evidence_ids=("ev-1", "ev-1"))
    with pytest.raises(HandoffError, match="scopes"):
        handoff(scopes=(Scope.file("src/**"), Scope.file("src/**")))


def test_generation_and_supersedes_identity_must_agree() -> None:
    with pytest.raises(HandoffError, match="inconsistent"):
        handoff(generation=2, supersedes_id=None)
    with pytest.raises(HandoffError, match="inconsistent"):
        handoff(generation=1, supersedes_id="handoff-0")


def test_supersede_rejects_explicit_empty_updates() -> None:
    with pytest.raises(HandoffError, match="next_action"):
        handoff().supersede(
            id="handoff-2",
            created_by="worker-2",
            created_at=NOW + timedelta(minutes=1),
            source_observation_id="chg-1:git:2",
            next_action="",
        )


def test_created_at_requires_timezone() -> None:
    with pytest.raises(HandoffError, match="timezone"):
        handoff(created_at=NOW.replace(tzinfo=None))
