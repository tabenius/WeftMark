from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from weftmark.adapters.forgejo import ForgejoForgeAdapter, ForgejoHttpResponse
from weftmark.adapters.gitea import GiteaForgeAdapter
from weftmark.application.ports.forge import ForgeAvailability, ForgeConclusion
from weftmark.application.ports.git import GitObjectId


SHA_A = "a" * 40
SHA_B = "b" * 40
NOW = "2026-08-20T10:00:00Z"
LATER = "2026-08-20T10:05:00Z"
API = "https://forgejo.test/api/v1"
WEB = "https://forgejo.test"


def response(payload, *, status: int = 200) -> ForgejoHttpResponse:
    return ForgejoHttpResponse(status, json.dumps(payload).encode(), {})


@dataclass
class FixtureTransport:
    fixtures: dict[str, ForgejoHttpResponse | Exception]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(self, url: str, *, headers: Mapping[str, str]) -> ForgejoHttpResponse:
        self.calls.append((url, dict(headers)))
        value = self.fixtures[url]
        if isinstance(value, Exception):
            raise value
        return value


def adapter(fixtures, *, actions_supported=True):
    transport = FixtureTransport(fixtures)
    return (
        ForgejoForgeAdapter(
            "team/repo",
            api_base=API,
            web_base=WEB,
            actions_supported=actions_supported,
            transport=transport,
        ),
        transport,
    )


def test_forgejo_is_a_separate_public_dialect_not_a_gitea_alias() -> None:
    value, _ = adapter({}, actions_supported=False)
    assert ForgejoForgeAdapter is not GiteaForgeAdapter
    assert value.repository().provider == "forgejo"
    assert value.repository().web_url == f"{WEB}/team/repo"
    assert value.capabilities().workflow_runs is False


def test_forgejo_pull_request_maps_through_shared_contract() -> None:
    url = f"{API}/repos/team/repo/pulls/42"
    value, _ = adapter(
        {
            url: response(
                {
                    "number": 42,
                    "title": "Forgejo support",
                    "state": "open",
                    "merged": False,
                    "merged_at": None,
                    "draft": False,
                    "html_url": f"{WEB}/team/repo/pulls/42",
                    "updated_at": NOW,
                    "user": {"id": 7, "login": "alice"},
                    "head": {"ref": "forgejo", "sha": SHA_B},
                    "base": {"ref": "main", "sha": SHA_A},
                }
            )
        }
    )
    result = value.change_request("42")
    assert result.availability is ForgeAvailability.AVAILABLE
    assert result.value.source_branch == "forgejo"
    assert str(result.value.head) == SHA_B


def test_forgejo_actions_enabled_and_disabled_have_distinct_observations() -> None:
    url = (
        f"{API}/repos/team/repo/actions/runs"
        f"?head_sha={SHA_B}&page=1&limit=100"
    )
    enabled, _ = adapter(
        {
            url: response(
                {
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 17,
                            "name": "Forgejo Actions CI",
                            "event": "pull_request",
                            "status": "success",
                            "head_sha": SHA_B,
                            "html_url": f"{WEB}/team/repo/actions/runs/17",
                            "run_started_at": NOW,
                            "updated_at": LATER,
                        }
                    ],
                }
            )
        }
    )
    run = enabled.workflow_runs(GitObjectId(SHA_B)).value[0]
    assert run.conclusion is ForgeConclusion.PASSED

    disabled, transport = adapter({}, actions_supported=False)
    result = disabled.workflow_runs(GitObjectId(SHA_B))
    assert result.availability is ForgeAvailability.UNSUPPORTED
    assert transport.calls == []


def test_forgejo_actions_endpoint_absence_is_unsupported_for_older_instances() -> None:
    url = (
        f"{API}/repos/team/repo/actions/runs"
        f"?head_sha={SHA_B}&page=1&limit=100"
    )
    value, _ = adapter({url: response({"message": "not found"}, status=404)})
    assert value.workflow_runs(GitObjectId(SHA_B)).availability is ForgeAvailability.UNSUPPORTED
