from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from weftmark.adapters.gitlab import (
    GitlabForgeAdapter,
    GitlabHttpResponse,
    GitlabTransportError,
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
NOW = "2026-08-20T10:00:00Z"
LATER = "2026-08-20T10:05:00Z"
API = "https://gitlab.test/api/v4"
WEB = "https://gitlab.test"
PROJECT_ID = "group%2Fsubgroup%2Frepo"


def response(payload, *, status: int = 200) -> GitlabHttpResponse:
    return GitlabHttpResponse(status, json.dumps(payload).encode(), {})


@dataclass
class FixtureTransport:
    fixtures: dict[str, GitlabHttpResponse | Exception]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(self, url: str, *, headers: Mapping[str, str]) -> GitlabHttpResponse:
        self.calls.append((url, dict(headers)))
        value = self.fixtures[url]
        if isinstance(value, Exception):
            raise value
        return value


def adapter(fixtures: dict[str, GitlabHttpResponse | Exception], *, token: str | None = None):
    transport = FixtureTransport(fixtures)
    return (
        GitlabForgeAdapter(
            "group/subgroup/repo",
            token=token,
            api_base=API,
            web_base=WEB,
            transport=transport,
        ),
        transport,
    )


def test_capabilities_and_change_request_map_merge_request_without_domain_leakage() -> None:
    url = f"{API}/projects/{PROJECT_ID}/merge_requests/42"
    value, transport = adapter(
        {
            url: response(
                {
                    "iid": 42,
                    "title": "Ship GitLab adapter",
                    "state": "opened",
                    "draft": True,
                    "source_branch": "feature/gitlab",
                    "target_branch": "main",
                    "web_url": f"{WEB}/group/subgroup/repo/-/merge_requests/42",
                    "updated_at": NOW,
                    "merged_at": None,
                    "author": {"id": 7, "username": "alice"},
                    "diff_refs": {"base_sha": SHA_A, "head_sha": SHA_B},
                }
            )
        },
        token="fixture-token",
    )

    result = value.change_request("42")

    assert value.capabilities().workflow_runs is True
    assert result.availability is ForgeAvailability.AVAILABLE
    assert result.value.state is ForgeChangeState.OPEN
    assert result.value.source_branch == "feature/gitlab"
    assert str(result.value.base) == SHA_A
    assert transport.calls[0][1]["PRIVATE-TOKEN"] == "fixture-token"
    assert "fixture-token" not in repr(result)


def test_commit_statuses_preserve_pass_fail_and_pending() -> None:
    url = (
        f"{API}/projects/{PROJECT_ID}/repository/commits/{SHA_B}/statuses"
        "?all=true&per_page=100&page=1"
    )
    value, _ = adapter(
        {
            url: response(
                [
                    {
                        "id": 1,
                        "name": "lint",
                        "status": "success",
                        "target_url": f"{WEB}/jobs/1",
                        "created_at": NOW,
                        "finished_at": LATER,
                    },
                    {
                        "id": 2,
                        "name": "security",
                        "status": "failed",
                        "target_url": f"{WEB}/jobs/2",
                        "created_at": NOW,
                        "finished_at": LATER,
                    },
                    {
                        "id": 3,
                        "name": "unit",
                        "status": "pending",
                        "target_url": f"{WEB}/jobs/3",
                        "created_at": NOW,
                        "finished_at": None,
                    },
                ]
            )
        }
    )

    result = value.checks(GitObjectId(SHA_B))
    by_name = {item.name: item for item in result.value}

    assert by_name["lint"].conclusion is ForgeConclusion.PASSED
    assert by_name["security"].conclusion is ForgeConclusion.FAILED
    assert by_name["unit"].status is ForgeRunStatus.PENDING
    assert by_name["unit"].conclusion is None


def test_pipelines_distinguish_missing_from_failed() -> None:
    empty_url = f"{API}/projects/{PROJECT_ID}/pipelines?sha={SHA_A}&per_page=100&page=1"
    empty, _ = adapter({empty_url: response([])})
    assert empty.workflow_runs(GitObjectId(SHA_A)).availability is ForgeAvailability.MISSING

    failed_url = f"{API}/projects/{PROJECT_ID}/pipelines?sha={SHA_B}&per_page=100&page=1"
    failed, _ = adapter(
        {
            failed_url: response(
                [
                    {
                        "id": 91,
                        "name": "MR pipeline",
                        "source": "merge_request_event",
                        "status": "failed",
                        "sha": SHA_B,
                        "web_url": f"{WEB}/pipelines/91",
                        "created_at": NOW,
                        "updated_at": LATER,
                    }
                ]
            )
        }
    )
    result = failed.workflow_runs(GitObjectId(SHA_B))
    assert result.value[0].conclusion is ForgeConclusion.FAILED


def test_approvals_discussions_and_diffs_map_to_generic_records() -> None:
    approvals_url = f"{API}/projects/{PROJECT_ID}/merge_requests/42/approvals"
    discussions_url = (
        f"{API}/projects/{PROJECT_ID}/merge_requests/42/discussions?per_page=100&page=1"
    )
    diffs_url = f"{API}/projects/{PROJECT_ID}/merge_requests/42/diffs?per_page=100&page=1"
    value, _ = adapter(
        {
            approvals_url: response(
                {
                    "approved": True,
                    "approved_by": [
                        {
                            "user": {"id": 8, "username": "reviewer"},
                            "approved_at": NOW,
                        }
                    ],
                }
            ),
            discussions_url: response(
                [
                    {
                        "id": "discussion-1",
                        "notes": [
                            {
                                "id": 11,
                                "body": "General note",
                                "author": {"id": 8, "username": "reviewer"},
                                "created_at": NOW,
                                "updated_at": NOW,
                                "system": False,
                                "position": None,
                            },
                            {
                                "id": 12,
                                "body": "Inline note",
                                "author": {"id": 8, "username": "reviewer"},
                                "created_at": NOW,
                                "updated_at": LATER,
                                "system": False,
                                "position": {"new_path": "src/new.py", "new_line": 7},
                            },
                            {
                                "id": 13,
                                "body": "changed title",
                                "author": {"id": 1, "username": "system"},
                                "created_at": NOW,
                                "updated_at": NOW,
                                "system": True,
                            },
                        ],
                    }
                ]
            ),
            diffs_url: response(
                [
                    {
                        "old_path": "src/old.py",
                        "new_path": "src/new.py",
                        "renamed_file": True,
                        "new_file": False,
                        "deleted_file": False,
                        "collapsed": False,
                        "too_large": False,
                        "diff": "@@ -1 +1,2 @@\n-old\n+new\n+extra\n",
                    }
                ]
            ),
        }
    )

    reviews = value.reviews("42")
    comments = value.comments("42")
    files = value.changed_files("42")

    assert reviews.value[0].state is ForgeReviewState.APPROVED
    assert len(comments.value) == 2
    inline = next(item for item in comments.value if item.kind is ForgeCommentKind.REVIEW)
    assert inline.path == "src/new.py"
    assert inline.line == 7
    assert files.value[0].entry.kind is GitChangeKind.RENAMED
    assert files.value[0].entry.old_path == "src/old.py"
    assert files.value[0].additions == 2
    assert files.value[0].deletions == 1


def test_truncated_diff_refuses_to_invent_line_counts() -> None:
    url = f"{API}/projects/{PROJECT_ID}/merge_requests/42/diffs?per_page=100&page=1"
    value, _ = adapter(
        {
            url: response(
                [
                    {
                        "old_path": "huge.bin",
                        "new_path": "huge.bin",
                        "renamed_file": False,
                        "new_file": False,
                        "deleted_file": False,
                        "collapsed": True,
                        "too_large": False,
                        "diff": "",
                    }
                ]
            )
        }
    )
    result = value.changed_files("42")
    assert result.availability is ForgeAvailability.UNAVAILABLE
    assert "truncated" in result.detail


def test_404_transport_and_api_failure_are_safe_observation_states() -> None:
    missing_url = f"{API}/projects/{PROJECT_ID}/merge_requests/404"
    missing, _ = adapter({missing_url: response({"message": "404"}, status=404)})
    assert missing.change_request("404").availability is ForgeAvailability.MISSING

    transport_url = f"{API}/projects/{PROJECT_ID}/merge_requests/43"
    transport, _ = adapter({transport_url: GitlabTransportError("secret detail")})
    unavailable = transport.change_request("43")
    assert unavailable.availability is ForgeAvailability.UNAVAILABLE
    assert unavailable.detail == "GitLab transport unavailable"
    assert "secret detail" not in unavailable.detail

    api_url = f"{API}/projects/{PROJECT_ID}/merge_requests/44"
    api, _ = adapter({api_url: response({"message": "down"}, status=503)})
    result = api.change_request("44")
    assert result.availability is ForgeAvailability.UNAVAILABLE
    assert "503" in result.detail
