from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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


def test_native_task_cli_round_trip_relations_and_completion_refusal(
    tmp_path: Path, capsys
) -> None:
    repo = repository(tmp_path)
    create_a = command(
        repo,
        "task",
        "create",
        "task-a",
        "--title",
        "First native task",
        "--why",
        "Remove Frog from new work.",
        "--what",
        "Exercise native task commands.",
        "--priority",
        "p0",
        "--scope",
        "file:src/**",
    )
    create_b = command(
        repo,
        "task",
        "create",
        "task-b",
        "--title",
        "Dependent native task",
        "--why",
        "Plan the next slice.",
        "--what",
        "Wait for task-a.",
        "--priority",
        "p1",
        "--scope",
        "contract:native-task-v1",
    )

    assert main(create_a) == 0
    created_a = json.loads(capsys.readouterr().out)["task"]
    assert created_a["id"] == "task-a"
    assert created_a["state"] == "todo"
    assert main(create_b) == 0
    capsys.readouterr()
    assert main(create_a) == 2
    assert "already exists" in json.loads(capsys.readouterr().out)["error"]

    assert main(command(repo, "task", "list", "--state", "todo")) == 0
    listed = json.loads(capsys.readouterr().out)["tasks"]
    assert [value["id"] for value in listed] == ["task-a", "task-b"]
    assert main(command(repo, "task", "show", "task-a")) == 0
    assert json.loads(capsys.readouterr().out)["task"]["title"] == "First native task"

    assert main(
        command(
            repo,
            "task",
            "transition",
            "task-a",
            "in_progress",
            "--actor",
            "worker-1",
            "--reason",
            "native claim acquired",
        )
    ) == 0
    transitioned = json.loads(capsys.readouterr().out)["task"]
    assert transitioned["state"] == "in_progress"
    assert transitioned["state_events"][-1]["actor_id"] == "worker-1"

    dependency = command(
        repo, "task", "dependency", "add", "task-b", "task-a"
    )
    assert main(dependency) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True
    assert main(dependency) == 0
    assert json.loads(capsys.readouterr().out)["created"] is False
    assert main(
        command(repo, "task", "dependency", "list", "--task", "task-b")
    ) == 0
    dependencies = json.loads(capsys.readouterr().out)["dependencies"]
    assert dependencies[0]["depends_on_task_id"] == "task-a"

    conflict = command(
        repo,
        "task",
        "conflict",
        "add",
        "task-b",
        "task-a",
        "--reason",
        "shared contract",
    )
    assert main(conflict) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True
    assert main(conflict) == 0
    assert json.loads(capsys.readouterr().out)["created"] is False
    assert main(
        command(repo, "task", "conflict", "list", "--task", "task-a")
    ) == 0
    conflicts = json.loads(capsys.readouterr().out)["conflicts"]
    assert conflicts[0]["first_task_id"] == "task-a"

    assert main(command(repo, "task", "show", "missing")) == 3
    assert "not found" in json.loads(capsys.readouterr().out)["error"]
    with pytest.raises(SystemExit) as refused:
        main(
            command(
                repo,
                "task",
                "transition",
                "task-a",
                "done",
                "--reason",
                "skip completion gates",
            )
        )
    assert refused.value.code == 2

