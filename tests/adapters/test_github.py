from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping

from weftmark.adapters.github import (
    GithubForgeAdapter,
    GithubHttpResponse,
    GithubTransportError,
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
API = "https://api.github.test"


def response(payload, *, status: int = 200) -> GithubHttpResponse:
    return GithubHttpResponse(status, json.dumps(payload).encode(), {})


@dataclass
class FixtureTransport:
    fixtures: dict[str, GithubHttpResponse | Exception]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(self, url: str, *, headers: Mapping[str, str]) -> GithubHttpResponse:
        self.calls.append((url, dict(headers)))
        value = self.fixtures[url]
        if isinstance(value, Exception):
            raise value
        return value


def adapter(fixtures: dict[str, GithubHttpResponse | Exception], *, token: str | None = None):
    transport = FixtureTransport(fixtures)
    return (
        GithubForgeAdapter(
            "team/repo",
            token=token,
            api_base=API,
            web_base="https://github.test",
            transport=transport,
        ),
        transport,
    )


def test_change_request_maps_github_pr_without_leaking_pr_terms_into_port() -> None:
    url = f"{API}/repos/team/repo/pulls/42"
    value, transport = adapter(
        {
            url: response(
                {
                    "number": 42,
                    "title": "Ship forge adapter",
                    "state": "open",
                    "draft": True,
                    "html_url": "https://github.test/team/repo/pull/42",
                    "updated_at": NOW,
                    "merged_at": None,
                    "user": {"id": 7, "login": "alice"},
                    "head": {"ref": "feature/forge", "sha": SHA_B},
                    "base": {"ref": "main", "sha": SHA_A},
                }
            )
        },
        token="fixture-token",
    )

    result = value.change_request("42")

    assert result.availability is ForgeAvailability.AVAILABLE
    assert result.value.state is ForgeChangeState.OPEN
    assert result.value.source_branch == "feature/forge"
    assert str(result.value.head) == SHA_B
    assert transport.calls[0][1]["Authorization"] == "Bearer fixture-token"
    assert "fixture-token" not in repr(result)


def test_checks_preserve_success_failure_and_pending_without_collapsing_state() -> None:
    url = f"{API}/repos/team/repo/commits/{SHA_B}/check-runs?filter=latest&per_page=100&page=1"
    value, _ = adapter(
        {
            url: response(
                {
                    "total_count": 3,
                    "check_runs": [
                        {
                            "id": 1,
                            "name": "lint",
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": SHA_B,
                            "details_url": "https://github.test/check/1",
                            "started_at": NOW,
                            "completed_at": LATER,
                        },
                        {
                            "id": 2,
                            "name": "security",
                            "status": "completed",
                            "conclusion": "failure",
                            "head_sha": SHA_B,
                            "details_url": "https://github.test/check/2",
                            "started_at": NOW,
                            "completed_at": LATER,
                        },
                        {
                            "id": 3,
                            "name": "unit",
                            "status": "queued",
                            "conclusion": None,
                            "head_sha": SHA_B,
                            "details_url": "https://github.test/check/3",
                            "started_at": None,
                            "completed_at": None,
                        },
                    ],
                }
            )
        }
    )

    result = value.checks(GitObjectId(SHA_B))
    by_name = {item.name: item for item in result.value}

    assert by_name["lint"].conclusion is ForgeConclusion.PASSED
    assert by_name["security"].conclusion is ForgeConclusion.FAILED
    assert by_name["unit"].status is ForgeRunStatus.QUEUED
    assert by_name["unit"].conclusion is None


def test_empty_checks_are_missing_and_api_failure_is_unavailable_not_failed() -> None:
    empty_url = f"{API}/repos/team/repo/commits/{SHA_B}/check-runs?filter=latest&per_page=100&page=1"
    empty, _ = adapter({empty_url: response({"total_count": 0, "check_runs": []})})
    missing = empty.checks(GitObjectId(SHA_B))

    failed_url = f"{API}/repos/team/repo/commits/{SHA_A}/check-runs?filter=latest&per_page=100&page=1"
    failed, _ = adapter({failed_url: response({"message": "down"}, status=503)})
    unavailable = failed.checks(GitObjectId(SHA_A))

    assert missing.availability is ForgeAvailability.MISSING
    assert unavailable.availability is ForgeAvailability.UNAVAILABLE
    assert unavailable.value is None
    assert "503" in unavailable.detail


def test_missing_workflow_run_is_distinct_from_failed_workflow() -> None:
    empty_url = f"{API}/repos/team/repo/actions/runs?head_sha={SHA_A}&per_page=100&page=1"
    empty, _ = adapter({empty_url: response({"total_count": 0, "workflow_runs": []})})
    assert empty.workflow_runs(GitObjectId(SHA_A)).availability is ForgeAvailability.MISSING

    failed_url = f"{API}/repos/team/repo/actions/runs?head_sha={SHA_B}&per_page=100&page=1"
    failed, _ = adapter(
        {
            failed_url: response(
                {
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 91,
                            "name": "CI",
                            "event": "pull_request",
                            "status": "completed",
                            "conclusion": "failure",
                            "head_sha": SHA_B,
                            "html_url": "https://github.test/actions/runs/91",
                            "run_started_at": NOW,
                            "updated_at": LATER,
                        }
                    ],
                }
            )
        }
    )
    result = failed.workflow_runs(GitObjectId(SHA_B))

    assert result.availability is ForgeAvailability.AVAILABLE
    assert result.value[0].conclusion is ForgeConclusion.FAILED


