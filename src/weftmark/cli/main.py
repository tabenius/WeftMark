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
from weftmark.application.change_binding import ChangeBindingError, ChangeBindingService
from weftmark.application.ledger import LedgerService, LedgerServiceError
from weftmark.domain.changeset import ChangeSet, ChangeSetError
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

        if args.command == "changeset" and args.changeset_command == "create":
            result = _create_changeset(args, git, ledger)
            _emit(result, json_output=args.json, action="created")
            return 0
        if args.command == "changeset" and args.changeset_command == "show":
            entry = ledger.latest(kind="changeset", entity_id=args.id)
            if entry is None:
                _emit_error(f"Change Set not found: {args.id}", json_output=args.json)
                return EXIT_NOT_FOUND
            _emit(entry.payload, json_output=args.json)
            return 0
        if args.command == "changeset" and args.changeset_command == "list":
            latest: dict[str, dict[str, Any]] = {}
            for entry in ledger.history(kind="changeset"):
                latest[entry.entity_id] = entry.payload
            result = [latest[id] for id in sorted(latest)]
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
        ValueError,
    ) as error:
        _emit_error(str(error), json_output=args.json)
        return EXIT_INVALID
    parser.error("unsupported command")
    return EXIT_INVALID


def _create_changeset(
    args: argparse.Namespace,
    git: LocalGit,
    ledger: LedgerService,
) -> dict[str, Any]:
    if ledger.latest(kind="changeset", entity_id=args.id) is not None:
        raise ChangeSetError(f"Change Set already exists: {args.id}")
    repository = git.repository()
    head = git.head()
    base = git.commit(args.base).id
    scopes = tuple(Scope.parse(value) for value in args.scope)
    if head.branch is None or repository.worktree is None:
        raise ChangeBindingError("Change Set creation requires an attached working tree")
    timestamp = _now()
    planned = ChangeSet.plan(
        id=args.id,
        goal=args.goal,
        repository_id=repository.id,
        base_sha=str(base),
        branch=head.branch,
        worktree=repository.worktree,
        scopes=tuple(scope.canonical for scope in scopes),
        at=timestamp,
    )
    binding = ChangeBindingService(git).create(
        planned,
        base_revision=args.base,
        observed_at=timestamp,
    )
    payload = {
        "id": binding.change_set.id,
        "goal": binding.change_set.goal,
        "repository_id": binding.change_set.repository_id,
        "base_sha": binding.latest.base_sha,
        "head_sha": binding.latest.head_sha,
        "base_revision": binding.base_revision,
        "branch": binding.latest.branch,
        "worktree": binding.latest.worktree,
        "scopes": [scope.to_dict() for scope in scopes],
        "state": binding.change_set.state.value,
        "observation_id": binding.latest.id,
        "changed_paths": list(binding.latest.changed_paths),
        "dirty_paths": list(binding.latest.dirty_paths),
        "created_at": binding.change_set.created_at.isoformat(),
        "updated_at": binding.change_set.updated_at.isoformat(),
    }
    ledger.record(
        kind="changeset",
        entity_id=binding.change_set.id,
        payload=payload,
        recorded_at=timestamp,
    )
    return payload


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
