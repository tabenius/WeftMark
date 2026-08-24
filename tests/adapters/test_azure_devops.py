from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

import pytest

from weftmark.adapters.azure_devops import (
    AzureDevopsAdapterError,
    AzureDevopsForgeAdapter,
    AzureDevopsHttpResponse,
    AzureDevopsTransportError,
)
from weftmark.application.ports.forge import (
    ForgeAvailability,
    ForgeChangeState,
    ForgeCommentKind,
    ForgeConclusion,
    ForgeReviewState,
    ForgeRunStatus,
)
from weftmark.application.ports.git import GitObjectId


SHA_A = "a" * 40
SHA_B = "b" * 40
NOW = "2026-08-23T10:00:00Z"
LATER = "2026-08-23T10:05:00Z"
API = "https://dev.azure.test"
WEB = "https://azure.test"
IDENTITY = "org/project/repo"
GIT = f"{API}/org/project/_apis/git/repositories/repo"


def response(payload, *, status: int = 200, headers=None) -> AzureDevopsHttpResponse:
    return AzureDevopsHttpResponse(
        status,
        json.dumps(payload).encode(),
        {} if headers is None else headers,
    )


def actor(identifier: str, login: str) -> dict[str, str]:
    return {"id": identifier, "uniqueName": login, "displayName": login}


def pull_request(*, status: str = "active"):
    payload = {
        "pullRequestId": 42,
        "title": "Ship Azure adapter",
        "status": status,
        "isDraft": True,
        "sourceRefName": "refs/heads/feature/azure",
        "targetRefName": "refs/heads/main",
        "lastMergeSourceCommit": {"commitId": SHA_B},
        "lastMergeTargetCommit": {"commitId": SHA_A},
        "createdBy": actor("alice-id", "alice@example.test"),
        "creationDate": NOW,
        "closedDate": None,
    }
    if status == "completed":
        payload["closedDate"] = LATER
    return payload


@dataclass
class FixtureTransport:
    fixtures: dict[str, AzureDevopsHttpResponse | Exception]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(self, url: str, *, headers: Mapping[str, str]) -> AzureDevopsHttpResponse:
        self.calls.append((url, dict(headers)))
        value = self.fixtures[url]
        if isinstance(value, Exception):
            raise value
        return value


def adapter(
    fixtures: dict[str, AzureDevopsHttpResponse | Exception],
    *,
    token: str | None = None,
):
    transport = FixtureTransport(fixtures)
    return (
        AzureDevopsForgeAdapter(
            IDENTITY,
            token=token,
            api_base=API,
            web_base=WEB,
            transport=transport,
        ),
        transport,
    )


def test_repository_capabilities_and_pull_request_keep_hierarchy_adapter_local() -> None:
    url = f"{GIT}/pullRequests/42?api-version=7.1"
    value, transport = adapter({url: response(pull_request())}, token="fixture-token")

    result = value.change_request("42")

    assert value.repository().provider == "azure-devops"
    assert value.repository().id == IDENTITY
    assert value.repository().web_url == f"{WEB}/org/project/_git/repo"
    assert value.capabilities().changed_files is True
    assert result.availability is ForgeAvailability.AVAILABLE
    assert result.value.state is ForgeChangeState.OPEN
    assert result.value.source_branch == "feature/azure"
    assert str(result.value.head) == SHA_B
    assert result.value.web_url == f"{WEB}/org/project/_git/repo/pullrequest/42"
    assert transport.calls[0][1]["Authorization"] == "Bearer fixture-token"
    assert "fixture-token" not in repr(result)


def test_completed_and_abandoned_pull_requests_map_without_policy_authority() -> None:
    url = f"{GIT}/pullRequests/42?api-version=7.1"
    merged, _ = adapter({url: response(pull_request(status="completed"))})
    merged_result = merged.change_request("42")

    assert merged_result.value.state is ForgeChangeState.MERGED
    assert merged_result.value.merged_at == merged_result.value.updated_at

    closed, _ = adapter({url: response(pull_request(status="abandoned"))})
    closed_result = closed.change_request("42")
    assert closed_result.value.state is ForgeChangeState.CLOSED
    assert closed_result.value.merged_at is None


