"""Failures must name the object, the cause and the next command."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from weftmark.cli import main as cli_main
from weftmark.cli.main import _emit_frog_parity, main


def _repo(path: Path) -> Path:
    subprocess.run(("git", "-C", str(path), "init", "--initial-branch=main"), check=True)
    subprocess.run(("git", "-C", str(path), "config", "user.name", "Tests"), check=True)
    subprocess.run(
        ("git", "-C", str(path), "config", "user.email", "tests@example.invalid"),
        check=True,
    )
    subprocess.run(("git", "-C", str(path), "commit", "--allow-empty", "-m", "base"), check=True)
    return path


def test_evidence_failure_is_actionable_in_json_and_human_output(
    tmp_path: Path, capsys
) -> None:
    repo = _repo(tmp_path)
    assert main([
        "--repo", str(repo), "changeset", "create", "ev-cs",
        "--goal", "Check failure reporting", "--scope", "file:src/**",
    ]) == 0
    capsys.readouterr()

    assert main([
        "--repo", str(repo), "--json", "evidence", "run", "ev-cs",
        "--id", "ev-fail", "--kind", "test", "--command", "false",
    ]) == 6
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["code"] == "EVIDENCE_FAILED"
    assert "ev-fail" in payload["error"]
    assert "false" in payload["error"]
    assert "weftmark evidence run --id" in payload["remediation"]
    assert payload["record"]["id"] == "ev-fail"
    assert payload["record"]["exit_code"] == 1

    assert main([
        "--repo", str(repo), "evidence", "run", "ev-cs",
        "--id", "ev-fail-human", "--kind", "test", "--command", "false",
    ]) == 6
    captured = capsys.readouterr()
    assert "EVIDENCE_FAILED" in captured.err
    assert "NEXT: Rerun" in captured.err


def test_invalid_runtime_snapshot_is_not_silent_success(
    tmp_path: Path, capsys
) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("not JSON")
    assert main(["--repo", "/no/repo", "--json", "system", "--snapshot", str(snapshot)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["code"] == "RUNTIME_SNAPSHOT_UNAVAILABLE"
    assert payload["record"]["source"] == str(snapshot)
    assert "RAGBAZ_RUNTIME_STATUS" in payload["remediation"]

    assert main(["--repo", "/no/repo", "system", "--snapshot", str(snapshot)]) == 2
    assert "RUNTIME_SNAPSHOT_UNAVAILABLE" in capsys.readouterr().err


def test_ledger_verification_failure_uses_the_error_contract(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli_main, "verify_with_nostoi", lambda path: {
        "ok": False, "format": "jsonl", "path": path, "verified": 2,
        "problem": "digest mismatch at sequence 3",
    })
    assert main(["--repo", "/no/repo", "--json", "ledger", "verify", "/tmp/ledger.jsonl"]) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["code"] == "LEDGER_INVALID"
    assert "digest mismatch" in payload["error"]
    assert "backup" in payload["remediation"]

    assert main(["--repo", "/no/repo", "ledger", "verify", "/tmp/ledger.jsonl"]) == 4
    assert "LEDGER_INVALID" in capsys.readouterr().err


def test_frog_parity_blocked_json_no_longer_claims_success(capsys) -> None:
    payload = {
        "cutover_ready": False,
        "source": {"label": "import", "digest": "sha256:abc"},
        "checks": [
            {"id": "eligibility", "classification": "match", "required": True},
            {"id": "collision_refusal", "classification": "unavailable", "required": True},
        ],
    }
    _emit_frog_parity(payload, json_output=True)
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["code"] == "FROG_CUTOVER_BLOCKED"
    assert "collision_refusal" in body["error"]
    _emit_frog_parity({**payload, "cutover_ready": True}, json_output=True)
    assert json.loads(capsys.readouterr().out)["ok"] is True
