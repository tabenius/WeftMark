from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from weftmark.cli.main import (
    EXIT_EVIDENCE_FAILED,
    EXIT_EVIDENCE_UNAVAILABLE,
    EXIT_INVALID,
    EXIT_POLICY,
    main,
)


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
            "Exercise scope and evidence",
            "--scope",
            "file:src/**",
            "--scope",
            "contract:api-v1",
        ]
    ) == 0
    return tmp_path


def test_scope_audit_reports_clean_and_blocking_drift(
    tmp_path: Path, capsys
) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    (repo / "src").mkdir()
    (repo / "src" / "ok.py").write_text("ok = True\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "inside")

    assert main(
        ["--repo", str(repo), "--json", "scope", "audit", "chg-1"]
    ) == 0
    clean = json.loads(capsys.readouterr().out)
    assert clean["ok"]
    assert clean["scope_audit"]["actual_paths"] == ["src/ok.py"]

    (repo / "outside.txt").write_text("outside\n", encoding="utf-8")
    git(repo, "add", "outside.txt")
    git(repo, "commit", "-m", "outside")
    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "scope",
            "audit",
            "chg-1",
            "--semantic-change",
            "schema:new-ledger",
        ]
    ) == EXIT_POLICY
    drift = json.loads(capsys.readouterr().out)
    assert not drift["ok"]
    assert len(drift["scope_audit"]["findings"]) == 2


def test_scope_amend_widens_a_blocked_change_set_back_into_scope(
    tmp_path: Path, capsys
) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    (repo / "outside.txt").write_text("outside\n", encoding="utf-8")
    git(repo, "add", "outside.txt")
    git(repo, "commit", "-m", "outside")

    assert main(
        ["--repo", str(repo), "--json", "scope", "audit", "chg-1"]
    ) == EXIT_POLICY
    blocked = json.loads(capsys.readouterr().out)
    assert not blocked["ok"]
    assert blocked["scope_audit"]["findings"][0]["scope"]["key"] == "outside.txt"

    assert main(
        [
            "--repo",
            str(repo),
            "scope",
            "amend",
            "chg-1",
            "--scope",
            "file:outside.txt",
            "--reason",
            "outside.txt is a genuinely related config touched by this change",
        ]
    ) == 0
    amended = capsys.readouterr().out
    assert "scope amended chg-1  active" in amended
    assert "file:outside.txt" in amended

    assert main(
        ["--repo", str(repo), "--json", "scope", "audit", "chg-1"]
    ) == 0
    clean_again = json.loads(capsys.readouterr().out)
    assert clean_again["ok"]
    assert clean_again["scope_audit"]["findings"] == []

    assert main(["--repo", str(repo), "--json", "changeset", "show", "chg-1"]) == 0
    shown = json.loads(capsys.readouterr().out)["changeset"]
    assert shown["scope_amendments"][-1]["added_scopes"] == ["file:outside.txt"]
    assert (
        shown["scope_amendments"][-1]["reason"]
        == "outside.txt is a genuinely related config touched by this change"
    )

    denied = main(
        [
            "--repo",
            str(repo),
            "--json",
            "scope",
            "amend",
            "chg-1",
            "--scope",
            "file:outside.txt",
            "--reason",
            "already declared, should be refused",
        ]
    )
    assert denied == EXIT_INVALID


def test_evidence_run_show_and_list_are_durable(tmp_path: Path, capsys) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    command = [sys.executable, "-c", "print('verified')"]
    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "evidence",
            "run",
            "chg-1",
            "--id",
            "ev-1",
            "--kind",
            "test",
            "--command",
            *command,
        ]
    ) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["evidence"]["state"] == "passed"
    assert captured["evidence"]["command"]["argv"] == command
    assert "verified" not in captured["evidence"]

    assert main(
        ["--repo", str(repo), "--json", "evidence", "show", "ev-1"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["evidence"]["id"] == "ev-1"
    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "evidence",
            "list",
            "--changeset",
            "chg-1",
        ]
    ) == 0
    assert len(json.loads(capsys.readouterr().out)["evidence"]) == 1


def test_evidence_exit_codes_distinguish_failure_and_unavailability(
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
            "ev-fail",
            "--command",
            sys.executable,
            "-c",
            "raise SystemExit(9)",
        ]
    ) == EXIT_EVIDENCE_FAILED
    capsys.readouterr()
    assert main(
        [
            "--repo",
            str(repo),
            "evidence",
            "run",
            "chg-1",
            "--id",
            "ev-missing",
            "--command",
            str(repo / "missing-command"),
        ]
    ) == EXIT_EVIDENCE_UNAVAILABLE
    assert "unavailable" in capsys.readouterr().out


def test_dirty_tree_refuses_evidence_before_command_runs(tmp_path: Path, capsys) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    marker = repo / "marker"
    assert main(
        [
            "--repo",
            str(repo),
            "evidence",
            "run",
            "chg-1",
            "--id",
            "ev-dirty",
            "--command",
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').close()",
        ]
    ) == EXIT_INVALID
    assert "clean worktree" in capsys.readouterr().err
    assert not marker.exists()
