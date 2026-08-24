"""Dependency-free local HTTP read/control surface for Kanban-style clients.

V0 deliberately binds only to loopback addresses. Remote/mobile exposure must be
provided by an authenticated TLS reverse proxy, SSH forwarding, Tailscale Serve,
or another transport boundary outside this process. Mutations are disabled by
default and require a separate write token plus explicit capabilities.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import secrets
import socket
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Callable, Sequence
from urllib.parse import unquote, urlsplit

from weftmark.adapters.git_local import LocalGit, LocalGitError
from weftmark.adapters.jsonl_ledger import JsonlLedger, JsonlLedgerError
from weftmark.application.claims import ClaimConflict, ClaimService, ClaimServiceError
from weftmark.application.control import ControlConflict, ControlServiceError
from weftmark.application.kanban_projection import (
    KANBAN_PROJECTION_SCHEMA,
    KanbanProjection,
    kanban_projection_to_payload,
    project_workspace,
)
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import LocalWorkflowError, LocalWorkflowService
from weftmark.application.status import StatusService
from weftmark.application.tasks import TaskService
from weftmark.application.task_claims import TaskClaimError
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceProducer, ProducerKind
from weftmark.http.control import (
    ControlCapability,
    ControlHttpError,
    ControlProvider,
    LocalControlProvider,
    dispatch_control,
    parse_control_route,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_CONTROL_BODY_BYTES = 64 * 1024
ProjectionProvider = Callable[[datetime], KanbanProjection]


class HttpReadError(ValueError):
    """Raised for unsafe or invalid HTTP surface configuration."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ledger_path(override: str | None, repository_id: str) -> Path:
    if override:
        return Path(override).resolve()
    if not repository_id.startswith("git:"):
        raise HttpReadError("local repository identity cannot select a ledger")
    return Path(repository_id.removeprefix("git:")) / "weftmark" / "ledger.jsonl"


def _require_loopback(host: str) -> None:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise HttpReadError(
            "v0 HTTP surface accepts only localhost or a literal loopback address"
        ) from exc
    if address.is_loopback:
        return
    raise HttpReadError(
        "v0 HTTP surface only binds to loopback; expose it remotely through an "
        "authenticated TLS reverse proxy or secure tunnel"
    )


def _read_token(path: str | None) -> str | None:
    if path is None:
        return None
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        raise HttpReadError("token file must not be empty")
    return token


class LocalProjectionProvider:
    """Compose the existing local services without refreshing Git implicitly."""

    def __init__(self, repo: str, ledger_override: str | None = None) -> None:
        git = LocalGit(repo)
        repository = git.repository()
        ledger = LedgerService(JsonlLedger(_ledger_path(ledger_override, repository.id)))
        workspace = WorkspaceService(git, ledger)
        claims = ClaimService(workspace, ledger)
        workflow = LocalWorkflowService(
            workspace,
            ledger,
            EvidenceProducer(ProducerKind.WORKER, "weftmark-http-read"),
        )
        self._status = StatusService(
            workspace,
            claims,
            workflow,
            tasks=TaskService(ledger),
            ledger=ledger,
        )
        self._lock = Lock()

    def __call__(self, observed_at: datetime) -> KanbanProjection:
        with self._lock:
            status = self._status.summarize(observed_at=observed_at)
        return project_workspace(status)


