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
            "Status orientation",
            "--scope",
            "file:**",
        ]
    ) == 0
    assert main(
        [
            "--repo",
            str(tmp_path),
            "claim",
            "acquire",
            "chg-1",
            "--id",
            "claim-1",
        ]
    ) == 0
    return tmp_path


def test_status_has_compact_human_and_structured_json_output(
    tmp_path: Path, capsys
) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    assert main(["--repo", str(repo), "status"]) == 0
    human = capsys.readouterr().out
    assert "1 Change Sets  1 active claims" in human
    assert "chg-1  active  unreviewed  claim:claim-1  evidence:0/0" in human
    assert "observed head:" in human

    assert main(["--repo", str(repo), "--json", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"]
    assert payload["status"]["counts"]["change_sets"] == 1
    assert payload["status"]["change_sets"][0]["active_claim_ids"] == [
        "claim-1"
    ]


def test_status_read_does_not_append_or_refresh_records(tmp_path: Path, capsys) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    ledger = repo / ".git" / "weftmark" / "ledger.jsonl"
    before = ledger.read_bytes()
    assert main(["--repo", str(repo), "status"]) == 0
    capsys.readouterr()
    assert ledger.read_bytes() == before
