from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from weftmark.application.cad_evidence import CadEvidenceImportError, import_cad_evidence


class Ledger:
    def __init__(self):
        self.records = []
    def latest(self, *, kind, entity_id):
        return next((value for value in self.records if value[0] == kind and value[1] == entity_id), None)
    def record(self, *, kind, entity_id, payload, recorded_at):
        self.records.append((kind, entity_id, payload))
        return SimpleNamespace(digest="a" * 64)


class Workspace:
    def require_change_set(self, id):
        if id != "cs-42":
            raise ValueError("unknown Change Set")
        return SimpleNamespace(latest=SimpleNamespace(head_sha="b" * 40, dirty_paths=()))


RAW = b'{"violations":[]}'
DIGEST = hashlib.sha256(RAW).hexdigest()


def verified_event():
    return {
        "kind": "cad.evidence.completed",
        "subject": "cad-abc",
        "body": {
            "schema": "ragbaz.cad.nostoi-evidence/1",
            "evidence_id": "cad-abc", "change_set_id": "cs-42",
            "source_sha256": "a"*64, "report_sha256": DIGEST,
            "mode": "erc", "verdict": "clear",
            "tool_version": "10.0", "freshness": "saved-source-only",
            "certification": False,
        },
    }


def candidate():
    return {
        "schema": "ragbaz.cad.weftmark-evidence-candidate/1",
        "id": "cad-abc", "kind": "test",
        "subject": {"kind": "change_set", "id": "cs-42"},
        "state": "passed", "check": "erc", "tool": "kicad-cli",
        "tool_version": "10.0", "source_sha256": "a"*64,
        "report_sha256": DIGEST,
        "artifacts": [{"uri": "artifact:cad-erc", "digest": DIGEST}],
        "freshness": "saved-source-only", "approval": "none", "certification": False,
        "nostoi": {"kind": "cad.evidence.completed", "subject": "cad-abc", "receipt": "1 abc"}
    }


def import_it(data, *, verified=True, report=RAW, ledger=None):
    ledger = ledger or Ledger()
    result = import_cad_evidence(
        ledger, Workspace(), data,
        report_bytes=report, nostoi_chain_verified=verified,
        verified_nostoi_event=verified_event(), expected_source_sha256='a'*64,
        observed_at=datetime.now(timezone.utc)
    )
    return result, ledger


def test_import_creates_inert_nonapproval_ledger_record():
    ident, ledger = import_it(candidate())
    assert ident == "cad-abc"
    kind, _, payload = ledger.records[0]
    assert kind == "cad.evidence.imported"
    assert payload["bound_commit_sha"] == "b"*40
    assert payload["limitations"] == ["saved-source-only", "no-approval", "no-electrical-certification"]
    assert "receipt" not in payload
    assert "violations" not in json.dumps(payload)


def test_unverified_chain_rejected():
    with pytest.raises(CadEvidenceImportError):
        import_it(candidate(), verified=False)


def test_tampered_report_and_false_approval_rejected():
    with pytest.raises(CadEvidenceImportError, match="hash mismatch"):
        import_it(candidate(), report=b'{}')
    bad = candidate()
    bad["approval"] = "approved"
    with pytest.raises(CadEvidenceImportError, match="cannot confer"):
        import_it(bad)


def test_duplicate_candidate_refused():
    ledger = Ledger()
    import_it(candidate(), ledger=ledger)
    with pytest.raises(CadEvidenceImportError, match="duplicate"):
        import_it(candidate(), ledger=ledger)

def test_verified_event_body_and_source_digest_are_required():
    ledger=Ledger()
    data=candidate()
    with pytest.raises(CadEvidenceImportError,match="specific independently"):
        import_cad_evidence(ledger, Workspace(), data, report_bytes=RAW,
            nostoi_chain_verified=True, expected_source_sha256='a'*64,
            observed_at=datetime.now(timezone.utc))
    with pytest.raises(CadEvidenceImportError,match="not independently bound"):
        import_cad_evidence(ledger, Workspace(), data, report_bytes=RAW,
            nostoi_chain_verified=True, verified_nostoi_event=verified_event(),
            expected_source_sha256='b'*64, observed_at=datetime.now(timezone.utc))
    wrong=verified_event()
    wrong["body"]["report_sha256"]='0'*64
    with pytest.raises(CadEvidenceImportError,match="does not bind"):
        import_cad_evidence(ledger, Workspace(), data, report_bytes=RAW,
            nostoi_chain_verified=True, verified_nostoi_event=wrong,
            expected_source_sha256='a'*64, observed_at=datetime.now(timezone.utc))
