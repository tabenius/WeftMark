"""Trusted local CAD/Nostoi intake orchestration.

This is an operator-owned offline boundary, not a browser endpoint.
Verifies Nostoi with its CLI and resolves a saved CAD source itself.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from weftmark.application.cad_evidence import CadEvidenceImportError, import_cad_evidence

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT = re.compile(r"([1-9][0-9]*) ([0-9a-f]{64})\Z")


def import_verified_cad_evidence(
    ledger, workspace, candidate, *,
    report_path: Path, source_path: Path, nostoi_ledger: Path,
    observed_at: datetime, nostoi_bin: str = "nostoi",
    trusted_head: tuple[int, str] | None = None,
    artifact_root: Path | None = None,
    git_snapshot_root: Path | None = None,
    git_source_path: str | None = None,
    runner=subprocess.run,
):
    """Verify exact stored source, report and verified event before ledger import.

    The caller chooses local paths, but cannot supply a trust Boolean or event.
    An external anchor is still needed to detect a truncated/rebuilt valid chain.
    """
    if not isinstance(candidate, dict):
        raise CadEvidenceImportError("candidate must be a JSON object")
    expected = candidate.get("source_sha256")
    if not isinstance(expected, str) or not _SHA.fullmatch(expected):
        raise CadEvidenceImportError("invalid source SHA-256")
    if not isinstance(candidate.get("nostoi"), dict):
        raise CadEvidenceImportError("missing Nostoi receipt")
    receipt = candidate["nostoi"].get("receipt")
    match = _RECEIPT.fullmatch(receipt.strip()) if isinstance(receipt, str) else None
    if not match:
        raise CadEvidenceImportError("invalid Nostoi receipt")
    source = Path(source_path).resolve(strict=True)
    report = Path(report_path).resolve(strict=True)
    chain = Path(nostoi_ledger).resolve(strict=True)
    if artifact_root is not None:
        root = Path(artifact_root).resolve(strict=True)
        if not root.is_dir() or not report.is_relative_to(root):
            raise CadEvidenceImportError("report escapes trusted artifact root")
        if source.is_relative_to(root) or chain.is_relative_to(root):
            raise CadEvidenceImportError("source and ledger must be separate from artifact storage")
    if any(not p.is_file() for p in (source, report, chain)):
        raise CadEvidenceImportError("source, report and chain must be regular files")
    if source.stat().st_size > 64_000_000 or report.stat().st_size > 16_000_000:
        raise CadEvidenceImportError("oversized CAD source or report")
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise CadEvidenceImportError("saved source revision mismatch")
    report_bytes = report.read_bytes()
    if hashlib.sha256(report_bytes).hexdigest() != candidate.get("report_sha256"):
        raise CadEvidenceImportError("saved report digest mismatch")
    if (git_snapshot_root is None) != (git_source_path is None):
        raise CadEvidenceImportError("Git snapshot root and source path must be provided together")
    if git_snapshot_root is not None:
        repo = Path(git_snapshot_root).resolve(strict=True)
        rel = Path(git_source_path)
        if (
            not isinstance(git_source_path, str)
            or rel.is_absolute()
            or ".." in rel.parts
            or not git_source_path.endswith((".kicad_sch", ".kicad_pcb"))
            or not (repo / rel).resolve().is_relative_to(repo)
            or source != (repo / rel).resolve(strict=True)
        ):
            raise CadEvidenceImportError("invalid or mismatched Git-bound CAD source path")
        change_set_id = candidate.get("subject", {}).get("id")
        binding = workspace.require_change_set(change_set_id)
        commit = binding.latest.head_sha
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise CadEvidenceImportError("invalid Git head SHA")
        blob = runner(["git", "-C", str(repo), "show", f"{commit}:{git_source_path}"],
                      check=True, capture_output=True, timeout=30).stdout
        if not isinstance(blob, bytes) or hashlib.sha256(blob).hexdigest() != expected:
            raise CadEvidenceImportError("CAD source does not match reviewed Git revision")
    # No shell and no user-defined command arguments from the evidence itself.
    runner([nostoi_bin, "verify", str(chain)], check=True, capture_output=True,
           text=True, timeout=30)
    output = runner([nostoi_bin, "show", str(chain), match.group(1), "--json"],
                    check=True, capture_output=True, text=True, timeout=30)
    try:
        shown = json.loads(output.stdout)
        if shown.get("verified") is not True or not isinstance(shown.get("record"), dict):
            raise CadEvidenceImportError("specific Nostoi record is unverified")
        record = shown["record"]
    except (ValueError, AttributeError) as exc:
        raise CadEvidenceImportError("invalid verified Nostoi record") from exc
    if record.get("seq") != int(match.group(1)) or record.get("digest") != match.group(2):
        raise CadEvidenceImportError("Nostoi receipt does not match verified record")
    if trusted_head is not None:
        seq, digest = trusted_head
        if not isinstance(seq, int) or seq < 1 or not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise CadEvidenceImportError("invalid trusted Nostoi checkpoint")
        head = runner([nostoi_bin, "head", str(chain), "--json"],
                      check=True, capture_output=True, text=True, timeout=30)
        try:
            current_head = json.loads(head.stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise CadEvidenceImportError("invalid Nostoi head response") from exc
        if current_head.get("seq") != seq or current_head.get("digest") != digest:
            raise CadEvidenceImportError("Nostoi chain head does not match trusted checkpoint")
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise CadEvidenceImportError("CAD source changed during intake")
    return import_cad_evidence(
        ledger, workspace, candidate, report_bytes=report_bytes,
        nostoi_chain_verified=True, verified_nostoi_event=record,
        expected_source_sha256=expected, observed_at=observed_at,
    )
