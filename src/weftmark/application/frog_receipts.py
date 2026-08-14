"""Durable read-only receipts for externally captured Frog plan snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import (
    LEDGER_GENESIS_DIGEST,
    LedgerEntry,
    LedgerHeadChanged,
)


class FrogReceiptError(ValueError):
    """Raised when a Frog snapshot or stored receipt is invalid."""


_RECORD_COLLECTIONS = {
    "repos",
    "tasks",
    "task_dependencies",
    "task_conflicts",
    "task_tags",
    "task_assignments",
    "agents",
    "files",
    "task_files",
    "locks",
}


@dataclass(frozen=True, slots=True)
class FrogSnapshotReceipt:
    digest: str
    source_label: str
    captured_at: datetime
    imported_at: datetime
    counts: Mapping[str, int]
    snapshot: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FrogReceiptResult:
    receipt: FrogSnapshotReceipt
    imported: bool
    sequence: int


class FrogReceiptService:
    def __init__(self, ledger: LedgerService) -> None:
        self._ledger = ledger

    def record(
        self,
        snapshot: Mapping[str, Any],
        *,
        imported_at: datetime,
    ) -> FrogReceiptResult:
        _require_aware(imported_at, name="imported_at")
        receipt = _receipt_from_snapshot(snapshot, imported_at=imported_at)
        for _ in range(8):
            entries = self._ledger.snapshot()
            existing = _find(entries, receipt.digest)
            if existing is not None:
                return FrogReceiptResult(
                    _receipt_from_payload(existing.payload),
                    False,
                    existing.sequence,
                )
            expected = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
            try:
                entry = self._ledger.record_if_head(
                    kind="frog_snapshot",
                    entity_id=receipt.digest,
                    payload=_receipt_to_payload(receipt),
                    recorded_at=imported_at,
                    expected_digest=expected,
                )
                return FrogReceiptResult(receipt, True, entry.sequence)
            except LedgerHeadChanged:
                continue
        raise FrogReceiptError("ledger remained busy while recording Frog snapshot")

    def get(self, digest: str) -> FrogSnapshotReceipt | None:
        entry = _find(self._ledger.snapshot(), digest)
        return None if entry is None else _receipt_from_payload(entry.payload)

    def list(self) -> tuple[FrogSnapshotReceipt, ...]:
        return tuple(
            _receipt_from_payload(entry.payload)
            for entry in self._ledger.snapshot()
            if entry.kind == "frog_snapshot"
        )

    def tasks(
        self,
        digest: str,
        *,
        repo_path: str | None = None,
        workflow_status: str | None = None,
    ) -> tuple[Mapping[str, Any], ...] | None:
        receipt = self.get(digest)
        if receipt is None:
            return None
        tasks = receipt.snapshot["records"]["tasks"]
        return tuple(
            task
            for task in tasks
            if (repo_path is None or task["repo_path"] == repo_path)
            and (
                workflow_status is None
                or task["workflow_status"] == workflow_status
            )
        )


def receipt_result_to_payload(result: FrogReceiptResult) -> dict[str, Any]:
    return {
        **receipt_summary_to_payload(result.receipt),
        "imported": result.imported,
        "sequence": result.sequence,
    }


def receipt_summary_to_payload(receipt: FrogSnapshotReceipt) -> dict[str, Any]:
    return {
        "digest": receipt.digest,
        "source_label": receipt.source_label,
        "captured_at": receipt.captured_at.isoformat(),
        "imported_at": receipt.imported_at.isoformat(),
        "counts": dict(receipt.counts),
    }


def receipt_to_payload(receipt: FrogSnapshotReceipt) -> dict[str, Any]:
    return {
        **receipt_summary_to_payload(receipt),
        "snapshot": receipt.snapshot,
    }


def _find(entries: tuple[LedgerEntry, ...], digest: str) -> LedgerEntry | None:
    return next(
        (
            entry
            for entry in entries
            if entry.kind == "frog_snapshot" and entry.entity_id == digest
        ),
        None,
    )


def _receipt_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    imported_at: datetime,
) -> FrogSnapshotReceipt:
    try:
        if set(snapshot) != {
            "schema_version",
            "source_kind",
            "source_label",
            "source_schema",
            "captured_at",
            "digest",
            "records",
        }:
            raise ValueError("unexpected snapshot fields")
        if snapshot["schema_version"] != 1:
            raise ValueError("unsupported snapshot schema")
        if snapshot["source_kind"] != "frog-agents-db":
            raise ValueError("unsupported snapshot source")
        label = str(snapshot["source_label"])
        if not label.strip():
            raise ValueError("empty source label")
        captured_at = datetime.fromisoformat(str(snapshot["captured_at"]))
        _require_aware(captured_at, name="captured_at")
        source_schema = snapshot["source_schema"]
        if not isinstance(source_schema, Mapping) or set(source_schema) != {
            "migrations"
        }:
            raise TypeError("source schema is malformed")
        migrations = source_schema["migrations"]
        if not isinstance(migrations, list) or not all(
            isinstance(value, str) and value for value in migrations
        ):
            raise TypeError("source migrations must be a string list")
        records = snapshot["records"]
        if not isinstance(records, Mapping) or set(records) != _RECORD_COLLECTIONS:
            raise TypeError("records are not an object")
        counts: dict[str, int] = {}
        for name, values in records.items():
            if not isinstance(name, str) or not isinstance(values, list):
                raise TypeError("record collections must be named lists")
            if not all(isinstance(value, Mapping) for value in values):
                raise TypeError("records must be objects")
            counts[name] = len(values)
        digest_contents = {
            "source_kind": snapshot["source_kind"],
            "source_label": label,
            "source_schema": snapshot["source_schema"],
            "records": records,
        }
        canonical = json.dumps(
            digest_contents,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        expected = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
        if snapshot["digest"] != expected:
            raise ValueError("snapshot digest mismatch")
        copied = json.loads(json.dumps(snapshot, sort_keys=True, allow_nan=False))
        return FrogSnapshotReceipt(
            expected,
            label,
            captured_at,
            imported_at,
            dict(sorted(counts.items())),
            copied,
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, FrogReceiptError):
            raise
        raise FrogReceiptError("Frog snapshot is malformed or corrupt") from error


def _receipt_to_payload(receipt: FrogSnapshotReceipt) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "digest": receipt.digest,
        "source_label": receipt.source_label,
        "captured_at": receipt.captured_at.isoformat(),
        "imported_at": receipt.imported_at.isoformat(),
        "snapshot": receipt.snapshot,
    }


def _receipt_from_payload(payload: Mapping[str, Any]) -> FrogSnapshotReceipt:
    try:
        if payload["schema_version"] != 1:
            raise ValueError("unsupported receipt schema")
        imported_at = datetime.fromisoformat(str(payload["imported_at"]))
        _require_aware(imported_at, name="stored imported_at")
        snapshot = payload["snapshot"]
        if not isinstance(snapshot, Mapping):
            raise TypeError("stored snapshot is not an object")
        receipt = _receipt_from_snapshot(snapshot, imported_at=imported_at)
        if (
            payload["digest"] != receipt.digest
            or payload["source_label"] != receipt.source_label
            or payload["captured_at"] != receipt.captured_at.isoformat()
        ):
            raise ValueError("receipt metadata mismatch")
        return receipt
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, FrogReceiptError):
            raise
        raise FrogReceiptError("stored Frog snapshot receipt is malformed") from error


def _require_aware(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FrogReceiptError(f"{name} must include a timezone")
