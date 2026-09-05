from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from weftmark.cli.main import main as cli_main
from weftmark.tui.app import ReviewApp, run_tui
from weftmark.tui.data import load_workspace_status
from weftmark.tui.screens import ChangeSetListScreen


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    assert (
        cli_main(
            [
                "--repo",
                str(tmp_path),
                "changeset",
                "create",
                "chg-1",
                "--goal",
                "Ship it",
                "--scope",
                "file:**",
            ]
        )
        == 0
    )
    return tmp_path


def run(coro):
    return asyncio.run(coro)


async def _app_mounts_list_screen_with_loaded_status(repo: Path):
    initial = load_workspace_status(str(repo), None)
    app = ReviewApp(repo=str(repo), ledger_override=None, initial_status=initial)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ChangeSetListScreen)


def test_app_mounts_list_screen_with_loaded_status(tmp_path: Path) -> None:
    repo = setup(tmp_path)
    run(_app_mounts_list_screen_with_loaded_status(repo))


async def _reload_status_returns_fresh_workspace_status(repo: Path):
    initial = load_workspace_status(str(repo), None)
    app = ReviewApp(repo=str(repo), ledger_override=None, initial_status=initial)
    async with app.run_test():
        assert (
            cli_main(
                [
                    "--repo",
                    str(repo),
                    "changeset",
                    "create",
                    "chg-2",
                    "--goal",
                    "Second",
                    "--scope",
                    "file:other/**",
                ]
            )
            == 0
        )
        refreshed = app.reload_status()
        assert len(refreshed.change_sets) == 2


def test_reload_status_returns_fresh_workspace_status(tmp_path: Path) -> None:
    repo = setup(tmp_path)
    run(_reload_status_returns_fresh_workspace_status(repo))


def test_run_tui_reports_clear_error_for_invalid_repo(tmp_path, capsys) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    exit_code = run_tui(str(not_a_repo), None)

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not a git repository" in err