def make_handler(
    provider: ProjectionProvider,
    *,
    token: str | None = None,
    control_provider: ControlProvider | None = None,
    write_token: str | None = None,
    write_capabilities: frozenset[ControlCapability] = frozenset(),
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "WeftMarkHTTP/0"
        sys_version = ""

        def _authorized(self, expected_token: str | None) -> bool:
            if expected_token is None:
                return True
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {expected_token}"
            return secrets.compare_digest(supplied, expected)

        def _send_json(
            self,
            status: int,
            payload: object,
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if extra_headers:
                for name, value in extra_headers.items():
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _refuse_if_unauthorized(self) -> bool:
            if self._authorized(token):
                return False
            self._send_json(
                401,
                {"ok": False, "error": "unauthorized"},
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return True

        def _refuse_control_access(self, capability: ControlCapability) -> bool:
            if control_provider is None or write_token is None:
                self._send_json(404, {"ok": False, "error": "control_disabled"})
                return True
            if not self._authorized(write_token):
                self._send_json(
                    401,
                    {"ok": False, "error": "unauthorized"},
                    extra_headers={"WWW-Authenticate": "Bearer"},
                )
                return True
            if capability not in write_capabilities:
                self._send_json(
                    403,
                    {
                        "ok": False,
                        "error": "capability_not_granted",
                        "capability": capability.value,
                    },
                )
                return True
            return False

        def _read_control_json(self) -> dict[str, object] | None:
            media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                self._send_json(415, {"ok": False, "error": "json_required"})
                return None
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._send_json(411, {"ok": False, "error": "content_length_required"})
                return None
            try:
                length = int(raw_length)
            except ValueError:
                self._send_json(400, {"ok": False, "error": "invalid_content_length"})
                return None
            if length < 0:
                self._send_json(400, {"ok": False, "error": "invalid_content_length"})
                return None
            if length > MAX_CONTROL_BODY_BYTES:
                self._send_json(413, {"ok": False, "error": "payload_too_large"})
                return None
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"ok": False, "error": "invalid_json"})
                return None
            if not isinstance(payload, dict):
                self._send_json(400, {"ok": False, "error": "json_object_required"})
                return None
            return payload

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": "weftmark-kanban-read",
                        "schema": KANBAN_PROJECTION_SCHEMA,
                    },
                )
                return

            prefix = "/v0/kanban/changes/"
            task_prefix = "/v0/kanban/tasks/"
            is_workspace = path == "/v0/kanban"
            is_single = path.startswith(prefix) and len(path) > len(prefix)
            is_task = path.startswith(task_prefix) and len(path) > len(task_prefix)
            if not is_workspace and not is_single and not is_task:
                self._send_json(404, {"ok": False, "error": "not_found"})
                return
            if self._refuse_if_unauthorized():
                return

            projection = provider(_now())
            if is_workspace:
                self._send_json(200, kanban_projection_to_payload(projection))
                return

            workspace_payload = kanban_projection_to_payload(projection)
            if is_task:
                task_id = unquote(path[len(task_prefix) :])
                card = next(
                    (
                        value
                        for value in workspace_payload["plan_cards"]
                        if value["id"] == task_id
                    ),
                    None,
                )
                if card is None:
                    self._send_json(404, {"ok": False, "error": "task_not_found"})
                    return
                self._send_json(
                    200,
                    {
                        "schema": KANBAN_PROJECTION_SCHEMA,
                        "generated_at": projection.generated_at.isoformat(),
                        "authority": workspace_payload["authority"],
                        "card": card,
                    },
                )
                return

            change_set_id = unquote(path[len(prefix) :])
            card = next(
                (
                    value
                    for value in workspace_payload["cards"]
                    if value["id"] == change_set_id
                ),
                None,
            )
            if card is None:
                self._send_json(
                    404,
                    {"ok": False, "error": "change_set_not_found"},
                )
                return
            self._send_json(
                200,
                {
                    "schema": KANBAN_PROJECTION_SCHEMA,
                    "generated_at": projection.generated_at.isoformat(),
                    "authority": workspace_payload["authority"],
                    "card": card,
                },
            )

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            path = urlsplit(self.path).path
            route = parse_control_route(path)
            if route is None:
                self._method_not_allowed()
                return
            if self._refuse_control_access(route.capability):
                return
            idempotency_key = self.headers.get("Idempotency-Key", "").strip()
            if not idempotency_key:
                self._send_json(
                    428,
                    {"ok": False, "error": "idempotency_key_required"},
                )
                return
            payload = self._read_control_json()
            if payload is None:
                return
            assert control_provider is not None
            try:
                result = dispatch_control(
                    control_provider,
                    route,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    requested_at=_now(),
                )
            except ControlConflict:
                self._send_json(409, {"ok": False, "error": "idempotency_conflict"})
                return
            except ClaimConflict:
                self._send_json(409, {"ok": False, "error": "scope_conflict"})
                return
            except TaskClaimError:
                self._send_json(409, {"ok": False, "error": "task_claim_rejected"})
                return
            except ClaimServiceError:
                self._send_json(409, {"ok": False, "error": "claim_operation_rejected"})
                return
            except LocalWorkflowError:
                self._send_json(409, {"ok": False, "error": "handoff_rejected"})
                return
            except (ControlHttpError, ControlServiceError):
                self._send_json(400, {"ok": False, "error": "invalid_control_request"})
                return
            self._send_json(200, {"ok": True, "control": result.to_dict()})

        def _method_not_allowed(self) -> None:
            self._send_json(
                405,
                {"ok": False, "error": "method_not_allowed"},
                extra_headers={"Allow": "GET"},
            )

        def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
            self._method_not_allowed()

        def log_message(self, format: str, *args: object) -> None:
            # Keep stdlib request logging but avoid Python implementation/version data.
            super().log_message(format, *args)

    return Handler


