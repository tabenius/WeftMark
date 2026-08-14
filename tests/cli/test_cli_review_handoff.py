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
            "Complete local review loop",
            "--scope",
            "file:**",
            "--scope",
            "contract:cli-v0",
        ]
    ) == 0
    return tmp_path


def run_test_evidence(repo: Path, id: str = "ev-test") -> int:
    return main(
        [
            "--repo",
            str(repo),
            "evidence",
            "run",
            "chg-1",
            "--id",
            id,
            "--kind",
            "test",
            "--command",
            sys.executable,
            "-c",
            "pass",
        ]
    )


def test_review_distinguishes_incomplete_then_ready_and_persists(
    tmp_path: Path, capsys
) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "review",
            "create",
            "chg-1",
            "--id",
            "review-incomplete",
        ]
    ) == EXIT_POLICY
    incomplete = json.loads(capsys.readouterr().out)
    assert incomplete["review"]["decision"]["outcome"] == "evidence_incomplete"

    assert run_test_evidence(repo) == 0
    capsys.readouterr()
    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "review",
            "create",
            "chg-1",
            "--id",
            "review-ready",
            "--author",
            "human-reviewer",
            "--require",
            "test",
            "--optional",
            "docs",
        ]
    ) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["ok"]
    assert ready["review"]["decision"]["outcome"] == "ready"
    assert ready["review"]["policy"]["issues"][0]["required"] is False

    assert main(
        ["--repo", str(repo), "--json", "review", "show", "review-ready"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["review"]["decision"]["author_id"] == "human-reviewer"
    assert main(
        ["--repo", str(repo), "--json", "review", "list", "--changeset", "chg-1"]
    ) == 0
    assert len(json.loads(capsys.readouterr().out)["reviews"]) == 2


def test_handoff_create_show_list_and_supersede_capture_proof_and_review(
    tmp_path: Path, capsys
) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    assert run_test_evidence(repo) == 0
    capsys.readouterr()
    assert main(
        [
            "--repo",
            str(repo),
            "review",
            "create",
            "chg-1",
            "--id",
            "review-1",
        ]
    ) == 0
    capsys.readouterr()

    create = [
        "--repo",
        str(repo),
        "--json",
        "handoff",
        "create",
        "chg-1",
        "--id",
        "handoff-1",
        "--task",
        "work-1",
        "--next",
        "Request merge",
        "--created-by",
        "worker-1",
        "--receiver",
        "worker-2",
    ]
    assert main(create) == 0
    first = json.loads(capsys.readouterr().out)["handoff"]
    assert first["evidence_ids"] == ["ev-test"]
    assert first["decision_ids"] == ["review-1"]
    assert first["generation"] == 1

    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "handoff",
            "create",
            "chg-1",
            "--id",
            "handoff-2",
            "--task",
        "work-1",
            "--next",
            "Merge after approval",
            "--supersedes",
            "handoff-1",
        ]
    ) == 0
    second = json.loads(capsys.readouterr().out)["handoff"]
    assert second["supersedes_id"] == "handoff-1"
    assert second["generation"] == 2

    assert main(
        ["--repo", str(repo), "--json", "handoff", "show", "handoff-2"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["handoff"]["next_action"] == "Merge after approval"
    assert main(
        ["--repo", str(repo), "--json", "handoff", "list", "--changeset", "chg-1"]
    ) == 0
    assert len(json.loads(capsys.readouterr().out)["handoffs"]) == 2


def test_handoff_refuses_dirty_worktree(tmp_path: Path, capsys) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    assert main(
        [
            "--repo",
            str(repo),
            "handoff",
            "create",
            "chg-1",
            "--id",
            "handoff-dirty",
            "--task",
            "task-1",
            "--next",
            "Continue",
        ]
    ) == EXIT_INVALID
    assert "clean worktree" in capsys.readouterr().err
