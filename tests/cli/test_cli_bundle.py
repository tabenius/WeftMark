from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from weftmark.cli.main import EXIT_BUNDLE, main


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def setup(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
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
            "Portable",
            "--scope",
            "file:**",
        ]
    ) == 0
    assert main(
        [
            "--repo",
            str(tmp_path),
            "evidence",
            "run",
            "chg-1",
            "--id",
            "ev-1",
            "--command",
            sys.executable,
            "-c",
            "pass",
        ]
    ) == 0
    return tmp_path


def test_cli_exports_file_and_verifies_it_offline(tmp_path: Path, capsys) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    output = tmp_path / "exports" / "chg-1.json"
    assert main(
        [
            "--repo",
            str(repo),
            "--json",
            "bundle",
            "export",
            "chg-1",
            "--output",
            str(output),
        ]
    ) == 0
    exported = json.loads(capsys.readouterr().out)["bundle"]
    assert exported["digest"].startswith("sha256:")
    assert output.exists()

    assert main(
        [
            "--repo",
            str(tmp_path / "not-a-git-repository"),
            "--json",
            "bundle",
            "verify",
            str(output),
        ]
    ) == 0
    verified = json.loads(capsys.readouterr().out)["verification"]
    assert verified["change_set_id"] == "chg-1"
    assert verified["counts"]["evidence"] == 1


def test_cli_stdout_export_and_tamper_refusal(tmp_path: Path, capsys) -> None:
    repo = setup(tmp_path)
    capsys.readouterr()
    assert main(
        ["--repo", str(repo), "--json", "bundle", "export", "chg-1"]
    ) == 0
    bundle = json.loads(capsys.readouterr().out)
    assert bundle["contents"]["change_set"]["id"] == "chg-1"

    bundle["contents"]["change_set"]["goal"] = "tampered"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(bundle), encoding="utf-8")
    assert main(
        ["--repo", str(repo), "bundle", "verify", str(tampered)]
    ) == EXIT_BUNDLE
    assert "digest" in capsys.readouterr().err


def test_cli_imports_bundle_once_and_exposes_read_only_receipt(
    tmp_path: Path, capsys
) -> None:
    source = setup(tmp_path / "source")
    capsys.readouterr()
    output = tmp_path / "chg-1.json"
    assert main(
        [
            "--repo",
            str(source),
            "--json",
            "bundle",
            "export",
            "chg-1",
            "--output",
            str(output),
        ]
    ) == 0
    digest = json.loads(capsys.readouterr().out)["bundle"]["digest"]

    receiver = tmp_path / "receiver"
    receiver.mkdir()
    git(receiver, "init", "--initial-branch=main")
    git(receiver, "config", "user.name", "WeftMark Tests")
    git(receiver, "config", "user.email", "weftmark@example.invalid")
    (receiver / "README.md").write_text("receiver\n", encoding="utf-8")
    git(receiver, "add", "README.md")
    git(receiver, "commit", "-m", "receiver")

    command = [
        "--repo",
        str(receiver),
        "--json",
        "bundle",
        "import",
        str(output),
    ]
    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)["import"]
    assert first["imported"] is True
    assert first["sequence"] == 1

    assert main(command) == 0
    repeated = json.loads(capsys.readouterr().out)["import"]
    assert repeated["imported"] is False
    assert repeated["sequence"] == first["sequence"]

    assert main(["--repo", str(receiver), "--json", "bundle", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)["imports"]
    assert [value["digest"] for value in listed] == [digest]

    assert main(
        ["--repo", str(receiver), "--json", "bundle", "show", digest]
    ) == 0
    shown = json.loads(capsys.readouterr().out)["imported_bundle"]
    assert shown["digest"] == digest
    assert shown["bundle"]["contents"]["change_set"]["id"] == "chg-1"

    assert main(["--repo", str(receiver), "--json", "changeset", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["changesets"] == []
