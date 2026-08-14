from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from weftmark.adapters.frog import FrogSnapshot
from weftmark.cli.main import main


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


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


def snapshot() -> FrogSnapshot:
    records = {
        "repos": (),
        "tasks": (
            {
                "slug": "external-1",
                "repo_path": "/source/project",
                "workflow_status": "in_progress",
                "priority": "p1",
                "title": "Continue migration",
            },
            {
                "slug": "external-2",
                "repo_path": "/source/other",
                "workflow_status": "done",
                "priority": "p2",
                "title": "Already complete",
            },
        ),
        "task_dependencies": (),
        "task_conflicts": (),
        "task_tags": (),
        "task_assignments": (),
        "agents": (),
        "files": (),
        "task_files": (),
        "locks": (),
    }
    contents = {
        "source_kind": "frog-agents-db",
        "source_label": "test-frog",
        "source_schema": {"migrations": ("001_initial.sql",)},
        "records": records,
    }
    canonical = json.dumps(
        contents, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    digest = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    return FrogSnapshot(
        "test-frog",
        ("001_initial.sql",),
        NOW,
        digest,
        records,
    )


def test_cli_records_and_inspects_frog_snapshot_receipt(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = repository(tmp_path)
    value = snapshot()
    monkeypatch.setattr(
        "weftmark.cli.main.read_frog_snapshot",
        lambda path, source_label, captured_at: value,
    )
    command = [
        "--repo",
        str(repo),
        "--json",
        "frog",
        "snapshot",
        "import",
        str(tmp_path / "AGENTS.db"),
        "--source-label",
        "test-frog",
    ]

    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)["frog_import"]
    assert first["imported"] is True
    assert first["counts"]["tasks"] == 2
    assert first["counts"]["locks"] == 0

    assert main(command) == 0
    repeated = json.loads(capsys.readouterr().out)["frog_import"]
    assert repeated["imported"] is False
    assert repeated["sequence"] == first["sequence"]

    assert main(
        ["--repo", str(repo), "--json", "frog", "snapshot", "list"]
    ) == 0
    listed = json.loads(capsys.readouterr().out)["frog_snapshots"]
    assert [item["digest"] for item in listed] == [value.digest]

    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "frog",
            "task",
            "list",
            value.digest,
            "--repo-path",
            "/source/project",
            "--workflow-status",
            "in_progress",
        ]
    ) == 0
    tasks = json.loads(capsys.readouterr().out)["frog_tasks"]
    assert [task["slug"] for task in tasks] == ["external-1"]

    assert main(
        ["--repo", str(repo), "--json", "frog", "snapshot", "show", value.digest]
    ) == 0
    shown = json.loads(capsys.readouterr().out)["frog_snapshot"]
    assert shown["snapshot"]["source_kind"] == "frog-agents-db"

    assert main(["--repo", str(repo), "--json", "changeset", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["changesets"] == []
