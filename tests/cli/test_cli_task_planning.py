from __future__ import annotations

import json
import subprocess
from pathlib import Path

from weftmark.cli.main import main


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository(path: Path) -> Path:
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.name", "WeftMark Tests")
    git(path, "config", "user.email", "weftmark@example.invalid")
    git(path, "commit", "--allow-empty", "-m", "base")
    return path


def command(repo: Path, *args: str) -> list[str]:
    return ["--repo", str(repo), "--json", *args]


def create(repo: Path, id: str, priority: str = "p1") -> int:
    return main(
        command(
            repo,
            "task",
            "create",
            id,
            "--title",
            id,
            "--why",
            "test native selection",
            "--what",
            "evaluate task intent",
            "--priority",
            priority,
        )
    )


def test_native_task_next_is_advisory_and_explains_skips(tmp_path: Path, capsys) -> None:
    repo = repository(tmp_path)
    for id, priority in (
        ("eligible", "p1"),
        ("active", "p0"),
        ("blocked-by-dep", "p0"),
        ("conflicted", "p0"),
    ):
        assert create(repo, id, priority) == 0
        capsys.readouterr()
    assert main(
        command(
            repo,
            "task",
            "transition",
            "active",
            "in_progress",
            "--reason",
            "claimed",
        )
    ) == 0
    capsys.readouterr()
    assert main(
        command(
            repo, "task", "dependency", "add", "blocked-by-dep", "active"
        )
    ) == 0
    capsys.readouterr()
    assert main(
        command(
            repo,
            "task",
            "conflict",
            "add",
            "conflicted",
            "active",
            "--reason",
            "shared surface",
        )
    ) == 0
    capsys.readouterr()

    assert main(command(repo, "task", "list")) == 0
    before = json.loads(capsys.readouterr().out)["tasks"]
    assert main(command(repo, "task", "next", "--limit", "3")) == 0
    selection = json.loads(capsys.readouterr().out)["task_selection"]
    assert [value["task"]["id"] for value in selection["tasks"]] == ["eligible"]
    reasons = {value["id"]: value["reasons"] for value in selection["skipped"]}
    assert reasons["blocked-by-dep"] == ["dependencies not done: active"]
    assert reasons["conflicted"] == ["conflicts in progress: active"]
    assert selection["authority"].startswith("advisory native intent")

    assert main(command(repo, "task", "list")) == 0
    after = json.loads(capsys.readouterr().out)["tasks"]
    assert after == before

