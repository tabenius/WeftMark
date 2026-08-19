from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from weftmark.application.control import ControlResult
from weftmark.application.kanban_projection import KanbanProjection
from weftmark.http.control import ControlCapability
from weftmark.http.server import HttpReadError, create_server


NOW = datetime(2026, 8, 19, 16, 30, tzinfo=timezone.utc)


class ProjectionProvider:
    def __call__(self, observed_at: datetime) -> KanbanProjection:
        return KanbanProjection(
            generated_at=observed_at,
            active_claim_count=0,
            expired_claim_count=0,
            released_claim_count=0,
            cards=(),
        )


class StubControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def claim_task(self, task_id: str, **kwargs: object) -> ControlResult:
        self.calls.append(("claim", task_id, dict(kwargs)))
        return ControlResult(
            "claim_task",
            task_id,
            str(kwargs["idempotency_key"]),
            False,
            {"claim": {"id": kwargs["claim_id"]}},
        )

    def release_claim(self, claim_id: str, **kwargs: object) -> ControlResult:
        self.calls.append(("release", claim_id, dict(kwargs)))
        return ControlResult(
            "release_claim",
            claim_id,
            str(kwargs["idempotency_key"]),
            False,
            {"id": claim_id, "effective_state": "released"},
        )

    def create_handoff(self, change_set_id: str, **kwargs: object) -> ControlResult:
        self.calls.append(("handoff", change_set_id, dict(kwargs)))
        return ControlResult(
            "create_handoff",
            change_set_id,
            str(kwargs["idempotency_key"]),
            False,
            {"id": kwargs["handoff_id"], "change_set_id": change_set_id},
        )


def start_server(
    *,
    control: StubControl | None = None,
    write_token: str | None = None,
    capabilities: frozenset[ControlCapability] = frozenset(),
    read_token: str | None = None,
):
    server = create_server(
        "127.0.0.1",
        0,
        ProjectionProvider(),
        token=read_token,
        control_provider=control,
        write_token=write_token,
        write_capabilities=capabilities,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, f"http://{host}:{port}"


def post(
    url: str,
    payload: object,
    *,
    token: str | None = None,
    idempotency_key: str | None = "request-1",
    content_type: str = "application/json",
):
    headers = {"Content-Type": content_type}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    data = json.dumps(payload).encode()
    return urlopen(Request(url, data=data, headers=headers, method="POST"), timeout=2)


def error_json(exc: HTTPError) -> dict:
    return json.loads(exc.read())


def stop(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_control_is_disabled_by_default() -> None:
    server, thread, base = start_server()
    try:
        with pytest.raises(HTTPError) as exc:
            post(f"{base}/v0/control/tasks/task-a/claim", {})
        assert exc.value.code == 404
        assert error_json(exc.value)["error"] == "control_disabled"
    finally:
        stop(server, thread)


def test_control_configuration_requires_token_and_capability() -> None:
    control = StubControl()
    with pytest.raises(HttpReadError, match="write token"):
        create_server(
            "127.0.0.1",
            0,
            ProjectionProvider(),
            control_provider=control,
            write_capabilities=frozenset({ControlCapability.CLAIM}),
        )
    with pytest.raises(HttpReadError, match="at least one write capability"):
        create_server(
            "127.0.0.1",
            0,
            ProjectionProvider(),
            control_provider=control,
            write_token="write-secret",
        )


def test_write_token_and_capability_are_both_required() -> None:
    control = StubControl()
    server, thread, base = start_server(
        control=control,
        write_token="write-secret",
        capabilities=frozenset({ControlCapability.RELEASE}),
    )
    payload = {
        "change_set_id": "chg-a",
        "claim_id": "claim-a",
        "base_revision": "a" * 40,
        "agent_id": "worker-a",
        "session_id": "session-a",
        "lease_seconds": 300,
    }
    try:
        with pytest.raises(HTTPError) as exc:
            post(f"{base}/v0/control/tasks/task-a/claim", payload)
        assert exc.value.code == 401
        assert control.calls == []

        with pytest.raises(HTTPError) as exc:
            post(
                f"{base}/v0/control/tasks/task-a/claim",
                payload,
                token="write-secret",
            )
        assert exc.value.code == 403
        assert error_json(exc.value)["capability"] == "claim"
        assert control.calls == []
    finally:
        stop(server, thread)


def test_claim_route_requires_idempotency_and_dispatches_strict_payload() -> None:
    control = StubControl()
    server, thread, base = start_server(
        control=control,
        write_token="write-secret",
        capabilities=frozenset({ControlCapability.CLAIM}),
    )
    payload = {
        "change_set_id": "chg-a",
        "claim_id": "claim-a",
        "base_revision": "a" * 40,
        "agent_id": "worker-a",
        "session_id": "session-a",
        "lease_seconds": 300,
    }
    try:
        with pytest.raises(HTTPError) as exc:
            post(
                f"{base}/v0/control/tasks/task-a/claim",
                payload,
                token="write-secret",
                idempotency_key=None,
            )
        assert exc.value.code == 428
        assert control.calls == []

        with post(
            f"{base}/v0/control/tasks/task-a/claim",
            payload,
            token="write-secret",
            idempotency_key="claim-req-1",
        ) as response:
            body = json.loads(response.read())
        assert body["ok"] is True
        assert body["control"]["operation"] == "claim_task"
        assert body["control"]["idempotency_key"] == "claim-req-1"
        assert control.calls[0][0:2] == ("claim", "task-a")
        assert control.calls[0][2]["base_revision"] == "a" * 40
    finally:
        stop(server, thread)


def test_unknown_fields_and_wrong_media_type_fail_before_provider_call() -> None:
    control = StubControl()
    server, thread, base = start_server(
        control=control,
        write_token="write-secret",
        capabilities=frozenset({ControlCapability.RELEASE}),
    )
    url = f"{base}/v0/control/claims/claim-a/release"
    try:
        with pytest.raises(HTTPError) as exc:
            post(
                url,
                {
                    "agent_id": "worker-a",
                    "session_id": "session-a",
                    "reason": "handoff",
                    "surprise": True,
                },
                token="write-secret",
            )
        assert exc.value.code == 400
        assert control.calls == []

        with pytest.raises(HTTPError) as exc:
            post(
                url,
                {"agent_id": "worker-a", "session_id": "session-a", "reason": "handoff"},
                token="write-secret",
                content_type="text/plain",
            )
        assert exc.value.code == 415
        assert control.calls == []
    finally:
        stop(server, thread)


def test_handoff_route_uses_separate_write_token_from_read_token() -> None:
    control = StubControl()
    server, thread, base = start_server(
        control=control,
        write_token="write-secret",
        read_token="read-secret",
        capabilities=frozenset({ControlCapability.HANDOFF}),
    )
    payload = {
        "handoff_id": "handoff-a",
        "task_id": "task-a",
        "next_action": "Continue with another provider",
        "created_by": "worker-a",
        "known_failures": [],
    }
    try:
        with pytest.raises(HTTPError) as exc:
            post(
                f"{base}/v0/control/changes/chg-a/handoffs",
                payload,
                token="read-secret",
            )
        assert exc.value.code == 401
        assert control.calls == []

        with post(
            f"{base}/v0/control/changes/chg-a/handoffs",
            payload,
            token="write-secret",
        ) as response:
            body = json.loads(response.read())
        assert body["control"]["result"]["id"] == "handoff-a"
        assert control.calls[0][0:2] == ("handoff", "chg-a")
    finally:
        stop(server, thread)
