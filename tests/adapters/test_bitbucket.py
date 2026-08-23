from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

import pytest

from weftmark.adapters.bitbucket import (
    BitbucketAdapterError,
    BitbucketForgeAdapter,
    BitbucketHttpResponse,
    BitbucketTransportError,
)
from weftmark.application.ports.forge import (
    ForgeAvailability,
    ForgeChangeState,
    ForgeCommentKind,
    ForgeConclusion,
    ForgeReviewState,
    ForgeRunStatus,
)
from weftmark.application.ports.git import GitChangeKind, GitObjectId


SHA_A = "a" * 40
SHA_B = "b" * 40
NOW = "2026-08-23T10:00:00Z"
LATER = "2026-08-23T10:05:00Z"
API = "https://api.bitbucket.test/2.0"
WEB = "https://bitbucket.test"
REPOSITORY = "team/repo"


def response(payload, *, status: int = 200) -> BitbucketHttpResponse:
    return BitbucketHttpResponse(status, json.dumps(payload).encode(), {})


def page(values, *, next_url: str | None = None):
    payload = {"values": values}
    if next_url is not None:
        payload["next"] = next_url
    return payload


def actor(identifier: str, nickname: str) -> dict[str, str]:
    return {"uuid": identifier, "nickname": nickname}


def html_link(path: str) -> dict[str, dict[str, str]]:
    return {"html": {"href": f"{WEB}/{path}"}}


def pull_request(*, state: str = "OPEN", participants=None):
    return {
        "id": 42,
        "title": "Ship Bitbucket adapter",
        "state": state,
        "draft": True,
        "updated_on": LATER,
        "author": actor("{alice}", "alice"),
        "source": {
            "branch": {"name": "feature/bitbucket"},
            "commit": {"hash": SHA_B},
        },
        "destination": {
            "branch": {"name": "main"},
            "commit": {"hash": SHA_A},
        },
        "participants": [] if participants is None else participants,
        "links": html_link("team/repo/pull-requests/42"),
    }


@dataclass
class FixtureTransport:
    fixtures: dict[str, BitbucketHttpResponse | Exception]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(self, url: str, *, headers: Mapping[str, str]) -> BitbucketHttpResponse:
        self.calls.append((url, dict(headers)))
        value = self.fixtures[url]
        if isinstance(value, Exception):
            raise value
        return value


def adapter(
    fixtures: dict[str, BitbucketHttpResponse | Exception],
    *,
    token: str | None = None,
):
    transport = FixtureTransport(fixtures)
    return (
        BitbucketForgeAdapter(
            REPOSITORY,
            token=token,
            api_base=API,
            web_base=WEB,
            transport=transport,
        ),
        transport,
    )


def test_repository_capabilities_and_pull_request_map_without_token_leakage() -> None:
    url = f"{API}/repositories/{REPOSITORY}/pullrequests/42"
    value, transport = adapter({url: response(pull_request())}, token="fixture-token")

    result = value.change_request("42")

    assert value.repository().provider == "bitbucket"
    assert value.repository().web_url == f"{WEB}/{REPOSITORY}"
    assert value.capabilities().workflow_runs is True
    assert result.availability is ForgeAvailability.AVAILABLE
    assert result.value.state is ForgeChangeState.OPEN
    assert result.value.source_branch == "feature/bitbucket"
    assert str(result.value.head) == SHA_B
    assert transport.calls[0][1]["Authorization"] == "Bearer fixture-token"
    assert "fixture-token" not in repr(result)


def test_merged_pull_request_has_provider_timestamp_and_closed_states_normalize() -> None:
    merged_url = f"{API}/repositories/{REPOSITORY}/pullrequests/42"
    merged, _ = adapter({merged_url: response(pull_request(state="MERGED"))})
    merged_result = merged.change_request("42")

    assert merged_result.value.state is ForgeChangeState.MERGED
    assert merged_result.value.merged_at == merged_result.value.updated_at

    declined, _ = adapter({merged_url: response(pull_request(state="DECLINED"))})
    declined_result = declined.change_request("42")
    assert declined_result.value.state is ForgeChangeState.CLOSED
    assert declined_result.value.merged_at is None


