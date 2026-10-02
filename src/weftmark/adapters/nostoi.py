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
    # A contradictory success payload must not override a failing verifier.
    report["ok"] = report["ok"] and result.returncode == 0
    return report


def attestation_command(
    path: str, *, principal: str, key: str | None = None,
    allowed_signers: str | None = None, fingerprint: str | None = None,
    json_output: bool = False,
) -> int:
    """An explicit human CLI step; inherit the terminal for agent/key prompts.

    Never called by a service, TUI refresh, or evidence runner. Verification
    requires a fingerprint pin rather than treating a sidecar as verified.
    """
    binary = os.environ.get("WEFTMARK_NOSTOI") or shutil.which("nostoi")
    if not binary:
        raise NostoiVerifierError("Nostoi CLI not found; install it or set WEFTMARK_NOSTOI")
    if key is not None:
        argv = [binary, "attest", str(Path(path).expanduser().resolve()),
                "--format", "weftmark-ledger-v1", "--key", str(Path(key).expanduser()),
                "--principal", principal]
    else:
        if not allowed_signers or not fingerprint:
            raise NostoiVerifierError("attestation verification requires allowed signers and a fingerprint pin")
        argv = [binary, "verify-attestation", str(Path(path).expanduser().resolve()),
                "--allowed-signers", str(Path(allowed_signers).expanduser()),
                "--principal", principal, "--fingerprint", fingerprint]
    if json_output:
        argv.append("--json")
    try:
        return subprocess.run(argv, check=False).returncode
    except OSError as error:
        raise NostoiVerifierError("Nostoi attestation command unavailable") from error
