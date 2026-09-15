"""The read-only forge transports must not follow cross-origin redirects.

Regression tests for the ``boundary:remote-forge`` credential-exfiltration
and SSRF gap: stdlib ``urlopen`` follows a ``3xx`` automatically and resends
the ``Authorization`` header to the redirect target. Each adapter transport
now uses an opener that refuses any redirect leaving the request origin, so
a malicious forge cannot bounce the authenticated request to another host.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator

import pytest

from weftmark.adapters.azure_devops import (
    AzureDevopsTransportError,
    UrlLibAzureDevopsTransport,
)
from weftmark.adapters.bitbucket import BitbucketTransportError, UrlLibBitbucketTransport
from weftmark.adapters.gitea_like import (
    GiteaLikeTransportError,
    UrlLibGiteaLikeTransport,
)
from weftmark.adapters.github import GithubTransportError, UrlLibGithubTransport
from weftmark.adapters.gitlab import GitlabTransportError, UrlLibGitlabTransport

TRANSPORTS = [
    pytest.param(UrlLibGithubTransport, GithubTransportError, id="github"),
    pytest.param(UrlLibGitlabTransport, GitlabTransportError, id="gitlab"),
    pytest.param(UrlLibGiteaLikeTransport, GiteaLikeTransportError, id="gitea_like"),
    pytest.param(UrlLibBitbucketTransport, BitbucketTransportError, id="bitbucket"),
    pytest.param(
        UrlLibAzureDevopsTransport, AzureDevopsTransportError, id="azure_devops"
    ),
]

_TOKEN = "Bearer super-secret-forge-token"


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    """Records every request it receives; answers 200 with tiny JSON."""

    seen: list[str | None] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        type(self).seen.append(self.headers.get("Authorization"))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence test noise
        return


@pytest.fixture
def attacker() -> Iterator[tuple[str, list[str | None]]]:
    seen: list[str | None] = []

    class Handler(_CaptureHandler):
        pass

    Handler.seen = seen
    server = _serve(Handler)
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", seen
    finally:
        server.shutdown()


@pytest.mark.parametrize("transport_cls,error_cls", TRANSPORTS)
def test_cross_origin_redirect_is_refused_and_token_not_leaked(
    transport_cls: type, error_cls: type[Exception], attacker: tuple[str, list]
) -> None:
    attacker_url, attacker_seen = attacker

    class Forge(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"{attacker_url}/steal")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    forge = _serve(Forge)
    fhost, fport = forge.server_address
    try:
        transport = transport_cls(timeout_seconds=5)
        with pytest.raises(error_cls):
            transport.get(
                f"http://{fhost}:{fport}/api/v1/thing",
                headers={"Authorization": _TOKEN, "User-Agent": "WeftMark/test"},
            )
    finally:
        forge.shutdown()

    assert attacker_seen == [], "authenticated request must never reach the other origin"


@pytest.mark.parametrize("transport_cls,error_cls", TRANSPORTS)
def test_same_origin_redirect_is_followed(
    transport_cls: type, error_cls: type[Exception]
) -> None:
    class Forge(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.endswith("/final"):
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # same-origin relative redirect (e.g. trailing-slash normalization)
            self.send_response(302)
            self.send_header("Location", "/api/v1/final")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    forge = _serve(Forge)
    fhost, fport = forge.server_address
    try:
        transport = transport_cls(timeout_seconds=5)
        response = transport.get(
            f"http://{fhost}:{fport}/api/v1/start",
            headers={"Authorization": _TOKEN, "User-Agent": "WeftMark/test"},
        )
        assert response.status == 200
        assert b'"ok"' in response.body
    finally:
        forge.shutdown()