def test_commit_statuses_preserve_success_failure_and_pending() -> None:
    url = f"{GIT}/commits/{SHA_B}/statuses?top=1000&skip=0&api-version=7.1"
    value, _ = adapter(
        {
            url: response(
                {
                    "count": 3,
                    "value": [
                        {
                            "id": 1,
                            "state": "succeeded",
                            "context": {"genre": "ci", "name": "lint"},
                            "targetUrl": f"{WEB}/status/1",
                            "creationDate": NOW,
                            "updatedDate": LATER,
                        },
                        {
                            "id": 2,
                            "state": "failed",
                            "context": {"genre": "ci", "name": "security"},
                            "targetUrl": f"{WEB}/status/2",
                            "creationDate": NOW,
                            "updatedDate": LATER,
                        },
                        {
                            "id": 3,
                            "state": "pending",
                            "context": {"genre": "ci", "name": "unit"},
                            "targetUrl": f"{WEB}/status/3",
                            "creationDate": NOW,
                            "updatedDate": LATER,
                        },
                    ],
                }
            )
        }
    )

    result = value.checks(GitObjectId(SHA_B))
    by_name = {item.name: item for item in result.value}

    assert by_name["ci/lint"].conclusion is ForgeConclusion.PASSED
    assert by_name["ci/security"].conclusion is ForgeConclusion.FAILED
    assert by_name["ci/unit"].status is ForgeRunStatus.PENDING
    assert by_name["ci/unit"].conclusion is None


def test_commit_statuses_follow_documented_top_skip_pagination() -> None:
    first = f"{GIT}/commits/{SHA_B}/statuses?top=1000&skip=0&api-version=7.1"
    second = f"{GIT}/commits/{SHA_B}/statuses?top=1000&skip=1000&api-version=7.1"
    status = {
        "id": 1,
        "state": "succeeded",
        "context": {"genre": "ci", "name": "same"},
        "targetUrl": f"{WEB}/status/1",
        "creationDate": NOW,
        "updatedDate": LATER,
    }
    value, transport = adapter(
        {
            first: response({"count": 1000, "value": [status] * 1000}),
            second: response({"count": 1, "value": [{**status, "id": 2}]}),
        }
    )

    result = value.checks(GitObjectId(SHA_B))

    assert result.availability is ForgeAvailability.AVAILABLE
    assert len(result.value) == 1001
    assert [call[0] for call in transport.calls] == [first, second]


def test_builds_distinguish_missing_failed_and_in_progress() -> None:
    query = (
        "repositoryId=repo&repositoryType=TfsGit&"
        f"sourceVersion={SHA_B}&queryOrder=finishTimeDescending&"
        "%24top=100&api-version=7.1"
    )
    url = f"{API}/org/project/_apis/build/builds?{query}"
    empty, _ = adapter({url: response({"count": 0, "value": []})})
    assert empty.workflow_runs(GitObjectId(SHA_B)).availability is ForgeAvailability.MISSING

    builds = [
        {
            "id": 10,
            "buildNumber": "20260823.10",
            "definition": {"name": "CI"},
            "status": "completed",
            "result": "failed",
            "reason": "pullRequest",
            "sourceVersion": SHA_B,
            "queueTime": NOW,
            "startTime": NOW,
            "finishTime": LATER,
            "url": f"{API}/build/10",
            "_links": {"web": {"href": f"{WEB}/build/10"}},
        },
        {
            "id": 11,
            "buildNumber": "20260823.11",
            "definition": {"name": "Integration"},
            "status": "inProgress",
            "result": None,
            "reason": "manual",
            "sourceVersion": SHA_B,
            "queueTime": NOW,
            "startTime": NOW,
            "finishTime": None,
            "url": f"{WEB}/build/11",
        },
    ]
    present, _ = adapter({url: response({"count": 2, "value": builds})})
    result = present.workflow_runs(GitObjectId(SHA_B))
    by_name = {item.name: item for item in result.value}

    assert by_name["CI"].conclusion is ForgeConclusion.FAILED
    assert by_name["CI"].web_url == f"{WEB}/build/10"
    assert by_name["Integration"].status is ForgeRunStatus.IN_PROGRESS