def test_build_statuses_preserve_pass_fail_and_pending() -> None:
    url = f"{API}/repositories/{REPOSITORY}/commit/{SHA_B}/statuses?pagelen=100"
    value, _ = adapter(
        {
            url: response(
                page(
                    [
                        {
                            "uuid": "{build-1}",
                            "key": "lint",
                            "name": "Lint",
                            "state": "SUCCESSFUL",
                            "url": f"{WEB}/build/1",
                            "created_on": NOW,
                            "updated_on": LATER,
                        },
                        {
                            "uuid": "{build-2}",
                            "key": "security",
                            "name": "Security",
                            "state": "FAILED",
                            "url": f"{WEB}/build/2",
                            "created_on": NOW,
                            "updated_on": LATER,
                        },
                        {
                            "uuid": "{build-3}",
                            "key": "unit",
                            "name": "Unit",
                            "state": "INPROGRESS",
                            "url": f"{WEB}/build/3",
                            "created_on": NOW,
                            "updated_on": LATER,
                        },
                    ]
                )
            )
        }
    )

    result = value.checks(GitObjectId(SHA_B))
    by_name = {item.name: item for item in result.value}

    assert by_name["Lint"].conclusion is ForgeConclusion.PASSED
    assert by_name["Security"].conclusion is ForgeConclusion.FAILED
    assert by_name["Unit"].status is ForgeRunStatus.IN_PROGRESS
    assert by_name["Unit"].conclusion is None


def test_pipelines_distinguish_missing_failed_and_in_progress() -> None:
    suffix = f"target.commit.hash={SHA_B}&sort=-created_on&pagelen=100"
    url = f"{API}/repositories/{REPOSITORY}/pipelines/?{suffix}"
    empty, _ = adapter({url: response(page([]))})
    assert empty.workflow_runs(GitObjectId(SHA_B)).availability is ForgeAvailability.MISSING

    failed, _ = adapter(
        {
            url: response(
                page(
                    [
                        {
                            "uuid": "{pipeline-1}",
                            "build_number": 10,
                            "state": {
                                "name": "COMPLETED",
                                "result": {"name": "FAILED"},
                            },
                            "target": {
                                "type": "pipeline_ref_target",
                                "commit": {"hash": SHA_B},
                            },
                            "trigger": {"name": "PUSH"},
                            "created_on": NOW,
                            "completed_on": LATER,
                            "links": html_link("team/repo/pipelines/10"),
                        },
                        {
                            "uuid": "{pipeline-2}",
                            "build_number": 11,
                            "state": {"name": "IN_PROGRESS"},
                            "target": {
                                "type": "pipeline_ref_target",
                                "commit": {"hash": SHA_B},
                            },
                            "trigger": {"name": "PULLREQUEST"},
                            "created_on": NOW,
                            "completed_on": None,
                            "links": html_link("team/repo/pipelines/11"),
                        },
                    ]
                )
            )
        }
    )

    result = failed.workflow_runs(GitObjectId(SHA_B))
    by_id = {item.external_id: item for item in result.value}

    assert by_id["{pipeline-1}"].conclusion is ForgeConclusion.FAILED
    assert by_id["{pipeline-2}"].status is ForgeRunStatus.IN_PROGRESS


