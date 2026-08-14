"""Application service for durable, JSON-safe workspace records."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Mapping

from weftmark.application.ports.ledger import (
    LedgerDraft,
    LedgerEntry,
    LedgerPort,
)


class LedgerServiceError(ValueError):
    """Raised when a workspace record is unsafe or ambiguous."""


_SENSITIVE_KEYS = frozenset(
    {"api_key", "credential", "credentials", "password", "private_key", "secret", "token"}
)
_SECRET_TEXT = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"(?:github_pat_|gh[oprsu]_|(?<![a-z0-9])sk-[a-z0-9])|"
    r"(?:password|secret|token|api[_-]?key|credential)"
    r"\s*[:=]\s*(?!<redacted>|redacted|\*\*\*)\S+",
    re.IGNORECASE,
)


class LedgerService:
    def __init__(self, ledger: LedgerPort) -> None:
        self._ledger = ledger

    def record(
        self,
        *,
        kind: str,
        entity_id: str,
        payload: Mapping[str, Any],
        recorded_at: datetime,
    ) -> LedgerEntry:
        draft = _safe_draft(kind, entity_id, payload, recorded_at)
        return self._ledger.append(draft)

    def record_if_head(
        self,
        *,
        kind: str,
        entity_id: str,
        payload: Mapping[str, Any],
        recorded_at: datetime,
        expected_digest: str,
    ) -> LedgerEntry:
        draft = _safe_draft(kind, entity_id, payload, recorded_at)
        return self._ledger.append_if_head(draft, expected_digest=expected_digest)

    def snapshot(self) -> tuple[LedgerEntry, ...]:
        """Return one validated ledger view for optimistic application logic."""

        return self._ledger.entries()

    def history(
        self,
        *,
        kind: str | None = None,
        entity_id: str | None = None,
    ) -> tuple[LedgerEntry, ...]:
        return tuple(
            entry
            for entry in self._ledger.entries()
            if (kind is None or entry.kind == kind)
            and (entity_id is None or entry.entity_id == entity_id)
        )

    def latest(self, *, kind: str, entity_id: str) -> LedgerEntry | None:
        matches = self.history(kind=kind, entity_id=entity_id)
        return matches[-1] if matches else None


def _safe_draft(
    kind: str,
    entity_id: str,
    payload: Mapping[str, Any],
    recorded_at: datetime,
) -> LedgerDraft:
    sensitive_paths = _sensitive_paths(payload)
    if sensitive_paths:
        raise LedgerServiceError(
            "ledger payload contains secret-bearing fields: "
            + ", ".join(sensitive_paths)
        )
    try:
        payload_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise LedgerServiceError("ledger payload must be JSON-safe") from error
    return LedgerDraft(kind, entity_id, payload_json, recorded_at)


def _sensitive_paths(value: object, prefix: str = "$") -> tuple[str, ...]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}"
            normalized_key = key_text.casefold().replace("-", "_")
            if normalized_key in _SENSITIVE_KEYS or any(
                normalized_key.endswith(f"_{sensitive}")
                for sensitive in _SENSITIVE_KEYS
            ):
                findings.append(path)
            findings.extend(_sensitive_paths(child, path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            findings.extend(_sensitive_paths(child, f"{prefix}[{index}]"))
    elif isinstance(value, str) and _SECRET_TEXT.search(value):
        findings.append(prefix)
    return tuple(findings)
