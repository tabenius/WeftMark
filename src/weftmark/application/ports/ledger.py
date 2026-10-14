"""Replaceable append-only ledger contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class LedgerContractError(ValueError):
    """Raised when a ledger value is malformed."""


class LedgerHeadChanged(RuntimeError):
    """Raised when an optimistic append observes a different ledger head."""


class LedgerIdempotencyConflict(RuntimeError):
    """An operation identifier was already used for a different request."""


@dataclass(frozen=True, slots=True)
class LedgerHead:
    workspace_id: str
    ledger_id: str
    sequence: int
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not isinstance(self.ledger_id, str) or not self.workspace_id or not self.ledger_id:
            raise LedgerContractError("workspace and ledger identity are required")
        if type(self.sequence) is not int or not 0 <= self.sequence <= 2**53 - 1:
            raise LedgerContractError("head sequence must be a safe nonnegative integer")
        if not isinstance(self.digest, str) or len(self.digest) != 64 or any(c not in "0123456789abcdef" for c in self.digest):
            raise LedgerContractError("head digest must be lowercase SHA-256")
        if self.sequence == 0 and self.digest != LEDGER_GENESIS_DIGEST:
            raise LedgerContractError("empty head must use the genesis digest")


LEDGER_GENESIS_DIGEST = "0" * 64


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

    def append_if_head(
        self, draft: LedgerDraft, *, expected_digest: str
    ) -> LedgerEntry:
        """Append only when the current head digest matches the expectation."""

    def entries(self) -> tuple[LedgerEntry, ...]:
        """Read and validate the complete ordered ledger."""


@runtime_checkable
class IdempotentLedgerPort(LedgerPort, Protocol):
    """Optional extension; existing JSONL and memory ports remain compatible."""

    def head(self) -> LedgerHead:
        """Return the head of a validated snapshot."""

    def append_idempotent(
        self, draft: LedgerDraft, *, operation_id: str,
        expected_digest: str | None = None, principal_id: str = "local",
    ) -> LedgerEntry:
        """Atomically append or replay the original result for the same request."""
