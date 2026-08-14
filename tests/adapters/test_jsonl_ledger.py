from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weftmark.adapters.jsonl_ledger import JsonlLedger, JsonlLedgerError, LedgerCorruption
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import LEDGER_GENESIS_DIGEST, LedgerDraft, LedgerHeadChanged


NOW = datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc)


def test_adapter_appends_persists_and_hash_chains_records(tmp_path: Path) -> None:
    path = tmp_path / ".weftmark" / "ledger.jsonl"
    service = LedgerService(JsonlLedger(path))
    first = service.record(kind="changeset", entity_id="chg-1", payload={"state": "active"}, recorded_at=NOW)
    second = service.record(kind="evidence", entity_id="ev-1", payload={"state": "passed"}, recorded_at=NOW)

    restored = JsonlLedger(path).entries()
    assert restored == (first, second)
    assert second.previous_digest == first.digest
    assert second.sequence == 2
    assert path.stat().st_mode & 0o777 == 0o600


def test_tampered_payload_and_broken_sequence_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    service = LedgerService(JsonlLedger(path))
    service.record(kind="changeset", entity_id="chg-1", payload={"state": "active"}, recorded_at=NOW)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["state"] = "ready"
    path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(LedgerCorruption, match="digest"):
        JsonlLedger(path).entries()
    with pytest.raises(LedgerCorruption):
        LedgerService(JsonlLedger(path)).record(kind="x", entity_id="x", payload={}, recorded_at=NOW)


def test_invalid_json_and_blank_records_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(LedgerCorruption):
        JsonlLedger(path).entries()

    path.write_text("\n", encoding="utf-8")
    with pytest.raises(LedgerCorruption, match="blank"):
        JsonlLedger(path).entries()


def test_missing_ledger_reads_empty_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    assert JsonlLedger(path).entries() == ()
    assert not path.exists()


def test_compare_and_append_refuses_a_stale_head_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = JsonlLedger(path)
    first = ledger.append_if_head(
        LedgerDraft("claim", "claim-1", '{"state":"active"}', NOW),
        expected_digest=LEDGER_GENESIS_DIGEST,
    )
    with pytest.raises(LedgerHeadChanged, match="head changed"):
        ledger.append_if_head(
            LedgerDraft("claim", "claim-2", '{"state":"active"}', NOW),
            expected_digest=LEDGER_GENESIS_DIGEST,
        )
    assert ledger.entries() == (first,)


def test_adapter_refuses_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "ledger.jsonl"
    os.symlink(target, link)

    with pytest.raises(JsonlLedgerError, match="symlink"):
        JsonlLedger(link).entries()


def test_adapter_refuses_broken_symlink_and_symlinked_directory(tmp_path: Path) -> None:
    broken = tmp_path / "broken.jsonl"
    os.symlink(tmp_path / "missing-target", broken)
    with pytest.raises(JsonlLedgerError, match="symlink"):
        JsonlLedger(broken).entries()

    real_directory = tmp_path / "real"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked"
    os.symlink(real_directory, linked_directory)
    with pytest.raises(JsonlLedgerError, match="symlinked ledger directory"):
        LedgerService(JsonlLedger(linked_directory / "ledger.jsonl")).record(
            kind="test", entity_id="1", payload={}, recorded_at=NOW
        )
