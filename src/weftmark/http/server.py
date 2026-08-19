"""Dependency-free local HTTP read surface for Kanban-style clients.

V0 deliberately binds only to loopback addresses. Remote/mobile exposure must be
provided by an authenticated TLS reverse proxy, SSH forwarding, Tailscale Serve,
or another transport boundary outside this process.
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
from weftmark.application.claims import ClaimService
from weftmark.application.kanban_projection import (
    KANBAN_PROJECTION_SCHEMA,
    KanbanProjection,
    kanban_projection_to_payload,
    project_workspace,
)
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.status import StatusService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceProducer, ProducerKind


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ProjectionProvider = Callable[[datetime], KanbanProjection]


class HttpReadError(ValueError):
    """Raised for unsafe or invalid HTTP read-surface configuration."""


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
        self._status = StatusService(workspace, claims, workflow)
        self._lock = Lock()

    def __call__(self, observed_at: datetime) -> KanbanProjection:
        with self._lock:
            status = self._status.summarize(observed_at=observed_at)
        return project_workspace(status)


def make_handler(
    provider: ProjectionProvider,
    *,
    token: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "WeftMarkHTTP/0"
        sys_version = ""

        def _authorized(self) -> bool:
            if token is None:
                return True
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
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
            if self._authorized():
                return False
            self._send_json(
                401,
                {"ok": False, "error": "unauthorized"},
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return True

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
            is_workspace = path == "/v0/kanban"
            is_single = path.startswith(prefix) and len(path) > len(prefix)
            if not is_workspace and not is_single:
                self._send_json(404, {"ok": False, "error": "not_found"})
                return
            if self._refuse_if_unauthorized():
                return

            projection = provider(_now())
            if is_workspace:
                self._send_json(200, kanban_projection_to_payload(projection))
                return

            change_set_id = unquote(path[len(prefix) :])
            workspace_payload = kanban_projection_to_payload(projection)
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

        def _method_not_allowed(self) -> None:
            self._send_json(
                405,
                {"ok": False, "error": "method_not_allowed"},
                extra_headers={"Allow": "GET"},
            )

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            self._method_not_allowed()

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
) -> ThreadingHTTPServer:
    _require_loopback(host)
    if not (0 <= port <= 65535):
        raise HttpReadError("port must be between 0 and 65535")
    server_type = IPv6ThreadingHTTPServer if ":" in host else IPv4ThreadingHTTPServer
    return server_type((host, port), make_handler(provider, token=token))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m weftmark.http.server",
        description="Serve the read-only WeftMark Kanban projection on loopback.",
    )
    parser.add_argument("--repo", default=".", help="path inside the Git repository")
    parser.add_argument("--ledger", help="override the local JSONL ledger path")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--token-file",
        help="optional file containing a bearer token for projection endpoints",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        token = _read_token(args.token_file)
        provider = LocalProjectionProvider(args.repo, args.ledger)
        server = create_server(args.host, args.port, provider, token=token)
    except (HttpReadError, LocalGitError, JsonlLedgerError, OSError) as exc:
        print(f"error: {exc}")
        return 2

    address, port = server.server_address[:2]
    display_address = f"[{address}]" if ":" in address else address
    auth = " bearer-token" if token is not None else ""
    print(f"weftmark read surface: http://{display_address}:{port}{auth}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