def test_approvals_comments_and_diffstat_map_to_generic_records() -> None:
    pull_url = f"{API}/repositories/{REPOSITORY}/pullrequests/42"
    comments_url = f"{pull_url}/comments?pagelen=100"
    diffstat_url = f"{pull_url}/diffstat?pagelen=100"
    participants = [
        {
            "role": "REVIEWER",
            "approved": True,
            "user": actor("{reviewer}", "reviewer"),
        },
        {
            "role": "PARTICIPANT",
            "approved": False,
            "user": actor("{participant}", "participant"),
        },
    ]
    value, _ = adapter(
        {
            pull_url: response(pull_request(participants=participants)),
            comments_url: response(
                page(
                    [
                        {
                            "id": 11,
                            "user": actor("{reviewer}", "reviewer"),
                            "content": {"raw": "General note"},
                            "created_on": NOW,
                            "updated_on": NOW,
                            "deleted": False,
                            "inline": None,
                            "links": html_link("comment/11"),
                        },
                        {
                            "id": 12,
                            "user": actor("{reviewer}", "reviewer"),
                            "content": {"raw": "Inline note"},
                            "created_on": NOW,
                            "updated_on": LATER,
                            "deleted": False,
                            "inline": {"path": "src/new.py", "to": 7},
                            "links": html_link("comment/12"),
                        },
                    ]
                )
            ),
            diffstat_url: response(
                page(
                    [
                        {
                            "status": "renamed",
                            "old": {"path": "src/old.py"},
                            "new": {"path": "src/new.py"},
                            "lines_added": 3,
                            "lines_removed": 1,
                        }
                    ]
                )
            ),
        }
    )

    reviews = value.reviews("42")
    comments = value.comments("42")
    files = value.changed_files("42")

    assert len(reviews.value) == 1
    assert reviews.value[0].state is ForgeReviewState.APPROVED
    assert reviews.value[0].commit == GitObjectId(SHA_B)
    assert {item.kind for item in comments.value} == {
        ForgeCommentKind.GENERAL,
        ForgeCommentKind.REVIEW,
    }
    inline = next(item for item in comments.value if item.kind is ForgeCommentKind.REVIEW)
    assert inline.path == "src/new.py"
    assert inline.line == 7
    assert files.value[0].entry.kind is GitChangeKind.RENAMED
    assert files.value[0].entry.old_path == "src/old.py"


def test_pagination_follows_same_origin_next_and_refuses_cross_origin() -> None:
    first = f"{API}/repositories/{REPOSITORY}/pullrequests/42/comments?pagelen=100"
    second = f"{API}/repositories/{REPOSITORY}/pullrequests/42/comments?page=2"
    value, transport = adapter(
        {
            first: response(
                page(
                    [
                        {
                            "id": 1,
                            "user": actor("{one}", "one"),
                            "content": {"raw": "one"},
                            "created_on": NOW,
                            "updated_on": NOW,
                            "inline": None,
                            "links": html_link("comment/1"),
                        }
                    ],
                    next_url=second,
                )
            ),
            second: response(
                page(
                    [
                        {
                            "id": 2,
                            "user": actor("{two}", "two"),
                            "content": {"raw": "two"},
                            "created_on": LATER,
                            "updated_on": LATER,
                            "inline": None,
                            "links": html_link("comment/2"),
                        }
                    ]
                )
            ),
        }
    )

    assert len(value.comments("42").value) == 2
    assert [call[0] for call in transport.calls] == [first, second]

    unsafe, unsafe_transport = adapter(
        {
            first: response(
                page([], next_url="https://attacker.invalid/steal")
            )
        }
    )
    result = unsafe.comments("42")
    assert result.availability is ForgeAvailability.UNAVAILABLE
    assert result.detail == "Bitbucket returned an unsafe pagination URL"
    assert len(unsafe_transport.calls) == 1


def test_missing_transport_malformed_data_and_configuration_fail_safely() -> None:
    missing_url = f"{API}/repositories/{REPOSITORY}/pullrequests/404"
    missing, _ = adapter({missing_url: response({"error": "missing"}, status=404)})
    assert missing.change_request("404").availability is ForgeAvailability.MISSING

    transport_url = f"{API}/repositories/{REPOSITORY}/pullrequests/43"
    unavailable, _ = adapter(
        {transport_url: BitbucketTransportError("secret upstream detail")}
    )
    result = unavailable.change_request("43")
    assert result.availability is ForgeAvailability.UNAVAILABLE
    assert result.detail == "Bitbucket transport unavailable"
    assert "secret upstream detail" not in result.detail

    malformed_url = f"{API}/repositories/{REPOSITORY}/pullrequests/44"
    malformed, _ = adapter({malformed_url: response({"id": 44})})
    assert malformed.change_request("44").availability is ForgeAvailability.UNAVAILABLE

    with pytest.raises(BitbucketAdapterError):
        BitbucketForgeAdapter("team/repo/extra")
    with pytest.raises(BitbucketAdapterError):
        BitbucketForgeAdapter(REPOSITORY, token="bad\ntoken")
    with pytest.raises(BitbucketAdapterError):
        BitbucketForgeAdapter(REPOSITORY, api_base="https://user:pass@example.test/2.0")
    with pytest.raises(BitbucketAdapterError):
        BitbucketForgeAdapter(REPOSITORY).change_request("0")
