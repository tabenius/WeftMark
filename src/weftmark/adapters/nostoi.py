"""Optional bridge to Nostoi's native chain verifier."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable


class NostoiVerifierError(RuntimeError):
    """Nostoi is unavailable or could not verify a WeftMark ledger."""


def verify_ledger(
    path: str | Path,
    *,
    executable: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict:
    """Verify a WeftMark JSONL ledger through Nostoi's native implementation.

    The command is argv-based, bounded by a timeout, and uses the explicit
    format so an unrelated or truncated file cannot be guessed into another
    chain format.
    """
    binary = executable or os.environ.get("WEFTMARK_NOSTOI") or shutil.which("nostoi")
    if not binary:
        raise NostoiVerifierError(
            "Nostoi CLI not found; install it or set WEFTMARK_NOSTOI to its path"
        )
    ledger = Path(path).expanduser().resolve()
    try:
        result = run(
            [binary, "verify", "--format", "weftmark-ledger-v1", "--json", str(ledger)],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NostoiVerifierError(f"Nostoi verification failed: {type(error).__name__}") from error
    try:
        report = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise NostoiVerifierError("Nostoi returned an invalid verification report") from error
    if not isinstance(report, dict) or not isinstance(report.get("ok"), bool):
        raise NostoiVerifierError("Nostoi returned an incomplete verification report")
    report["exit_code"] = result.returncode
    return report
