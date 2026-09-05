"""The terminal reviewer's Textual App and CLI/console-script entry points."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from textual.app import App

from weftmark.application.status import WorkspaceStatus
from weftmark.tui.data import TuiError, load_workspace_status
from weftmark.tui.screens import ChangeSetListScreen

# Mirrors weftmark.cli.main.EXIT_INVALID. Duplicated (not imported) so that
# weftmark.tui.app never depends on weftmark.cli.main — importing the base
# CLI module from here would work against the module boundary this package
# is built around (the base CLI must never import Textual, and this keeps
# the dependency arrow pointing the same direction it already does).
_EXIT_INVALID = 2


class ReviewApp(App):
    TITLE = "WeftMark Review"

    def __init__(
        self,
        *,
        repo: str,
        ledger_override: str | None,
        initial_status: WorkspaceStatus,
    ) -> None:
        super().__init__()
        self._repo = repo
        self._ledger_override = ledger_override
        self._workspace_status = initial_status

    def on_mount(self) -> None:
        self.push_screen(ChangeSetListScreen(self._workspace_status.change_sets))

    def reload_status(self) -> WorkspaceStatus:
        status = load_workspace_status(self._repo, self._ledger_override)
        self._workspace_status = status
        return status


def _emit_error(message: str, *, json_output: bool) -> None:
    """Mirrors weftmark.cli.main._emit_error's tiny if/else — not real
    business logic, so duplicating it here (rather than importing from
    weftmark.cli.main) keeps this module free of a dependency on the base
    CLI module."""

    if json_output:
        print(json.dumps({"ok": False, "error": message}, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)


def run_tui(repo: str, ledger_override: str | None, *, json_output: bool = False) -> int:
    try:
        initial_status = load_workspace_status(repo, ledger_override)
    except TuiError as error:
        _emit_error(str(error), json_output=json_output)
        return _EXIT_INVALID
    app = ReviewApp(
        repo=repo, ledger_override=ledger_override, initial_status=initial_status
    )
    app.run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weftmark-tui")
    parser.add_argument("--repo", default=".", help="path inside the Git repository")
    parser.add_argument("--ledger", help="override the local JSONL ledger path")
    args = parser.parse_args(argv)
    return run_tui(args.repo, args.ledger)


if __name__ == "__main__":
    sys.exit(main())
