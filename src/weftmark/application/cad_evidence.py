"""Bounded CAD evidence import into the WeftMark local review ledger.

Candidates are not approval decisions or signed attestations. This adapter only
trusts a caller-provided candidate after comparing it to the locally retained
KiCad report and a separately verified Nostoi chain by the operator.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from weftmark.application.ledger import LedgerService
from weftmark.application.workspace import WorkspaceService

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")


class CadEvidenceImportError(ValueError):
    pass


def import_cad_evidence(
    ledger: LedgerService,
    workspace: WorkspaceService,
    candidate: Mapping[str, Any],
    *,
    report_bytes: bytes,
    nostoi_chain_verified: bool,
    verified_nostoi_event: Mapping[str, Any] | None = None,
    expected_source_sha256: str | None = None,
    observed_at: datetime,
) -> str:
    """Record a non-approval CAD test outcome, tied to an existing Change Set.

    This method does not verify Nostoi itself. Callers MUST independently verify
    the matching chain and receipt before asserting nostoi_chain_verified.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise CadEvidenceImportError("timezone-aware observation required")
    if not nostoi_chain_verified:
        raise CadEvidenceImportError("independent Nostoi chain verification required")
    if not isinstance(verified_nostoi_event, Mapping):
        raise CadEvidenceImportError("specific independently verified Nostoi event required")
    if not isinstance(report_bytes, bytes) or len(report_bytes) > 16_000_000:
        raise CadEvidenceImportError("bounded original report required")
    if not isinstance(candidate, Mapping) or candidate.get("schema") != "ragbaz.cad.weftmark-evidence-candidate/1":
        raise CadEvidenceImportError("invalid CAD evidence schema")
    ident, source, report = candidate.get("id"), candidate.get("source_sha256"), candidate.get("report_sha256")
    if not isinstance(ident, str) or not _ID.fullmatch(ident):
        raise CadEvidenceImportError("invalid evidence ID")
    if any(not isinstance(v, str) or not _SHA.fullmatch(v) for v in (source, report)):
        raise CadEvidenceImportError("invalid CAD evidence digests")
    if expected_source_sha256 != source:
        raise CadEvidenceImportError("CAD source revision is not independently bound")
    if hashlib.sha256(report_bytes).hexdigest() != report:
        raise CadEvidenceImportError("KiCad report hash mismatch")
    try:
        parsed = json.loads(report_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CadEvidenceImportError("invalid KiCad JSON report") from exc
    if not isinstance(parsed, dict):
        raise CadEvidenceImportError("KiCad report must be an object")
    if candidate.get("kind") != "test" or candidate.get("check") not in ("erc", "drc"):
        raise CadEvidenceImportError("unsupported evidence type")
    if candidate.get("state") not in ("passed", "failed"):
        raise CadEvidenceImportError("invalid test verdict")
    if candidate.get("approval") != "none" or candidate.get("certification") is not False:
        raise CadEvidenceImportError("CAD evidence cannot confer approval or certification")
    if candidate.get("freshness") != "saved-source-only":
        raise CadEvidenceImportError("missing saved-source limitation")
    if candidate.get("tool") != "kicad-cli" or not isinstance(candidate.get("tool_version"), str):
        raise CadEvidenceImportError("unrecognized KiCad producer")
    subject = candidate.get("subject")
    if not isinstance(subject, dict) or subject.get("kind") != "change_set" or not isinstance(subject.get("id"), str):
        raise CadEvidenceImportError("missing Change Set binding")
    cs = subject["id"]
    if not _ID.fullmatch(cs):
        raise CadEvidenceImportError("invalid Change Set ID")
    binding = workspace.require_change_set(cs)
    if binding.latest.dirty_paths:
        raise CadEvidenceImportError("cannot bind external evidence to dirty Change Set")
    receipt = candidate.get("nostoi")
    if not isinstance(receipt, dict) or receipt.get("kind") != "cad.evidence.completed" or receipt.get("subject") != ident:
        raise CadEvidenceImportError("missing matching Nostoi receipt")
    if not isinstance(receipt.get("receipt"), str) or not receipt["receipt"].strip():
        raise CadEvidenceImportError("empty Nostoi receipt")
    body = verified_nostoi_event.get("body")
    if (
        verified_nostoi_event.get("kind") != "cad.evidence.completed"
        or verified_nostoi_event.get("subject") != ident
        or not isinstance(body, Mapping)
        or body.get("schema") != "ragbaz.cad.nostoi-evidence/1"
        or body.get("evidence_id") != ident
        or body.get("change_set_id") != cs
        or body.get("source_sha256") != source
        or body.get("report_sha256") != report
        or body.get("mode") != candidate["check"]
        or body.get("verdict") != ("clear" if candidate["state"] == "passed" else "violations")
        or body.get("tool_version") != candidate["tool_version"]
        or body.get("freshness") != "saved-source-only"
        or body.get("certification") is not False
    ):
        raise CadEvidenceImportError("verified Nostoi event does not bind this evidence")
    # Nostoi CLI 'append' returns the sequence and digest. Require the
    # independently verified record to match that receipt exactly.
    match = re.fullmatch(r"([1-9][0-9]*) ([0-9a-f]{64})", receipt["receipt"].strip())
    if (
        match is None
        or verified_nostoi_event.get("seq") != int(match.group(1))
        or verified_nostoi_event.get("digest") != match.group(2)
    ):
        raise CadEvidenceImportError("verified Nostoi event does not match append receipt")
    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1 or not isinstance(artifacts[0], dict):
        raise CadEvidenceImportError("exactly one report artifact required")
    artifact = artifacts[0]
    if not isinstance(artifact.get("uri"), str) or not re.fullmatch(r"artifact:[A-Za-z0-9_.-]{1,160}",artifact["uri"]):
        raise CadEvidenceImportError("untrusted artifact URI")
    if artifact.get("digest") != report:
        raise CadEvidenceImportError("artifact/report digest mismatch")
    if ledger.latest(kind="cad.evidence.imported",entity_id=ident) is not None or ledger.latest(kind="evidence",entity_id=ident) is not None:
        raise CadEvidenceImportError("duplicate evidence ID")
    head_sha = binding.latest.head_sha
    # External producer must not inherit authority from the importing operator.
    payload = {
        "schema": "ragbaz.weftmark.external-cad-evidence/1",
        "id": ident,
        "kind": "test",
        "producer": {"kind":"system","id":"cad:kicad-cli"},
        "subject": {"kind":"change_set","id":cs},
        "state": candidate["state"],
        "source_sha256": source,
        "report_sha256": report,
        "bound_commit_sha": head_sha,
        "artifact_uri": artifact["uri"],
        "nostoi_subject": ident,
        "nostoi_receipt_sha256": hashlib.sha256(receipt["receipt"].encode()).hexdigest(),
        "limitations": ["saved-source-only","no-approval","no-electrical-certification"],
    }
    ledger.record(kind="cad.evidence.imported",entity_id=ident,payload=payload,recorded_at=observed_at)
    return ident
