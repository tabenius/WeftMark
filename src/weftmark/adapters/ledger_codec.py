"""WeftMark v1 records using Nostoi's canonicalizer and native verifier.

The dependency is lazy so the existing JSONL runtime needs no native extension.
This module never signs, repairs history, or invents another chain format.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from weftmark.application.ports.ledger import LedgerDraft, LedgerEntry

FORMAT = "weftmark-ledger-v1"
MAX_RECORD_BYTES = 262144


class LedgerCodecError(ValueError):
    """Invalid, unsupported, or corrupt portable ledger content."""


def _nostoi():
    try:
        import nostoi
    except ImportError as error:
        raise LedgerCodecError("install weftmark[sqlite] for the pinned Nostoi bindings") from error
    return nostoi


def canonical(value: Any) -> str:
    _portable(value)
    try:
        return _nostoi().canonical(value)
    except (ValueError, OverflowError, TypeError) as error:
        raise LedgerCodecError("Nostoi canonical JSON refused the value") from error


def _portable(value: Any) -> None:
    """Narrow numeric profile before serde can coerce out-of-range integers.

    This is WeftMark's portability restriction, not a claimed Nostoi refusal.
    JSON strings carry exact payload integers through JavaScript API hosts.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if type(value) is int and -(2**63) <= value <= 2**64 - 1:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LedgerCodecError("JSON object keys must be strings")
            _portable(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _portable(item)
        return
    raise LedgerCodecError("portable JSON requires integers in signed/unsigned 64-bit range; floats are unsupported")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LedgerCodecError("duplicate JSON key")
        result[key] = value
    return result


def parse(text: str) -> dict:
    try:
        result = json.loads(text, object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(LedgerCodecError("nonfinite JSON")))
    except (ValueError, TypeError) as error:
        raise LedgerCodecError("invalid ledger JSON") from error
    if not isinstance(result, dict):
        raise LedgerCodecError("ledger JSON must be an object")
    return result


def draft_content(draft: LedgerDraft) -> dict:
    payload = parse(draft.payload_json)
    if canonical(payload) != draft.payload_json:
        raise LedgerCodecError("payload must use Nostoi-compatible canonical JSON")
    return {"kind": draft.kind, "entity_id": draft.entity_id,
            "payload": payload, "recorded_at": draft.recorded_at.isoformat()}


def make_record(draft: LedgerDraft, sequence: int, previous: str) -> str:
    content = dict(draft_content(draft), sequence=sequence, previous_digest=previous)
    record = canonical(dict(content, digest=digest(content)))
    if len(record.encode()) > MAX_RECORD_BYTES:
        raise LedgerCodecError("record exceeds the portable 256 KiB limit")
    return record


def decode_record(record: str) -> LedgerEntry:
    if not isinstance(record, str) or len(record.encode()) > MAX_RECORD_BYTES:
        raise LedgerCodecError("record exceeds the portable 256 KiB limit")
    data = parse(record)
    fields = {"sequence", "previous_digest", "digest", "kind", "entity_id", "payload", "recorded_at"}
    if set(data) != fields or type(data["sequence"]) is not int or not 1 <= data["sequence"] <= 2**53 - 1:
        raise LedgerCodecError("invalid WeftMark v1 record fields or sequence")
    for name in ("digest", "previous_digest"):
        value = data[name]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise LedgerCodecError("record digests must be lowercase SHA-256")
    try:
        draft = LedgerDraft(data["kind"], data["entity_id"], canonical(data["payload"]),
                            datetime.fromisoformat(data["recorded_at"]))
        entry = LedgerEntry(data["sequence"], data["previous_digest"], data["digest"], draft)
    except (ValueError, TypeError, AttributeError) as error:
        raise LedgerCodecError("invalid WeftMark v1 record") from error
    if canonical(data) != record:
        raise LedgerCodecError("stored record must be canonical")
    return entry


def verify_records(records: tuple[str, ...]) -> tuple[LedgerEntry, ...]:
    entries = tuple(decode_record(record) for record in records)
    try:
        report = _nostoi().verify_jsonl("".join(record + "\n" for record in records), FORMAT)
    except (ValueError, TypeError) as error:
        raise LedgerCodecError("Nostoi could not verify the ledger") from error
    if not report.get("ok"):
        raise LedgerCodecError("Nostoi rejected the ledger chain")
    return entries
