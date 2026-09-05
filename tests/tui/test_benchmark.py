# tests/tui/test_benchmark.py
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from weftmark.cli.main import main as cli_main
from weftmark.tui.app import ReviewApp
from weftmark.tui.data import load_workspace_status

CHANGE_SET_COUNT = 50
BUDGET_SECONDS = 1.0


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def build_workspace_with_many_change_sets(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    for index in range(CHANGE_SET_COUNT):
        assert (
            cli_main(
                [
                    "--repo",
                    str(tmp_path),
                    "changeset",
                    "create",
                    f"bench-cs-{index}",
                    "--goal",
                    f"Simulated repository {index}",
                    "--scope",
                    f"contract:bench-{index}",
                ]
            )
            == 0
        )
    return tmp_path


def run(coro):
    return asyncio.run(coro)


async def _mount_after_loading(repo: Path) -> float:
    started = time.monotonic()
    initial = load_workspace_status(str(repo), None)
    app = ReviewApp(repo=str(repo), ledger_override=None, initial_status=initial)
    async with app.run_test() as pilot:
        await pilot.pause()
    return time.monotonic() - started


def test_startup_stays_interactive_with_fifty_change_sets(tmp_path: Path) -> None:
    repo = build_workspace_with_many_change_sets(tmp_path)

    elapsed = run(_mount_after_loading(repo))

    assert elapsed < BUDGET_SECONDS, (
        f"startup (load + first render) took {elapsed:.3f}s, "
        f"budget is {BUDGET_SECONDS}s"
    )