def test_reviews_comments_and_changed_files_map_to_provider_neutral_records() -> None:
    reviews_url = f"{API}/repos/team/repo/pulls/42/reviews?per_page=100&page=1"
    general_url = f"{API}/repos/team/repo/issues/42/comments?per_page=100&page=1"
    review_comments_url = f"{API}/repos/team/repo/pulls/42/comments?per_page=100&page=1"
    files_url = f"{API}/repos/team/repo/pulls/42/files?per_page=100&page=1"
    value, _ = adapter(
        {
            reviews_url: response(
                [
                    {
                        "id": 10,
                        "user": {"id": 8, "login": "reviewer"},
                        "state": "APPROVED",
                        "body": "Looks good",
                        "submitted_at": NOW,
                        "commit_id": SHA_B,
                    }
                ]
            ),
            general_url: response(
                [
                    {
                        "id": 11,
                        "user": {"id": 8, "login": "reviewer"},
                        "body": "General note",
                        "created_at": NOW,
                        "updated_at": NOW,
                        "html_url": "https://github.test/comment/11",
                    }
                ]
            ),
            review_comments_url: response(
                [
                    {
                        "id": 12,
                        "user": {"id": 8, "login": "reviewer"},
                        "body": "Inline note",
                        "created_at": NOW,
                        "updated_at": LATER,
                        "html_url": "https://github.test/comment/12",
                        "path": "src/new.py",
                        "line": 7,
                    }
                ]
            ),
            files_url: response(
                [
                    {
                        "filename": "src/new.py",
                        "previous_filename": "src/old.py",
                        "status": "renamed",
                        "additions": 3,
                        "deletions": 1,
                    }
                ]
            ),
        }
    )

    reviews = value.reviews("42")
    comments = value.comments("42")
    files = value.changed_files("42")

    assert reviews.value[0].state is ForgeReviewState.APPROVED
    assert {item.kind for item in comments.value} == {
        ForgeCommentKind.GENERAL,
        ForgeCommentKind.REVIEW,
    }
    inline = next(item for item in comments.value if item.kind is ForgeCommentKind.REVIEW)
    assert inline.path == "src/new.py"
    assert files.value[0].entry.kind is GitChangeKind.RENAMED
    assert files.value[0].entry.old_path == "src/old.py"


def test_transport_exception_and_404_are_safe_observation_states() -> None:
    url = f"{API}/repos/team/repo/pulls/404"
    missing, _ = adapter({url: response({"message": "not found"}, status=404)})
    assert missing.change_request("404").availability is ForgeAvailability.MISSING

    transport_url = f"{API}/repos/team/repo/pulls/43"
    unavailable, _ = adapter({transport_url: GithubTransportError("secret upstream detail")})
    result = unavailable.change_request("43")

    assert result.availability is ForgeAvailability.UNAVAILABLE
    assert result.detail == "GitHub transport unavailable"
    assert "secret upstream detail" not in result.detail