def test_review_votes_and_human_threads_map_to_generic_records() -> None:
    reviewers_url = f"{GIT}/pullRequests/42/reviewers?api-version=7.1"
    threads_url = f"{GIT}/pullRequests/42/threads?api-version=7.1"
    value, _ = adapter(
        {
            reviewers_url: response(
                {
                    "count": 3,
                    "value": [
                        {**actor("approved", "approved@example.test"), "vote": 10},
                        {**actor("blocked", "blocked@example.test"), "vote": -10},
                        {**actor("pending", "pending@example.test"), "vote": 0},
                    ],
                }
            ),
            threads_url: response(
                {
                    "count": 2,
                    "value": [
                        {
                            "id": 7,
                            "threadContext": {
                                "filePath": "/src/new.py",
                                "rightFileStart": {"line": 12, "offset": 1},
                            },
                            "comments": [
                                {
                                    "id": 1,
                                    "content": "Inline note",
                                    "author": actor("reviewer", "reviewer@example.test"),
                                    "publishedDate": NOW,
                                    "lastUpdatedDate": LATER,
                                    "commentType": "text",
                                    "isDeleted": False,
                                },
                                {
                                    "id": 2,
                                    "content": "system update",
                                    "author": actor("system", "system"),
                                    "publishedDate": NOW,
                                    "lastUpdatedDate": NOW,
                                    "commentType": "system",
                                    "isDeleted": False,
                                },
                            ],
                        },
                        {
                            "id": 8,
                            "threadContext": None,
                            "comments": [
                                {
                                    "id": 1,
                                    "content": "General note",
                                    "author": actor("reviewer", "reviewer@example.test"),
                                    "publishedDate": NOW,
                                    "lastUpdatedDate": NOW,
                                    "commentType": "text",
                                    "isDeleted": False,
                                }
                            ],
                        },
                    ],
                }
            ),
        }
    )

    reviews = value.reviews("42")
    comments = value.comments("42")
    states = {review.author.login: review.state for review in reviews.value}

    assert states["approved@example.test"] is ForgeReviewState.APPROVED
    assert states["blocked@example.test"] is ForgeReviewState.CHANGES_REQUESTED
    assert states["pending@example.test"] is ForgeReviewState.PENDING
    assert len(comments.value) == 2
    inline = next(item for item in comments.value if item.kind is ForgeCommentKind.REVIEW)
    assert inline.path == "src/new.py"
    assert inline.line == 12
    assert any(item.kind is ForgeCommentKind.GENERAL for item in comments.value)


def test_continuation_token_paginates_and_unsafe_token_fails_closed() -> None:
    first = f"{GIT}/pullRequests/42/reviewers?api-version=7.1"
    second = f"{GIT}/pullRequests/42/reviewers?continuationToken=next-1&api-version=7.1"
    value, transport = adapter(
        {
            first: response(
                {"count": 1, "value": [{**actor("one", "one"), "vote": 10}]},
                headers={"X-MS-ContinuationToken": "next-1"},
            ),
            second: response(
                {"count": 1, "value": [{**actor("two", "two"), "vote": 0}]}
            ),
        }
    )

    assert len(value.reviews("42").value) == 2
    assert [call[0] for call in transport.calls] == [first, second]

    unsafe, _ = adapter(
        {
            first: response(
                {"count": 0, "value": []},
                headers={"x-ms-continuationtoken": "bad\nvalue"},
            )
        }
    )
    result = unsafe.reviews("42")
    assert result.availability is ForgeAvailability.UNAVAILABLE
    assert result.detail == "Azure DevOps returned malformed paginated data"


