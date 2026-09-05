"""The terminal reviewer's Textual App and CLI/console-script entry points."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from textual.app import App

from weftmark.application.status import WorkspaceStatus
from weftmark.tui.data import TuiError, load_workspace_status
from weftmark.tui.screens import ChangeSetListScreen


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


def run_tui(repo: str, ledger_override: str | None) -> int:
    try:
        initial_status = load_workspace_status(repo, ledger_override)
    except TuiError as error:
        print(f"weftmark tui: {error}", file=sys.stderr)
        return 1
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
