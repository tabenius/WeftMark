from __future__ import annotations

import json
import subprocess
from pathlib import Path

from weftmark.cli.main import EXIT_CONFLICT, EXIT_NOT_FOUND, main


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
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "base")
    for id, goal, file_scope in (
        ("chg-1", "First", "file:src/**"),
        ("chg-2", "Second", "file:docs/**"),
    ):
        assert main(
            [
                "--repo",
                str(tmp_path),
                "changeset",
                "create",
                id,
                "--goal",
                goal,
                "--scope",
                file_scope,
                "--scope",
                "contract:shared-api",
            ]
        ) == 0
    return tmp_path


def test_claim_acquire_show_list_renew_and_release_json(
    tmp_path: Path, capsys
) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    acquire = [
        "--repo",
        str(repo),
        "--json",
        "claim",
        "acquire",
        "chg-1",
        "--id",
        "claim-1",
        "--agent",
        "worker-1",
        "--session",
        "session-1",
        "--lease-seconds",
        "60",
    ]
    assert main(acquire) == 0
    created = json.loads(capsys.readouterr().out)["claim"]
    assert created["effective_state"] == "active"
    assert len(created["locks"]) == 2

    assert main(["--repo", str(repo), "--json", "claim", "show", "claim-1"]) == 0
    assert json.loads(capsys.readouterr().out)["claim"]["agent_id"] == "worker-1"
    assert main(
        ["--repo", str(repo), "--json", "claim", "list", "--changeset", "chg-1"]
    ) == 0
    assert len(json.loads(capsys.readouterr().out)["claims"]) == 1

    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "claim",
            "renew",
            "claim-1",
            "--agent",
            "worker-1",
            "--session",
            "session-1",
            "--extend-seconds",
            "60",
        ]
    ) == 0
    renewed = json.loads(capsys.readouterr().out)["claim"]
    assert renewed["locks"][0]["events"][-1]["kind"] == "renewed"

    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "claim",
            "release",
            "claim-1",
            "--agent",
            "worker-1",
            "--session",
            "session-1",
            "--reason",
            "slice completed",
        ]
    ) == 0
    released = json.loads(capsys.readouterr().out)["claim"]
    assert released["effective_state"] == "released"


def test_claim_conflict_has_distinct_exit_and_release_unblocks(
    tmp_path: Path, capsys
) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    first = [
        "--repo",
        str(repo),
        "claim",
        "acquire",
        "chg-1",
        "--id",
        "claim-1",
    ]
    second = [
        "--repo",
        str(repo),
        "--json",
        "claim",
        "acquire",
        "chg-2",
        "--id",
        "claim-2",
    ]
    assert main(first) == 0
    capsys.readouterr()
    assert main(second) == EXIT_CONFLICT
    refusal = json.loads(capsys.readouterr().out)
    assert not refusal["ok"]
    assert "contract:shared-api" in refusal["error"]

    assert main(
        [
            "--repo",
            str(repo),
            "claim",
            "release",
            "claim-1",
            "--reason",
            "done",
        ]
    ) == 0
    capsys.readouterr()
    assert main(second) == 0


def test_missing_claim_has_stable_not_found_exit(tmp_path: Path, capsys) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    assert main(["--repo", str(repo), "claim", "show", "missing"]) == EXIT_NOT_FOUND
    assert "not found" in capsys.readouterr().err
