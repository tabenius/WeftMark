from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from weftmark.cli.main import main as cli_main
from weftmark.tui.data import TuiError, load_workspace_status


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
    return tmp_path


def test_load_workspace_status_reads_change_sets(tmp_path: Path) -> None:
    repo = setup(tmp_path)
    assert (
        cli_main(
            [
                "--repo",
                str(repo),
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

    status = load_workspace_status(str(repo), None)

    assert len(status.change_sets) == 1
    assert status.change_sets[0].id == "chg-1"
    assert status.change_sets[0].goal == "Ship it"
    assert status.change_sets[0].lifecycle_state == "active"


def test_load_workspace_status_wraps_git_errors(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    with pytest.raises(TuiError, match="not a git repository"):
        load_workspace_status(str(not_a_repo), None)
