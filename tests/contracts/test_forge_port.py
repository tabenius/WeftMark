from __future__ import annotations

from datetime import UTC, datetime

import pytest

from weftmark.application.ports.forge import (
    ForgeActor,
    ForgeAvailability,
    ForgeCapabilities,
    ForgeChangedFile,
    ForgeChangeRequest,
    ForgeChangeState,
    ForgeCheck,
    ForgeConclusion,
    ForgeContractError,
    ForgePort,
    ForgeRepository,
    ForgeResult,
    ForgeRunStatus,
)
from weftmark.application.ports.git import GitChangeKind, GitDiffEntry, GitObjectId


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
SHA_A = GitObjectId("a" * 40)
SHA_B = GitObjectId("b" * 40)


def test_result_distinguishes_missing_unsupported_and_unavailable() -> None:
    missing = ForgeResult[tuple[ForgeCheck, ...]].missing("no checks for head")
    unsupported = ForgeResult[tuple[ForgeCheck, ...]].unsupported("checks unsupported")
    unavailable = ForgeResult[tuple[ForgeCheck, ...]].unavailable("provider transport unavailable")

    assert missing.availability is ForgeAvailability.MISSING
    assert unsupported.availability is ForgeAvailability.UNSUPPORTED
    assert unavailable.availability is ForgeAvailability.UNAVAILABLE
    assert missing.value is unsupported.value is unavailable.value is None


def test_result_rejects_ambiguous_availability() -> None:
    with pytest.raises(ForgeContractError):
        ForgeResult(ForgeAvailability.AVAILABLE)
    with pytest.raises(ForgeContractError):
        ForgeResult(ForgeAvailability.UNAVAILABLE, value=())
    with pytest.raises(ForgeContractError):
        ForgeResult(ForgeAvailability.UNAVAILABLE)
    with pytest.raises(ForgeContractError):
        ForgeResult(ForgeAvailability.UNSUPPORTED)


def test_capabilities_are_observation_support_not_authority() -> None:
    value = ForgeCapabilities(workflow_runs=False)
    assert value.change_requests is True
    assert value.workflow_runs is False
    assert not hasattr(value, "merge")
    assert not hasattr(value, "approve")


def test_change_request_uses_provider_neutral_terms() -> None:
    change = ForgeChangeRequest(
        external_id="42",
        title="Ship forge port",
        state=ForgeChangeState.OPEN,
        source_branch="feature/forge",
        target_branch="main",
        head=SHA_B,
        base=SHA_A,
        web_url="https://forge.example/team/repo/change/42",
        author=ForgeActor("user:7", "alice"),
        draft=False,
        updated_at=NOW,
    )

    assert change.source_branch == "feature/forge"
    assert not hasattr(change, "pull_request_number")
    assert not hasattr(change, "merge_request_iid")


def test_merged_change_requires_merge_timestamp() -> None:
    with pytest.raises(ForgeContractError, match="merged_at"):
        ForgeChangeRequest(
            external_id="42",
            title="Merged",
            state=ForgeChangeState.MERGED,
            source_branch="feature",
            target_branch="main",
            head=SHA_B,
            base=SHA_A,
            web_url="https://forge.example/change/42",
            author=ForgeActor("7", "alice"),
            draft=False,
            updated_at=NOW,
        )


def test_checks_separate_run_status_from_conclusion() -> None:
    queued = ForgeCheck(
        external_id="check-1",
        name="unit",
        status=ForgeRunStatus.QUEUED,
        conclusion=None,
        head=SHA_B,
    )
    passed = ForgeCheck(
        external_id="check-2",
        name="unit",
        status=ForgeRunStatus.COMPLETED,
        conclusion=ForgeConclusion.PASSED,
        head=SHA_B,
        completed_at=NOW,
    )

    assert queued.conclusion is None
    assert passed.conclusion is ForgeConclusion.PASSED

    with pytest.raises(ForgeContractError, match="conclusion"):
        ForgeCheck(
            external_id="check-3",
            name="bad",
            status=ForgeRunStatus.COMPLETED,
            conclusion=None,
            head=SHA_B,
        )


def test_changed_file_counts_distinguish_unknown_from_exact_zero() -> None:
    entry = GitDiffEntry("src/value.py", GitChangeKind.MODIFIED)

    unknown = ForgeChangedFile(entry, additions=None, deletions=None)
    exact_zero = ForgeChangedFile(entry, additions=0, deletions=0)

    assert unknown.additions is None
    assert unknown.deletions is None
    assert exact_zero.additions == 0
    assert exact_zero.deletions == 0

    with pytest.raises(ForgeContractError, match="negative"):
        ForgeChangedFile(entry, additions=-1, deletions=None)


def test_forge_port_is_runtime_checkable_and_read_side_only() -> None:
    class FakeForge:
        def repository(self):
            return ForgeRepository("fake", "team/repo", "https://forge.example/team/repo")

        def capabilities(self):
            return ForgeCapabilities()

        def change_request(self, external_id):
            raise NotImplementedError

        def checks(self, head):
            raise NotImplementedError

        def workflow_runs(self, head):
            raise NotImplementedError

        def reviews(self, external_id):
            raise NotImplementedError

        def comments(self, external_id):
            raise NotImplementedError

        def changed_files(self, external_id):
            raise NotImplementedError

    value = FakeForge()
    assert isinstance(value, ForgePort)
    assert not hasattr(value, "merge")
    assert not hasattr(value, "approve")
