"""Every forge adapter must fail safe on hostile or malformed forge data.

Forge responses are the untrusted side of ``boundary:remote-forge``. A
malformed body, a non-JSON body, or an error status must resolve to an
explicit unavailable/missing ``ForgeResult`` -- never an exception that
escapes the adapter or a fabricated change-request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import pytest

from weftmark.adapters.azure_devops import AzureDevopsForgeAdapter
from weftmark.adapters.bitbucket import BitbucketForgeAdapter
from weftmark.adapters.forgejo import ForgejoForgeAdapter
from weftmark.adapters.gitea import GiteaForgeAdapter
from weftmark.adapters.github import GithubForgeAdapter
from weftmark.adapters.gitlab import GitlabForgeAdapter
from weftmark.application.ports.forge import ForgeAvailability

# concrete adapter and a repository id valid for its constructor.
ADAPTERS = [
    pytest.param(GithubForgeAdapter, "team/repo", id="github"),
    pytest.param(GitlabForgeAdapter, "group/repo", id="gitlab"),
    pytest.param(GiteaForgeAdapter, "owner/repo", id="gitea"),
    pytest.param(ForgejoForgeAdapter, "owner/repo", id="forgejo"),
    pytest.param(BitbucketForgeAdapter, "workspace/repo", id="bitbucket"),
    pytest.param(AzureDevopsForgeAdapter, "org/project/repo", id="azure"),
]


@dataclass
class _Resp:
    """Duck-typed stand-in for every adapter's HttpResponse dataclass."""

    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass
class _FakeTransport:
    """Returns the same canned response for any URL the adapter requests."""

    resp: _Resp

    def get(self, url: str, *, headers: Mapping[str, str]) -> _Resp:
        return self.resp


def _build(adapter_cls: type, repo: str, resp: _Resp):
    return adapter_cls(repo, transport=_FakeTransport(resp))


@pytest.mark.parametrize("adapter_cls,repo", ADAPTERS)
def test_malformed_json_payload_is_unavailable(adapter_cls: type, repo: str) -> None:
    adapter = _build(adapter_cls, repo, _Resp(200, b'{"garbage": true}'))
    result = adapter.change_request("1")
    assert result.availability is ForgeAvailability.UNAVAILABLE


@pytest.mark.parametrize("adapter_cls,repo", ADAPTERS)
def test_non_json_body_is_unavailable(adapter_cls: type, repo: str) -> None:
    adapter = _build(adapter_cls, repo, _Resp(200, b"<html>not json</html>"))
    result = adapter.change_request("1")
    assert result.availability is ForgeAvailability.UNAVAILABLE


@pytest.mark.parametrize("adapter_cls,repo", ADAPTERS)
def test_not_found_status_is_missing(adapter_cls: type, repo: str) -> None:
    adapter = _build(adapter_cls, repo, _Resp(404, b'{"message": "nope"}'))
    result = adapter.change_request("1")
    assert result.availability is ForgeAvailability.MISSING


@pytest.mark.parametrize("adapter_cls,repo", ADAPTERS)
def test_server_error_status_is_unavailable(adapter_cls: type, repo: str) -> None:
    adapter = _build(adapter_cls, repo, _Resp(500, b'{"message": "boom"}'))
    result = adapter.change_request("1")
    assert result.availability is ForgeAvailability.UNAVAILABLE
