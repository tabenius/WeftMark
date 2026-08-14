from __future__ import annotations

from datetime import datetime, timezone

import pytest

from weftmark.application.ledger import LedgerService, LedgerServiceError
from weftmark.application.ports.ledger import LedgerDraft, LedgerEntry


NOW = datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc)


class MemoryLedger:
    def __init__(self) -> None:
        self.values: list[LedgerEntry] = []

    def append(self, draft: LedgerDraft) -> LedgerEntry:
        previous = self.values[-1].digest if self.values else "0" * 64
        entry = LedgerEntry(len(self.values) + 1, previous, "a" * 64, draft)
        self.values.append(entry)
        return entry

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self.values)


def test_service_records_queries_and_selects_latest_entity_snapshot() -> None:
    service = LedgerService(MemoryLedger())
    service.record(kind="changeset", entity_id="chg-1", payload={"state": "active"}, recorded_at=NOW)
    latest = service.record(kind="changeset", entity_id="chg-1", payload={"state": "review"}, recorded_at=NOW)
    service.record(kind="evidence", entity_id="ev-1", payload={"state": "passed"}, recorded_at=NOW)

    assert service.latest(kind="changeset", entity_id="chg-1") == latest
    assert len(service.history(kind="changeset")) == 2
    assert len(service.history(entity_id="ev-1")) == 1


@pytest.mark.parametrize(
    "key", ("password", "token", "secret", "private_key", "github-token")
)
def test_service_refuses_secret_bearing_keys_at_any_depth(key: str) -> None:
    with pytest.raises(LedgerServiceError, match=key):
        LedgerService(MemoryLedger()).record(
            kind="unsafe",
            entity_id="1",
            payload={"nested": [{key: "value"}]},
            recorded_at=NOW,
        )


def test_service_refuses_non_json_payloads() -> None:
    with pytest.raises(LedgerServiceError, match="JSON-safe"):
        LedgerService(MemoryLedger()).record(
            kind="bad", entity_id="1", payload={"value": object()}, recorded_at=NOW
        )


def test_service_refuses_secret_material_hidden_in_text_values() -> None:
    with pytest.raises(LedgerServiceError, match=r"\$.notes"):
        LedgerService(MemoryLedger()).record(
            kind="unsafe",
            entity_id="1",
            payload={"notes": "connect with password=hunter2"},
            recorded_at=NOW,
        )
