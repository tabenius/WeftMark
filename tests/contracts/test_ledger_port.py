from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from weftmark.application.ports.ledger import (
    LEDGER_GENESIS_DIGEST,
    LedgerContractError,
    LedgerDraft,
    LedgerEntry,
    LedgerHeadChanged,
    LedgerPort,
)


NOW = datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc)


class MemoryLedger:
    def __init__(self) -> None:
        self.values: list[LedgerEntry] = []

    def append(self, draft: LedgerDraft) -> LedgerEntry:
        previous = self.values[-1].digest if self.values else LEDGER_GENESIS_DIGEST
        entry = LedgerEntry(len(self.values) + 1, previous, "a" * 64, draft)
        self.values.append(entry)
        return entry

    def append_if_head(
        self, draft: LedgerDraft, *, expected_digest: str
    ) -> LedgerEntry:
        actual = self.values[-1].digest if self.values else LEDGER_GENESIS_DIGEST
        if actual != expected_digest:
            raise LedgerHeadChanged
        return self.append(draft)

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self.values)


def test_port_is_structural_and_payload_is_canonical_json() -> None:
    ledger = MemoryLedger()
    draft = LedgerDraft("changeset", "chg-1", '{"a":1,"b":2}', NOW)

    assert isinstance(ledger, LedgerPort)
    assert ledger.append(draft).payload == {"a": 1, "b": 2}
    assert ledger.entries()[0].sequence == 1


def test_noncanonical_nonobject_and_naive_values_fail_closed() -> None:
    with pytest.raises(LedgerContractError, match="canonical"):
        LedgerDraft("kind", "id", json.dumps({"b": 2, "a": 1}), NOW)
    with pytest.raises(LedgerContractError, match="JSON object"):
        LedgerDraft("kind", "id", "[]", NOW)
    with pytest.raises(LedgerContractError, match="timezone"):
        LedgerDraft("kind", "id", "{}", NOW.replace(tzinfo=None))
