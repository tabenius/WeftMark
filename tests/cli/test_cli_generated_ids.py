from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from weftmark.cli.main import main


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
    return tmp_path


def test_complete_cli_workflow_generates_ids_when_omitted(
    tmp_path: Path, capsys
) -> None:
    repo = setup(tmp_path)
    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "changeset",
            "create",
            "--goal",
            "Generated workflow",
            "--scope",
            "file:**",
        ]
    ) == 0
    change_set_id = json.loads(capsys.readouterr().out)["changeset"]["id"]
    assert change_set_id.startswith("chg-")

    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "claim",
            "acquire",
            change_set_id,
        ]
    ) == 0
    claim_id = json.loads(capsys.readouterr().out)["claim"]["id"]
    assert claim_id.startswith("claim-")

    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "evidence",
            "run",
            change_set_id,
            "--command",
            sys.executable,
            "-c",
            "pass",
        ]
    ) == 0
    evidence_id = json.loads(capsys.readouterr().out)["evidence"]["id"]
    assert evidence_id.startswith("evidence-")

    assert main(
        ["--repo", str(repo), "--json", "review", "create", change_set_id]
    ) == 0
    review_id = json.loads(capsys.readouterr().out)["review"]["decision"]["id"]
    assert review_id.startswith("review-")

    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "handoff",
            "create",
            change_set_id,
            "--task",
            "generated-workflow",
            "--next",
            "Continue",
        ]
    ) == 0
    handoff_id = json.loads(capsys.readouterr().out)["handoff"]["id"]
    assert handoff_id.startswith("handoff-")


def test_explicit_ids_remain_authoritative(tmp_path: Path, capsys) -> None:
    repo = setup(tmp_path)
    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "changeset",
            "create",
            "explicit-change",
            "--goal",
            "Explicit",
            "--scope",
            "file:**",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["changeset"]["id"] == "explicit-change"
