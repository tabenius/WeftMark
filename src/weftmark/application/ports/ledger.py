"""Replaceable append-only ledger contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class LedgerContractError(ValueError):
    """Raised when a ledger value is malformed."""


@dataclass(frozen=True, slots=True)
class LedgerDraft:
    kind: str
    entity_id: str
    payload_json: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        for name in ("kind", "entity_id", "payload_json"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise LedgerContractError(f"{name} must not be empty")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise LedgerContractError("recorded_at must include a timezone")
        try:
            payload = json.loads(self.payload_json)
        except json.JSONDecodeError as error:
            raise LedgerContractError("payload_json must be valid JSON") from error
        if not isinstance(payload, dict):
            raise LedgerContractError("ledger payload must be a JSON object")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if canonical != self.payload_json:
            raise LedgerContractError("payload_json must use canonical JSON encoding")

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    sequence: int
    previous_digest: str
    digest: str
    draft: LedgerDraft

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise LedgerContractError("ledger sequence must be positive")
        for name in ("previous_digest", "digest"):
            value = getattr(self, name)
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise LedgerContractError(f"{name} must be a SHA-256 hex digest")

    @property
    def kind(self) -> str:
        return self.draft.kind

    @property
    def entity_id(self) -> str:
        return self.draft.entity_id

    @property
    def payload(self) -> dict[str, Any]:
        return self.draft.payload


@runtime_checkable
class LedgerPort(Protocol):
    def append(self, draft: LedgerDraft) -> LedgerEntry:
        """Append one entry and return its assigned sequence and digest."""

    def entries(self) -> tuple[LedgerEntry, ...]:
        """Read and validate the complete ordered ledger."""
