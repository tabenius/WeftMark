from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from weftmark.cli.main import EXIT_INVALID, EXIT_POLICY, main


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "base")
    assert main(
        [
            "--repo",
            str(tmp_path),
            "changeset",
            "create",
            "chg-1",
            "--goal",
            "Lifecycle",
            "--scope",
            "file:**",
        ]
    ) == 0
    return tmp_path


def test_cli_transitions_ready_handoff_through_closed(
    tmp_path: Path, capsys
) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    assert main(
        [
            "--repo",
            str(repo),
            "evidence",
            "run",
            "chg-1",
            "--id",
            "ev-1",
            "--command",
            sys.executable,
            "-c",
            "pass",
        ]
    ) == 0
    capsys.readouterr()
    assert main(
        ["--repo", str(repo), "review", "create", "chg-1", "--id", "review-1"]
    ) == 0
    capsys.readouterr()
    assert main(
        [
            "--repo",
            str(repo),
            "handoff",
            "create",
            "chg-1",
            "--id",
            "handoff-1",
            "--task",
            "work-lifecycle",
            "--next",
            "Close",
        ]
    ) == 0
    capsys.readouterr()

    for state in ("review", "merged", "closed"):
        assert main(
            [
                "--repo",
                str(repo),
                "--json",
                "changeset",
                "transition",
                "chg-1",
                state,
            ]
        ) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["changeset"]["state"] == state


def test_cli_distinguishes_policy_gate_from_invalid_transition(
    tmp_path: Path, capsys
) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    assert main(
        [
            "--repo",
            str(repo),
            "review",
            "create",
            "chg-1",
            "--id",
            "review-incomplete",
        ]
    ) == EXIT_POLICY
    capsys.readouterr()
    assert main(
        ["--repo", str(repo), "changeset", "transition", "chg-1", "review"]
    ) == 0
    capsys.readouterr()
    assert main(
        ["--repo", str(repo), "changeset", "transition", "chg-1", "merged"]
    ) == EXIT_POLICY
    assert "current releasable" in capsys.readouterr().err

    other = setup(tmp_path / "other")
    capsys.readouterr()
    assert main(
        ["--repo", str(other), "changeset", "transition", "chg-1", "closed"]
    ) == EXIT_INVALID
    assert "active to closed" in capsys.readouterr().err
