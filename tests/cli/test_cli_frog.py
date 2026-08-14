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
                "created_at": "2026-08-14T10:00:00+00:00",
            },
            {
                "slug": "external-2",
                "repo_path": "/source/other",
                "workflow_status": "done",
                "priority": "p2",
                "title": "Already complete",
                "created_at": "2026-08-14T09:00:00+00:00",
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
        [
            "--repo",
            str(repo),
            "--json",
            "frog",
            "task",
            "next",
            value.digest,
            "--repo-path",
            "/source/project",
            "--limit",
            "2",
        ]
    ) == 0
    selection = json.loads(capsys.readouterr().out)["frog_task_selection"]
    assert [item["task"]["slug"] for item in selection["tasks"]] == [
        "external-1"
    ]
    assert selection["authority"].startswith("advisory imported intent")
    assert selection["ignored_observations"] == {"assignments": 0, "locks": 0}

    assert main(
        ["--repo", str(repo), "--json", "frog", "snapshot", "show", value.digest]
    ) == 0
    shown = json.loads(capsys.readouterr().out)["frog_snapshot"]
    assert shown["snapshot"]["source_kind"] == "frog-agents-db"

    assert main(["--repo", str(repo), "--json", "changeset", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["changesets"] == []

    promote = [
        "--repo",
        str(repo),
        "--json",
        "frog",
        "task",
        "promote",
        value.digest,
        "external-1",
        "--id",
        "promoted-1",
        "--scope",
        "file:src/**",
        "--scope",
        "contract:migration-v1",
    ]
    assert main(promote) == 0
    promoted = json.loads(capsys.readouterr().out)["frog_promotion"]
    assert promoted["promoted"] is True
    assert promoted["source_task_slug"] == "external-1"
    assert promoted["change_set"]["id"] == "promoted-1"
    assert promoted["change_set"]["state"] == "active"

    assert main(promote) == 0
    repeated = json.loads(capsys.readouterr().out)["frog_promotion"]
    assert repeated["promoted"] is False
    assert repeated["change_set"]["id"] == "promoted-1"

    assert main(["--repo", str(repo), "--json", "claim", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["claims"] == []

    claim = [
        "--repo",
        str(repo),
        "--json",
        "frog",
        "task",
        "claim",
        value.digest,
        "external-1",
        "--id",
        "promoted-1",
        "--claim-id",
        "promoted-claim-1",
        "--agent",
        "local-worker",
        "--session",
        "local-session",
        "--scope",
        "file:src/**",
        "--scope",
        "contract:migration-v1",
    ]
    assert main(claim) == 0
    claimed = json.loads(capsys.readouterr().out)["frog_task_claim"]
    assert claimed["claimed"] is True
    assert claimed["claim"]["effective_state"] == "active"
    assert claimed["authority"] == "local Change Set and semantic claim"

    assert main(claim) == 0
    repeated_claim = json.loads(capsys.readouterr().out)["frog_task_claim"]
    assert repeated_claim["claimed"] is False
    assert repeated_claim["claim"]["id"] == "promoted-claim-1"

    refused = [
        "--repo",
        str(repo),
        "--json",
        "frog",
        "task",
        "claim",
        value.digest,
        "external-2",
        "--scope",
        "file:other/**",
    ]
    assert main(refused) == 2
    assert "not eligible" in json.loads(capsys.readouterr().out)["error"]
