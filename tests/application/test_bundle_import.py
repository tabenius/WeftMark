from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.bundle import BundleError
from weftmark.application.bundle_import import BundleImportError, BundleImportService
from weftmark.application.ledger import LedgerService


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def bundle(change_set_id: str = "external-1") -> dict[str, object]:
    contents = {
        "format": "weftmark-portable-bundle-v1",
        "exported_at": NOW.isoformat(),
        "change_set": {"id": change_set_id},
        "claims": [],
        "evidence": [],
        "reviews": [],
        "handoffs": [],
    }
    canonical = json.dumps(
        contents, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return {
        "schema_version": 1,
        "digest": f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        "contents": contents,
    }


def service(tmp_path: Path) -> BundleImportService:
    return BundleImportService(
        LedgerService(JsonlLedger(tmp_path / "ledger.jsonl"))
    )


def test_import_is_idempotent_and_remains_a_separate_read_only_record(
    tmp_path: Path,
) -> None:
    imports = service(tmp_path)
    external = bundle()

    first = imports.import_bundle(external, imported_at=NOW)
    repeated = imports.import_bundle(external, imported_at=NOW)

    assert first.imported is True
    assert repeated.imported is False
    assert first.sequence == repeated.sequence == 1
    assert first.record.digest == external["digest"]
    assert imports.get(first.record.digest) == first.record
    assert imports.list() == (first.record,)

    entries = LedgerService(JsonlLedger(tmp_path / "ledger.jsonl")).snapshot()
    assert len(entries) == 1
    assert entries[0].kind == "imported_bundle"
    assert entries[0].entity_id == external["digest"]


def test_import_rejects_tampering_and_naive_receipt_time(tmp_path: Path) -> None:
    imports = service(tmp_path)
    tampered = bundle()
    tampered["contents"]["change_set"]["id"] = "tampered"  # type: ignore[index]

    with pytest.raises(BundleError, match="digest"):
        imports.import_bundle(tampered, imported_at=NOW)
    with pytest.raises(BundleImportError, match="timezone"):
        imports.import_bundle(bundle(), imported_at=NOW.replace(tzinfo=None))

    assert imports.list() == ()