class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True


class IPv4ThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def create_server(
    host: str,
    port: int,
    provider: ProjectionProvider,
    *,
    token: str | None = None,
    control_provider: ControlProvider | None = None,
    write_token: str | None = None,
    write_capabilities: frozenset[ControlCapability] = frozenset(),
) -> ThreadingHTTPServer:
    _require_loopback(host)
    if not (0 <= port <= 65535):
        raise HttpReadError("port must be between 0 and 65535")
    if control_provider is None:
        if write_token is not None or write_capabilities:
            raise HttpReadError(
                "write authorization cannot be configured without a control provider"
            )
    else:
        if write_token is None:
            raise HttpReadError("control provider requires a dedicated write token")
        if not write_capabilities:
            raise HttpReadError("control provider requires at least one write capability")
    server_type = IPv6ThreadingHTTPServer if ":" in host else IPv4ThreadingHTTPServer
    return server_type(
        (host, port),
        make_handler(
            provider,
            token=token,
            control_provider=control_provider,
            write_token=write_token,
            write_capabilities=write_capabilities,
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m weftmark.http.server",
        description="Serve the WeftMark Kanban projection and optional loopback control.",
    )
    parser.add_argument("--repo", default=".", help="path inside the Git repository")
    parser.add_argument("--ledger", help="override the local JSONL ledger path")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--token-file",
        help="optional file containing a bearer token for projection endpoints",
    )
    parser.add_argument(
        "--write-token-file",
        help="file containing the separate bearer token required for control endpoints",
    )
    parser.add_argument(
        "--write-capability",
        action="append",
        choices=tuple(value.value for value in ControlCapability),
        default=[],
        help="grant one control capability; repeat for multiple capabilities",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        token = _read_token(args.token_file)
        provider = LocalProjectionProvider(args.repo, args.ledger)
        write_token = _read_token(args.write_token_file)
        write_capabilities = frozenset(
            ControlCapability(value) for value in args.write_capability
        )
        control_provider: ControlProvider | None = None
        if write_token is not None:
            if not write_capabilities:
                raise HttpReadError(
                    "--write-token-file requires at least one --write-capability"
                )
            git = LocalGit(args.repo)
            repository = git.repository()
            control_provider = LocalControlProvider(
                args.repo,
                _ledger_path(args.ledger, repository.id),
            )
        elif write_capabilities:
            raise HttpReadError(
                "--write-capability requires --write-token-file"
            )
        server = create_server(
            args.host,
            args.port,
            provider,
            token=token,
            control_provider=control_provider,
            write_token=write_token,
            write_capabilities=write_capabilities,
        )
    except (HttpReadError, LocalGitError, JsonlLedgerError, OSError) as exc:
        print(f"error: {exc}")
        return 2

    address, port = server.server_address[:2]
    display_address = f"[{address}]" if ":" in address else address
    auth = " bearer-token" if token is not None else ""
    control = (
        ""
        if not write_capabilities
        else " control=" + ",".join(sorted(value.value for value in write_capabilities))
    )
    print(f"weftmark HTTP surface: http://{display_address}:{port}{auth}{control}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
