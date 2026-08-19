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


def test_native_task_claim_cli_creates_and_recovers_local_authority(
    tmp_path: Path, capsys
) -> None:
    repo = repository(tmp_path)
    assert main(
        command(
            repo,
            "task",
            "create",
            "native-work",
            "--title",
            "Native work",
            "--why",
            "remove manual composition",
            "--what",
            "claim task scopes",
            "--priority",
            "p0",
            "--scope",
            "file:src/**",
            "--scope",
            "contract:native-v1",
        )
    ) == 0
    capsys.readouterr()
    claim = command(
        repo,
        "task",
        "claim",
        "native-work",
        "--changeset-id",
        "native-work-changeset",
        "--claim-id",
        "native-work-claim",
        "--agent",
        "worker-1",
        "--session",
        "session-1",
        "--lease-seconds",
        "300",
    )

    assert main(claim) == 0
    first = json.loads(capsys.readouterr().out)["task_claim"]
    assert first["claimed"] is True
    assert first["completed"] is True
    assert first["change_set"]["id"] == "native-work-changeset"
    assert first["claim"]["id"] == "native-work-claim"
    assert first["claim"]["effective_state"] == "active"

    assert main(claim) == 0
    repeated = json.loads(capsys.readouterr().out)["task_claim"]
    assert repeated["claimed"] is False
    assert repeated["change_set"]["id"] == "native-work-changeset"
    assert main(command(repo, "task", "show", "native-work")) == 0
    task = json.loads(capsys.readouterr().out)["task"]
    assert task["state"] == "in_progress"
    assert "native-work-claim" in task["state_events"][-1]["rationale"]

    assert main(
        command(
            repo,
            "task",
            "create",
            "scopeless",
            "--title",
            "Scopeless",
            "--why",
            "test refusal",
            "--what",
            "do not claim",
        )
    ) == 0
    capsys.readouterr()
    assert main(command(repo, "task", "claim", "scopeless")) == 2
    assert "declared scopes" in json.loads(capsys.readouterr().out)["error"]

