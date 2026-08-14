"""Idempotent read-only receipt of verified external bundles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from weftmark.application.bundle import BundleVerification, verify_bundle
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import (
    LEDGER_GENESIS_DIGEST,
    LedgerEntry,
    LedgerHeadChanged,
)


class BundleImportError(ValueError):
    """Raised when an imported bundle record is malformed or cannot settle."""


@dataclass(frozen=True, slots=True)
class ImportedBundle:
    digest: str
    change_set_id: str
    imported_at: datetime
    bundle: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class BundleImportResult:
    record: ImportedBundle
    imported: bool
    sequence: int


class BundleImportService:
    def __init__(self, ledger: LedgerService) -> None:
        self._ledger = ledger

    def import_bundle(
        self,
        bundle: Mapping[str, Any],
        *,
        imported_at: datetime,
    ) -> BundleImportResult:
        _require_aware(imported_at, name="imported_at")
        verified = verify_bundle(bundle)
        record = ImportedBundle(
            digest=verified.digest,
            change_set_id=verified.change_set_id,
            imported_at=imported_at,
            bundle=bundle,
        )
        for _ in range(8):
            snapshot = self._ledger.snapshot()
            existing = _find(snapshot, verified.digest)
            if existing is not None:
                return BundleImportResult(
                    record=_record_from_payload(existing.payload),
                    imported=False,
                    sequence=existing.sequence,
                )
            expected = snapshot[-1].digest if snapshot else LEDGER_GENESIS_DIGEST
            try:
                entry = self._ledger.record_if_head(
                    kind="imported_bundle",
                    entity_id=verified.digest,
                    payload=_record_to_payload(record),
                    recorded_at=imported_at,
                    expected_digest=expected,
                )
                return BundleImportResult(record, True, entry.sequence)
            except LedgerHeadChanged:
                continue
        raise BundleImportError("ledger remained busy while importing bundle; retry")

    def get(self, digest: str) -> ImportedBundle | None:
        entry = _find(self._ledger.snapshot(), digest)
        return None if entry is None else _record_from_payload(entry.payload)

    def list(self) -> tuple[ImportedBundle, ...]:
        return tuple(
            _record_from_payload(entry.payload)
            for entry in self._ledger.snapshot()
            if entry.kind == "imported_bundle"
        )


def import_result_to_payload(result: BundleImportResult) -> dict[str, Any]:
    return {
        "digest": result.record.digest,
        "change_set_id": result.record.change_set_id,
        "imported_at": result.record.imported_at.isoformat(),
        "imported": result.imported,
        "sequence": result.sequence,
    }


def imported_bundle_to_payload(record: ImportedBundle) -> dict[str, Any]:
    return {
        "digest": record.digest,
        "change_set_id": record.change_set_id,
        "imported_at": record.imported_at.isoformat(),
        "bundle": record.bundle,
    }


def _find(entries: tuple[LedgerEntry, ...], digest: str) -> LedgerEntry | None:
    return next(
        (
            entry
            for entry in entries
            if entry.kind == "imported_bundle" and entry.entity_id == digest
        ),
        None,
    )


def _record_to_payload(record: ImportedBundle) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "digest": record.digest,
        "change_set_id": record.change_set_id,
        "imported_at": record.imported_at.isoformat(),
        "bundle": record.bundle,
    }


def _record_from_payload(payload: Mapping[str, Any]) -> ImportedBundle:
    try:
        if payload["schema_version"] != 1:
            raise ValueError("unsupported schema")
        bundle = payload["bundle"]
        if not isinstance(bundle, Mapping):
            raise TypeError("bundle is not an object")
        verification: BundleVerification = verify_bundle(bundle)
        if (
            payload["digest"] != verification.digest
            or payload["change_set_id"] != verification.change_set_id
        ):
            raise ValueError("import metadata does not match bundle")
        imported_at = datetime.fromisoformat(str(payload["imported_at"]))
        _require_aware(imported_at, name="stored imported_at")
        return ImportedBundle(
            digest=verification.digest,
            change_set_id=verification.change_set_id,
            imported_at=imported_at,
            bundle=bundle,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BundleImportError("stored imported bundle is malformed") from error


def _require_aware(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BundleImportError(f"{name} must include a timezone")
