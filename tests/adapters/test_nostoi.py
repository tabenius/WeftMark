from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from weftmark.adapters.nostoi import NostoiVerifierError, verify_ledger
from weftmark.adapters.nostoi import attestation_command


def test_verifies_explicit_weftmark_format_with_nostoi(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    calls: list[tuple[list[str], dict]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0,
            json.dumps({"format": "weftmark-ledger-v1", "records": 3,
                        "verified": 3, "ok": True, "head": None,
                        "problem": None, "path": str(ledger)}),
            "",
        )

    report = verify_ledger(ledger, executable="nostoi", run=run)

    assert report["ok"] is True
    assert calls[0][0] == [
        "nostoi", "verify", "--format", "weftmark-ledger-v1", "--json",
        str(ledger.resolve()),
    ]
    assert calls[0][1]["timeout"] == 10
    assert calls[0][1]["check"] is False


@pytest.mark.parametrize(
    "stdout,returncode",
    [("not json", 2), (json.dumps({"format": "weftmark-ledger-v1"}), 0)],
)
def test_rejects_invalid_native_verifier_reports(
    tmp_path: Path, stdout: str, returncode: int
) -> None:
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    with pytest.raises(NostoiVerifierError):
        verify_ledger(tmp_path / "ledger.jsonl", executable="nostoi", run=run)


def test_times_out_without_disclosing_process_output(tmp_path: Path) -> None:
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 10, output="sensitive")

    with pytest.raises(NostoiVerifierError, match="TimeoutExpired"):
        verify_ledger(tmp_path / "ledger.jsonl", executable="nostoi", run=run)


def test_failing_verifier_cannot_claim_success(tmp_path):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, '{"ok":true}', "")
    assert verify_ledger(tmp_path / "ledger", executable="nostoi", run=run)["ok"] is False


def test_attestation_verification_preserves_failure_and_requires_pin(monkeypatch, tmp_path):
    monkeypatch.setenv("WEFTMARK_NOSTOI", "nostoi")
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(NostoiVerifierError, match="fingerprint"):
        attestation_command(str(tmp_path / "ledger"), principal="alice", allowed_signers="signers")
    assert not calls
    assert attestation_command(str(tmp_path / "ledger"), principal="alice", allowed_signers="signers", fingerprint="SHA256:pin") == 1
    assert calls[0][0][-2:] == ["--fingerprint", "SHA256:pin"]
    assert "capture_output" not in calls[0][1]  # human terminal retained
