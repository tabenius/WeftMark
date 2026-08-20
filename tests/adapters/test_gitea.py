from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from weftmark.adapters.gitea import (
    GiteaForgeAdapter,
    GiteaHttpResponse,
    GiteaTransportError,
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
API = "https://gitea.test/api/v1"
WEB = "https://gitea.test"


def response(payload, *, status: int = 200) -> GiteaHttpResponse:
    return GiteaHttpResponse(status, json.dumps(payload).encode(), {})


@dataclass
class FixtureTransport:
    fixtures: dict[str, GiteaHttpResponse | Exception]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(self, url: str, *, headers: Mapping[str, str]) -> GiteaHttpResponse:
        self.calls.append((url, dict(headers)))
        value = self.fixtures[url]
        if isinstance(value, Exception):
            raise value
        return value


def adapter(fixtures, *, actions_supported=True, token=None):
    transport = FixtureTransport(fixtures)
    return (
        GiteaForgeAdapter(
            "team/repo",
            token=token,
            api_base=API,
            web_base=WEB,
            actions_supported=actions_supported,
            transport=transport,
        ),
        transport,
    )


def test_pull_request_and_capabilities_map_without_gitea_terms_in_contract() -> None:
    url = f"{API}/repos/team/repo/pulls/42"
    value, transport = adapter(
        {
            url: response(
                {
                    "number": 42,
                    "title": "Ship adapter",
                    "state": "open",
                    "merged": False,
                    "merged_at": None,
                    "draft": False,
                    "html_url": f"{WEB}/team/repo/pulls/42",
                    "updated_at": NOW,
                    "user": {"id": 7, "login": "alice"},
                    "head": {"ref": "feature", "sha": SHA_B},
                    "base": {"ref": "main", "sha": SHA_A},
                }
            )
        },
        token="fixture-token",
    )
    result = value.change_request("42")
    assert value.repository().provider == "gitea"
    assert value.capabilities().workflow_runs is True
    assert result.value.state is ForgeChangeState.OPEN
    assert result.value.source_branch == "feature"
    assert transport.calls[0][1]["Authorization"] == "token fixture-token"
    assert "fixture-token" not in repr(result)


def test_commit_statuses_and_actions_runs_preserve_truthful_states() -> None:
    statuses = (
        f"{API}/repos/team/repo/commits/{SHA_B}/statuses"
        "?sort=recentupdate&page=1&limit=100"
    )
    runs = (
        f"{API}/repos/team/repo/actions/runs"
        f"?head_sha={SHA_B}&page=1&limit=100"
    )
    value, _ = adapter(
        {
            statuses: response(
                [
                    {"id": 1, "context": "lint", "status": "success", "target_url": f"{WEB}/status/1", "created_at": NOW, "updated_at": LATER},
                    {"id": 2, "context": "security", "status": "failure", "target_url": f"{WEB}/status/2", "created_at": NOW, "updated_at": LATER},
                    {"id": 3, "context": "unit", "status": "pending", "target_url": f"{WEB}/status/3", "created_at": NOW, "updated_at": NOW},
                ]
            ),
            runs: response(
                {
                    "total_count": 1,
                    "workflow_runs": [
                        {"id": 91, "name": "CI", "event": "pull_request", "status": "success", "head_sha": SHA_B, "html_url": f"{WEB}/actions/runs/91", "run_started_at": NOW, "updated_at": LATER}
                    ],
                }
            ),
        }
    )
    checks = {item.name: item for item in value.checks(GitObjectId(SHA_B)).value}
    assert checks["lint"].conclusion is ForgeConclusion.PASSED
    assert checks["security"].conclusion is ForgeConclusion.FAILED
    assert checks["unit"].status is ForgeRunStatus.PENDING
    workflow = value.workflow_runs(GitObjectId(SHA_B)).value[0]
    assert workflow.status is ForgeRunStatus.COMPLETED
    assert workflow.conclusion is ForgeConclusion.PASSED


def test_actions_disabled_or_endpoint_absent_is_unsupported_not_missing() -> None:
    disabled, transport = adapter({}, actions_supported=False)
    result = disabled.workflow_runs(GitObjectId(SHA_B))
    assert result.availability is ForgeAvailability.UNSUPPORTED
    assert transport.calls == []

    url = (
        f"{API}/repos/team/repo/actions/runs"
        f"?head_sha={SHA_B}&page=1&limit=100"
    )
    absent, _ = adapter({url: response({"message": "not found"}, status=404)})
    result = absent.workflow_runs(GitObjectId(SHA_B))
    assert result.availability is ForgeAvailability.UNSUPPORTED


def test_reviews_comments_and_files_map_to_generic_records() -> None:
    reviews = f"{API}/repos/team/repo/pulls/42/reviews?page=1&limit=100"
    general = f"{API}/repos/team/repo/issues/42/comments?page=1&limit=100"
    review_comments = f"{API}/repos/team/repo/pulls/42/reviews/8/comments?page=1&limit=100"
    files = f"{API}/repos/team/repo/pulls/42/files?page=1&limit=100"
    value, _ = adapter(
        {
            reviews: response([
                {"id": 8, "user": {"id": 9, "login": "reviewer"}, "state": "APPROVED", "body": "LGTM", "submitted_at": NOW, "commit_id": SHA_B}
            ]),
            general: response([
                {"id": 11, "user": {"id": 9, "login": "reviewer"}, "body": "General", "created_at": NOW, "updated_at": NOW, "html_url": f"{WEB}/comment/11"}
            ]),
            review_comments: response([
                {"id": 12, "user": {"id": 9, "login": "reviewer"}, "body": "Inline", "created_at": NOW, "updated_at": LATER, "html_url": f"{WEB}/comment/12", "path": "src/new.py", "new_position": 7}
            ]),
            files: response([
                {"filename": "src/new.py", "previous_filename": "src/old.py", "status": "renamed", "additions": 3, "deletions": 1}
            ]),
        }
    )
    assert value.reviews("42").value[0].state is ForgeReviewState.APPROVED
    comments = value.comments("42").value
    assert {item.kind for item in comments} == {ForgeCommentKind.GENERAL, ForgeCommentKind.REVIEW}
    inline = next(item for item in comments if item.kind is ForgeCommentKind.REVIEW)
    assert inline.path == "src/new.py" and inline.line == 7
    changed = value.changed_files("42").value[0]
    assert changed.entry.kind is GitChangeKind.RENAMED
    assert changed.entry.old_path == "src/old.py"
    assert (changed.additions, changed.deletions) == (3, 1)


def test_transport_and_api_failures_remain_unavailable() -> None:
    url = f"{API}/repos/team/repo/pulls/42"
    transport, _ = adapter({url: GiteaTransportError("secret")})
    result = transport.change_request("42")
    assert result.availability is ForgeAvailability.UNAVAILABLE
    assert "secret" not in result.detail

    api, _ = adapter({url: response({"message": "down"}, status=503)})
    result = api.change_request("42")
    assert result.availability is ForgeAvailability.UNAVAILABLE
    assert "503" in result.detail
