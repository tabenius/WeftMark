from __future__ import annotations

import json
import subprocess
from pathlib import Path

from weftmark.cli.main import EXIT_NOT_FOUND, main


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "WeftMark Tests")
    git(tmp_path, "config", "user.email", "weftmark@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path


def test_changeset_create_show_and_list_human_output(
    tmp_path: Path, capsys
) -> None:
    repo = repository(tmp_path)
    assert main(
        [
            "--repo",
            str(repo),
            "changeset",
            "create",
            "chg-1",
            "--goal",
            "Add local workflow",
            "--scope",
            "file:src/**",
            "--scope",
            "contract:cli-v0",
        ]
    ) == 0
    created = capsys.readouterr().out
    assert "created chg-1  active" in created
    assert "contract:cli-v0" in created

    assert main(["--repo", str(repo), "changeset", "show", "chg-1"]) == 0
    shown = capsys.readouterr().out
    assert "goal: Add local workflow" in shown

    assert main(["--repo", str(repo), "changeset", "list"]) == 0
    listed = capsys.readouterr().out
    assert "chg-1  active  main  Add local workflow" in listed


def test_json_output_is_structured_and_ledger_does_not_dirty_worktree(
    tmp_path: Path, capsys
) -> None:
    repo = repository(tmp_path)
    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "changeset",
            "create",
            "chg-json",
            "--goal",
            "Structured output",
            "--scope",
            "file:**",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"]
    assert payload["changeset"]["id"] == "chg-json"
    assert payload["changeset"]["head_sha"] == git(repo, "rev-parse", "HEAD")
    assert git(repo, "status", "--porcelain") == ""

    assert main(["--repo", str(repo), "--json", "changeset", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["changesets"][0]["id"] == "chg-json"


def test_missing_and_duplicate_changesets_have_stable_errors(
    tmp_path: Path, capsys
) -> None:
    repo = repository(tmp_path)
    assert main(["--repo", str(repo), "changeset", "show", "missing"]) == EXIT_NOT_FOUND
    assert "not found" in capsys.readouterr().err

    create = [
        "--repo",
        str(repo),
        "changeset",
        "create",
        "chg-1",
        "--goal",
        "Goal",
        "--scope",
        "file:**",
    ]
    assert main(create) == 0
    capsys.readouterr()
    assert main(create) != 0
    assert "already exists" in capsys.readouterr().err


def test_refresh_records_new_head_and_dirty_paths(tmp_path: Path, capsys) -> None:
    repo = repository(tmp_path)
    create = [
        "--repo",
        str(repo),
        "changeset",
        "create",
        "chg-1",
        "--goal",
        "Refresh facts",
        "--scope",
        "file:**",
    ]
    assert main(create) == 0
    capsys.readouterr()
    previous = git(repo, "rev-parse", "HEAD")
    (repo / "next.txt").write_text("next\n", encoding="utf-8")
    git(repo, "add", "next.txt")
    git(repo, "commit", "-m", "next")
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    assert main(
        ["--repo", str(repo), "--json", "changeset", "refresh", "chg-1"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)["changeset"]
    assert payload["head_sha"] != previous
    assert payload["dirty_paths"] == ["dirty.txt"]
    assert len(payload["observations"]) == 2
    assert payload["lineage"][-1]["kind"] == "head_advanced"


def test_cli_source_writes_only_through_ledger_service() -> None:
    source = Path(__file__).parents[2] / "src" / "weftmark" / "cli" / "main.py"
    text = source.read_text(encoding="utf-8")
    assert "LedgerService" in text
    assert ".write_text(" not in text
    assert "open(" not in text