def test_changed_files_preserve_paths_and_unknown_counts_across_iteration_pages() -> None:
    iterations_url = f"{GIT}/pullRequests/42/iterations?api-version=7.1"
    first_changes_url = (
        f"{GIT}/pullRequests/42/iterations/3/changes?%24top=2000&api-version=7.1"
    )
    second_changes_url = (
        f"{GIT}/pullRequests/42/iterations/3/changes?"
        "%24top=25&%24skip=2&api-version=7.1"
    )
    value, transport = adapter(
        {
            iterations_url: response(
                {"count": 2, "value": [{"id": 1}, {"id": 3}]}
            ),
            first_changes_url: response(
                {
                    "changeEntries": [
                        {
                            "changeTrackingId": 1,
                            "changeType": "edit",
                            "item": {"path": "/src/value.py"},
                        }
                    ],
                    "nextSkip": 2,
                    "nextTop": 25,
                }
            ),
            second_changes_url: response(
                {
                    "changeEntries": [
                        {
                            "changeTrackingId": 2,
                            "changeType": "rename",
                            "originalPath": "/src/old.py",
                            "item": {"path": "/src/new.py"},
                        }
                    ]
                }
            ),
        }
    )

    result = value.changed_files("42")

    assert result.availability is ForgeAvailability.AVAILABLE
    by_path = {item.entry.path: item for item in result.value}
    assert by_path["src/value.py"].additions is None
    assert by_path["src/value.py"].deletions is None
    assert by_path["src/new.py"].entry.old_path == "src/old.py"
    assert [call[0] for call in transport.calls] == [
        iterations_url,
        first_changes_url,
        second_changes_url,
    ]


def test_changed_files_treat_zero_iteration_cursor_as_terminal() -> None:
    iterations_url = f"{GIT}/pullRequests/42/iterations?api-version=7.1"
    changes_url = (
        f"{GIT}/pullRequests/42/iterations/1/changes?%24top=2000&api-version=7.1"
    )
    value, transport = adapter(
        {
            iterations_url: response({"count": 1, "value": [{"id": 1}]}),
            changes_url: response(
                {
                    "changeEntries": [
                        {
                            "changeTrackingId": 1,
                            "changeType": "edit",
                            "item": {"path": "/src/value.py"},
                        }
                    ],
                    "nextSkip": 0,
                    "nextTop": 0,
                }
            ),
        }
    )

    result = value.changed_files("42")

    assert result.availability is ForgeAvailability.AVAILABLE
    assert len(result.value) == 1
    assert [call[0] for call in transport.calls] == [iterations_url, changes_url]


def test_renamed_change_without_original_path_is_unavailable() -> None:
    iterations_url = f"{GIT}/pullRequests/42/iterations?api-version=7.1"
    changes_url = (
        f"{GIT}/pullRequests/42/iterations/1/changes?%24top=2000&api-version=7.1"
    )
    value, _ = adapter(
        {
            iterations_url: response({"count": 1, "value": [{"id": 1}]}),
            changes_url: response(
                {
                    "changeEntries": [
                        {
                            "changeTrackingId": 1,
                            "changeType": "rename",
                            "item": {"path": "/src/new.py"},
                        }
                    ]
                }
            ),
        }
    )

    result = value.changed_files("42")
    assert result.availability is ForgeAvailability.UNAVAILABLE
    assert result.detail == "Azure DevOps returned malformed iteration-change data"


def test_missing_transport_malformed_data_and_configuration_fail_safely() -> None:
    missing_url = f"{GIT}/pullRequests/404?api-version=7.1"
    missing, _ = adapter({missing_url: response({"message": "missing"}, status=404)})
    assert missing.change_request("404").availability is ForgeAvailability.MISSING

    transport_url = f"{GIT}/pullRequests/43?api-version=7.1"
    unavailable, _ = adapter(
        {transport_url: AzureDevopsTransportError("secret upstream detail")}
    )
    result = unavailable.change_request("43")
    assert result.availability is ForgeAvailability.UNAVAILABLE
    assert result.detail == "Azure DevOps transport unavailable"
    assert "secret upstream detail" not in result.detail

    malformed_url = f"{GIT}/pullRequests/44?api-version=7.1"
    malformed, _ = adapter({malformed_url: response({"pullRequestId": 44})})
    assert malformed.change_request("44").availability is ForgeAvailability.UNAVAILABLE

    with pytest.raises(AzureDevopsAdapterError):
        AzureDevopsForgeAdapter("org/project")
    with pytest.raises(AzureDevopsAdapterError):
        AzureDevopsForgeAdapter(IDENTITY, token="bad\ntoken")
    with pytest.raises(AzureDevopsAdapterError):
        AzureDevopsForgeAdapter(
            IDENTITY, api_base="https://user:pass@example.test"
        )
    with pytest.raises(AzureDevopsAdapterError):
        AzureDevopsForgeAdapter(IDENTITY).change_request("0")
