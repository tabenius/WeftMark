"""First local WeftMark CLI surface."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from weftmark.adapters.git_local import LocalGit, LocalGitError
from weftmark.adapters.jsonl_ledger import JsonlLedger, JsonlLedgerError
from weftmark.application.change_binding import ChangeBindingError
from weftmark.application.ledger import LedgerService, LedgerServiceError
from weftmark.application.workspace import (
    WorkspaceError,
    WorkspaceService,
    binding_to_payload,
)
from weftmark.domain.changeset import ChangeSetError
from weftmark.domain.scope import Scope, ScopeError


EXIT_INVALID = 2
EXIT_NOT_FOUND = 3
EXIT_LEDGER = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weftmark",
        description="Local coordination, Git lineage, evidence, and review.",
    )
    parser.add_argument("--repo", default=".", help="path inside the Git repository")
    parser.add_argument("--ledger", help="override the local JSONL ledger path")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    changeset = commands.add_parser("changeset", help="manage Change Sets")
    changeset_commands = changeset.add_subparsers(dest="changeset_command", required=True)

    create = changeset_commands.add_parser("create", help="create and activate a Change Set")
    create.add_argument("id")
    create.add_argument("--goal", required=True)
    create.add_argument("--base", default="HEAD", help="base Git revision")
    create.add_argument(
        "--scope",
        action="append",
        required=True,
        help="canonical scope such as file:src/** or contract:api-v1",
    )

    show = changeset_commands.add_parser("show", help="show the latest Change Set snapshot")
    show.add_argument("id")
    refresh = changeset_commands.add_parser(
        "refresh", help="record the latest Git head, diff, and dirty paths"
    )
    refresh.add_argument("id")
    refresh.add_argument("--base", help="replace the tracked base revision")
    changeset_commands.add_parser("list", help="list latest Change Set snapshots")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        git = LocalGit(args.repo)
        repository = git.repository()
        ledger_path = _ledger_path(args.ledger, repository.id)
        ledger = LedgerService(JsonlLedger(ledger_path))
        workspace = WorkspaceService(git, ledger)

        if args.command == "changeset" and args.changeset_command == "create":
            result = _create_changeset(args, workspace)
            _emit(result, json_output=args.json, action="created")
            return 0
        if args.command == "changeset" and args.changeset_command == "show":
            binding = workspace.get_change_set(args.id)
            if binding is None:
                _emit_error(f"Change Set not found: {args.id}", json_output=args.json)
                return EXIT_NOT_FOUND
            _emit(binding_to_payload(binding), json_output=args.json)
            return 0
        if args.command == "changeset" and args.changeset_command == "refresh":
            binding = workspace.refresh_change_set(
                args.id,
                observed_at=_now(),
                base_revision=args.base,
            )
            _emit(
                binding_to_payload(binding),
                json_output=args.json,
                action="refreshed",
            )
            return 0
        if args.command == "changeset" and args.changeset_command == "list":
            result = [
                binding_to_payload(binding)
                for binding in workspace.list_change_sets()
            ]
            _emit_list(result, json_output=args.json)
            return 0
    except (JsonlLedgerError, LedgerServiceError) as error:
        _emit_error(str(error), json_output=args.json)
        return EXIT_LEDGER
    except (
        ChangeBindingError,
        ChangeSetError,
        LocalGitError,
        ScopeError,
        WorkspaceError,
        ValueError,
    ) as error:
        _emit_error(str(error), json_output=args.json)
        return EXIT_INVALID
    parser.error("unsupported command")
    return EXIT_INVALID


def _create_changeset(
    args: argparse.Namespace,
    workspace: WorkspaceService,
) -> dict[str, Any]:
    scopes = tuple(Scope.parse(value) for value in args.scope)
    timestamp = _now()
    binding = workspace.create_change_set(
        id=args.id,
        goal=args.goal,
        base_revision=args.base,
        scopes=scopes,
        created_at=timestamp,
    )
    return binding_to_payload(binding)


def _ledger_path(override: str | None, repository_id: str) -> Path:
    if override:
        return Path(override).resolve()
    if not repository_id.startswith("git:"):
        raise ChangeBindingError("local repository identity cannot select a ledger")
    return Path(repository_id.removeprefix("git:")) / "weftmark" / "ledger.jsonl"


def _emit(payload: dict[str, Any], *, json_output: bool, action: str | None = None) -> None:
    if json_output:
        print(json.dumps({"ok": True, "changeset": payload}, sort_keys=True))
        return
    prefix = f"{action} " if action else ""
    print(f"{prefix}{payload['id']}  {payload['state']}")
    print(f"  goal: {payload['goal']}")
    print(f"  branch: {payload['branch']}")
    print(f"  base: {payload['base_sha']}")
    print(f"  head: {payload['head_sha']}")
    print("  scopes: " + ", ".join(f"{scope['kind']}:{scope['key']}" for scope in payload["scopes"]))


def _emit_list(payloads: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "changesets": payloads}, sort_keys=True))
        return
    if not payloads:
        print("no Change Sets")
        return
    for payload in payloads:
        print(f"{payload['id']}  {payload['state']}  {payload['branch']}  {payload['goal']}")


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": False, "error": message}, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
