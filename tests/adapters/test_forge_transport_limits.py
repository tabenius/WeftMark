"""Response-size cap (F-2) and plaintext-credential guard (F-3).

F-2: a hostile forge (untrusted side of boundary:remote-forge) must not be
able to exhaust memory with an unbounded response body -- on either the
success or the HTTP-error path.

F-3: an adapter configured with a token must refuse a plaintext ``http://``
api_base so the credential is never sent in the clear.
"""

from __future__ import annotations

import http.server
import io
import threading
from collections.abc import Iterator

import pytest

import weftmark.adapters.azure_devops as azure_mod
import weftmark.adapters.bitbucket as bitbucket_mod
import weftmark.adapters.gitea_like as gitea_mod
import weftmark.adapters.github as github_mod
import weftmark.adapters.gitlab as gitlab_mod
from weftmark.adapters._http import ResponseTooLargeError, read_capped
from weftmark.adapters.azure_devops import (
    AzureDevopsAdapterError,
    AzureDevopsForgeAdapter,
    AzureDevopsTransportError,
    UrlLibAzureDevopsTransport,
)
from weftmark.adapters.bitbucket import (
    BitbucketAdapterError,
    BitbucketForgeAdapter,
    BitbucketTransportError,
    UrlLibBitbucketTransport,
)
from weftmark.adapters.forgejo import ForgejoAdapterError, ForgejoForgeAdapter
from weftmark.adapters.gitea import GiteaAdapterError, GiteaForgeAdapter
from weftmark.adapters.gitea_like import (
    GiteaLikeTransportError,
    UrlLibGiteaLikeTransport,
)
from weftmark.adapters.github import (
    GithubAdapterError,
    GithubForgeAdapter,
    GithubTransportError,
    UrlLibGithubTransport,
)
from weftmark.adapters.gitlab import (
    GitlabAdapterError,
    GitlabForgeAdapter,
    GitlabTransportError,
    UrlLibGitlabTransport,
)

# module, transport class, transport error -- one entry per real transport.
TRANSPORTS = [
    pytest.param(github_mod, UrlLibGithubTransport, GithubTransportError, id="github"),
    pytest.param(gitlab_mod, UrlLibGitlabTransport, GitlabTransportError, id="gitlab"),
    pytest.param(
        gitea_mod, UrlLibGiteaLikeTransport, GiteaLikeTransportError, id="gitea_like"
    ),
    pytest.param(
        bitbucket_mod, UrlLibBitbucketTransport, BitbucketTransportError, id="bitbucket"
    ),
    pytest.param(
        azure_mod, UrlLibAzureDevopsTransport, AzureDevopsTransportError, id="azure"
    ),
]

# concrete adapter, its config error, and a repository id valid for it.
ADAPTERS = [
    pytest.param(GithubForgeAdapter, GithubAdapterError, "owner/repo", id="github"),
    pytest.param(GitlabForgeAdapter, GitlabAdapterError, "group/repo", id="gitlab"),
    pytest.param(GiteaForgeAdapter, GiteaAdapterError, "owner/repo", id="gitea"),
    pytest.param(ForgejoForgeAdapter, ForgejoAdapterError, "owner/repo", id="forgejo"),
    pytest.param(
        BitbucketForgeAdapter, BitbucketAdapterError, "workspace/repo", id="bitbucket"
    ),
    pytest.param(
        AzureDevopsForgeAdapter, AzureDevopsAdapterError, "org/project/repo", id="azure"
    ),
]


# --- F-2 unit: the shared cap helper ---------------------------------------


def test_read_capped_returns_body_at_or_under_cap() -> None:
    assert read_capped(io.BytesIO(b"abc"), max_bytes=8) == b"abc"
    assert read_capped(io.BytesIO(b"12345678"), max_bytes=8) == b"12345678"


def test_read_capped_refuses_oversized_body() -> None:
    with pytest.raises(ResponseTooLargeError):
        read_capped(io.BytesIO(b"123456789"), max_bytes=8)


# --- F-2 integration: oversized bodies become "unavailable" ----------------


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def tiny_cap(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Shrink every transport's read cap so tests need only a few bytes."""

    def capped(fp: object) -> bytes:
        return read_capped(fp, max_bytes=16)  # type: ignore[arg-type]

    for module, *_ in (p.values for p in TRANSPORTS):
        monkeypatch.setattr(module, "read_capped", capped)
    yield


@pytest.mark.parametrize("module,transport_cls,error_cls", TRANSPORTS)
@pytest.mark.parametrize("status", [200, 500])
def test_oversized_body_is_reported_unavailable(
    module: object,
    transport_cls: type,
    error_cls: type[Exception],
    status: int,
    tiny_cap: None,
) -> None:
    big = b"x" * 4096

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(big)))
            self.end_headers()
            self.wfile.write(big)

        def log_message(self, *args: object) -> None:
            return

    server = _serve(Handler)
    host, port = server.server_address
    try:
        transport = transport_cls(timeout_seconds=5)
        with pytest.raises(error_cls):
            transport.get(f"http://{host}:{port}/api", headers={"User-Agent": "t"})
    finally:
        server.shutdown()


# --- F-3: a token must never ride a plaintext base -------------------------


@pytest.mark.parametrize("adapter_cls,error_cls,repo", ADAPTERS)
def test_http_api_base_with_token_is_refused(
    adapter_cls: type, error_cls: type[Exception], repo: str
) -> None:
    with pytest.raises(error_cls):
        adapter_cls(repo, token="secret", api_base="http://forge.internal")


@pytest.mark.parametrize("adapter_cls,error_cls,repo", ADAPTERS)
def test_https_api_base_with_token_is_allowed(
    adapter_cls: type, error_cls: type[Exception], repo: str
) -> None:
    adapter_cls(repo, token="secret", api_base="https://forge.internal")


@pytest.mark.parametrize("adapter_cls,error_cls,repo", ADAPTERS)
def test_http_api_base_without_token_is_allowed(
    adapter_cls: type, error_cls: type[Exception], repo: str
) -> None:
    adapter_cls(repo, api_base="http://forge.internal")
