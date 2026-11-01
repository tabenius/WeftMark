"""Privacy-bounded, read-only CAD evidence inspector projection.

Reads only WeftMark's inert CAD import entries, never raw KiCad reports or
untrusted artifact URLs. It does not elevate evidence to review readiness.
"""
from __future__ import annotations

from typing import Any

from weftmark.application.ledger import LedgerService


def inspect_cad_evidence(ledger: LedgerService, *, change_set_id: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(change_set_id, str) or not change_set_id or len(change_set_id) > 120:
        raise ValueError("invalid Change Set ID")
    records = ledger.history(kind="cad.evidence.imported")
    result = []
    for entry in records:
        payload = entry.payload
        if payload.get("schema") != "ragbaz.weftmark.external-cad-evidence/1":
            continue
        if payload.get("subject") != {"kind": "change_set", "id": change_set_id}:
            continue
        result.append({
            "id": payload["id"],
            "change_set_id": change_set_id,
            "check": "KiCad ERC/DRC evidence",
            "test_outcome": payload["state"],
            "source_sha256": payload["source_sha256"],
            "report_sha256": payload["report_sha256"],
            "bound_commit_sha": payload["bound_commit_sha"],
            "nostoi_receipt_sha256": payload["nostoi_receipt_sha256"],
            "artifact_reference": payload["artifact_uri"],
            "limitations": tuple(payload["limitations"]),
            "approval": "none",
            "ready_for_release": False,
        })
    return tuple(result)
