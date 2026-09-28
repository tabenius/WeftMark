from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from weftmark.adapters.nostoi import NostoiVerifierError, verify_ledger


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
