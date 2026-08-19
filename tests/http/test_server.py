from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from weftmark.application.kanban_projection import (
    KanbanCardProjection,
    KanbanLane,
    KanbanProjection,
)
from weftmark.http.server import HttpReadError, _require_loopback, create_server


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def projection() -> KanbanProjection:
    return KanbanProjection(
        generated_at=NOW,
        active_claim_count=1,
        expired_claim_count=0,
        released_claim_count=0,
        cards=(
            KanbanCardProjection(
                change_set_id="chg-1",
                title="HTTP projection",
                lane=KanbanLane.ACTIVE,
                lifecycle_state="active",
                readiness="unreviewed",
                branch="weft/chg-1",
                head_sha="abc123",
                observed_at=NOW,
                dirty_paths=(),
                active_claim_ids=("claim-1",),
                evidence_total=1,
                evidence_current=1,
                evidence_obsolete=0,
                evidence_failed=0,
                evidence_unavailable=0,
                latest_review_id=None,
                latest_review_outcome=None,
                latest_review_head_sha=None,
                latest_review_is_current=False,
                latest_handoff_id=None,
                latest_handoff_head_sha=None,
                latest_handoff_is_current=False,
                attention=(),
            ),
        ),
    )


class StubProvider:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, observed_at: datetime) -> KanbanProjection:
        assert observed_at.tzinfo is not None
        self.calls += 1
        return projection()


def start_server(*, token: str | None = None):
    provider = StubProvider()
    server = create_server("127.0.0.1", 0, provider, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return provider, server, thread, f"http://{host}:{port}"


def read_json(url: str, *, token: str | None = None) -> dict:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    with urlopen(Request(url, headers=headers), timeout=2) as response:
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        return json.loads(response.read())


def test_workspace_and_single_change_routes_share_projection() -> None:
    provider, server, thread, base = start_server()
    try:
        workspace = read_json(f"{base}/v0/kanban")
        single = read_json(f"{base}/v0/kanban/changes/chg-1")

        assert workspace["schema"] == "weftmark.kanban-projection.v0"
        assert workspace["authority"]["projection"] == "read_only"
        assert workspace["cards"][0]["id"] == "chg-1"
        assert single["card"] == workspace["cards"][0]
        assert provider.calls == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_unknown_change_is_404_after_one_authorized_projection_read() -> None:
    provider, server, thread, base = start_server()
    try:
        with pytest.raises(HTTPError) as exc:
            read_json(f"{base}/v0/kanban/changes/missing")
        assert exc.value.code == 404
        assert json.loads(exc.value.read())["error"] == "change_set_not_found"
        assert provider.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_unknown_route_is_workspace_blind() -> None:
    provider, server, thread, base = start_server()
    try:
        with pytest.raises(HTTPError) as exc:
            read_json(f"{base}/not-a-route")
        assert exc.value.code == 404
        assert provider.calls == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mutating_methods_are_refused_without_calling_provider() -> None:
    provider, server, thread, base = start_server()
    try:
        request = Request(f"{base}/v0/kanban", data=b"{}", method="POST")
        with pytest.raises(HTTPError) as exc:
            urlopen(request, timeout=2)
        assert exc.value.code == 405
        assert exc.value.headers["Allow"] == "GET"
        assert provider.calls == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_optional_bearer_token_protects_projection_but_not_health() -> None:
    provider, server, thread, base = start_server(token="secret-token")
    try:
        assert read_json(f"{base}/healthz")["ok"] is True
        assert provider.calls == 0

        with pytest.raises(HTTPError) as exc:
            read_json(f"{base}/v0/kanban")
        assert exc.value.code == 401
        assert exc.value.headers["WWW-Authenticate"] == "Bearer"
        assert provider.calls == 0

        payload = read_json(f"{base}/v0/kanban", token="secret-token")
        assert payload["cards"][0]["id"] == "chg-1"
        assert provider.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_v0_refuses_non_loopback_binding() -> None:
    provider = StubProvider()

    with pytest.raises(HttpReadError, match="only binds to loopback"):
        create_server("0.0.0.0", 0, provider)

    with pytest.raises(HttpReadError, match="only binds to loopback"):
        create_server("::", 0, provider, token="still-not-enough")


def test_loopback_policy_accepts_ipv4_ipv6_and_localhost() -> None:
    _require_loopback("127.0.0.1")
    _require_loopback("127.42.0.1")
    _require_loopback("::1")
    _require_loopback("localhost")

    with pytest.raises(HttpReadError, match="literal loopback"):
        _require_loopback("example.invalid")
