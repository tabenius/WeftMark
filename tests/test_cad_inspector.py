from types import SimpleNamespace

from weftmark.application.cad_inspector import inspect_cad_evidence


class Ledger:
    def history(self, *, kind):
        assert kind == "cad.evidence.imported"
        return (
            SimpleNamespace(payload={
                "schema": "ragbaz.weftmark.external-cad-evidence/1",
                "id": "cad-abc",
                "subject": {"kind": "change_set", "id": "cs-42"},
                "state": "passed",
                "source_sha256": "a" * 64,
                "report_sha256": "b" * 64,
                "bound_commit_sha": "c" * 40,
                "nostoi_receipt_sha256": "d" * 64,
                "artifact_uri": "artifact:local-erc",
                "limitations": ["saved-source-only", "no-approval", "no-electrical-certification"],
            }),
            SimpleNamespace(payload={
                "schema": "ragbaz.weftmark.external-cad-evidence/1",
                "subject": {"kind": "change_set", "id": "other"}
            }),
        )


def test_cad_inspector_only_projects_matching_nonapproving_evidence():
    records = inspect_cad_evidence(Ledger(), change_set_id="cs-42")
    assert len(records) == 1
    row = records[0]
    assert row["test_outcome"] == "passed"
    assert row["approval"] == "none"
    assert row["ready_for_release"] is False
    assert row["artifact_reference"] == "artifact:local-erc"
    assert "report" not in row


def test_cad_inspector_rejects_invalid_change_set():
    import pytest
    with pytest.raises(ValueError):
        inspect_cad_evidence(Ledger(), change_set_id="")
