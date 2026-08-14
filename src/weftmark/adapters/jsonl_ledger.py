"""Process-locked append-only JSONL ledger with a SHA-256 hash chain."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from weftmark.application.ports.ledger import LedgerDraft, LedgerEntry, LedgerPort


class JsonlLedgerError(RuntimeError):
    """Base class for local ledger failures."""


class LedgerCorruption(JsonlLedgerError):
    """Raised when sequence, encoding, or digest-chain validation fails."""


_GENESIS_DIGEST = "0" * 64


class JsonlLedger(LedgerPort):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, draft: LedgerDraft) -> LedgerEntry:
        with self._open(create=True) as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            entries = self._read_locked(stream)
            sequence = len(entries) + 1
            previous = entries[-1].digest if entries else _GENESIS_DIGEST
            digest = _entry_digest(sequence, previous, draft)
            entry = LedgerEntry(sequence, previous, digest, draft)
            stream.seek(0, os.SEEK_END)
            stream.write(_encode(entry) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            return entry

    def entries(self) -> tuple[LedgerEntry, ...]:
        if self.path.is_symlink():
            raise JsonlLedgerError("refusing to follow ledger symlink")
        if not self.path.exists():
            return ()
        with self._open(create=False) as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            return self._read_locked(stream)

    def _open(self, *, create: bool) -> IO[str]:
        if self.path.is_symlink():
            raise JsonlLedgerError("refusing to follow ledger symlink")
        if self.path.parent.is_symlink():
            raise JsonlLedgerError("refusing to use a symlinked ledger directory")
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT | os.O_APPEND
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise JsonlLedgerError(f"cannot open local ledger: {type(error).__name__}") from error
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "r+", encoding="utf-8", newline="")

    def _read_locked(self, stream: IO[str]) -> tuple[LedgerEntry, ...]:
        stream.seek(0)
        lines = stream.read().splitlines()
        entries: list[LedgerEntry] = []
        previous = _GENESIS_DIGEST
        for index, line in enumerate(lines, start=1):
            if not line:
                raise LedgerCorruption(f"blank ledger record at sequence {index}")
            entry = _decode(line)
            if entry.sequence != index:
                raise LedgerCorruption(f"non-monotonic ledger sequence at {index}")
            if entry.previous_digest != previous:
                raise LedgerCorruption(f"broken ledger chain at sequence {index}")
            expected = _entry_digest(index, previous, entry.draft)
            if entry.digest != expected:
                raise LedgerCorruption(f"invalid ledger digest at sequence {index}")
            entries.append(entry)
            previous = entry.digest
        return tuple(entries)


def _content(sequence: int, previous: str, draft: LedgerDraft) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "previous_digest": previous,
        "kind": draft.kind,
        "entity_id": draft.entity_id,
        "payload": draft.payload,
        "recorded_at": draft.recorded_at.isoformat(),
    }


def _entry_digest(sequence: int, previous: str, draft: LedgerDraft) -> str:
    encoded = json.dumps(
        _content(sequence, previous, draft), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _encode(entry: LedgerEntry) -> str:
    record = _content(entry.sequence, entry.previous_digest, entry.draft)
    record["digest"] = entry.digest
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _decode(line: str) -> LedgerEntry:
    try:
        record = json.loads(line)
        allowed = {
            "sequence",
            "previous_digest",
            "digest",
            "kind",
            "entity_id",
            "payload",
            "recorded_at",
        }
        if not isinstance(record, dict) or set(record) != allowed:
            raise ValueError("unexpected ledger fields")
        payload_json = json.dumps(
            record["payload"], sort_keys=True, separators=(",", ":")
        )
        draft = LedgerDraft(
            record["kind"],
            record["entity_id"],
            payload_json,
            datetime.fromisoformat(record["recorded_at"]),
        )
        return LedgerEntry(
            record["sequence"], record["previous_digest"], record["digest"], draft
        )
    except (KeyError, TypeError, ValueError) as error:
        raise LedgerCorruption("ledger contains an invalid JSON record") from error
