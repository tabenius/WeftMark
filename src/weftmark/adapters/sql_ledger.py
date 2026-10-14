"""Portable, CAS-guarded ledger plans for an atomic SQLite-family batch host.

Hosts own connections, auth and transactions. They MUST execute every batch in
order in one all-or-nothing transaction, including snapshot read batches. Plans
are internal trusted-host data, never a client-supplied SQL execution API.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files
import re
from typing import Callable, Mapping, Protocol
import uuid

from weftmark.application.ports.ledger import (
    LEDGER_GENESIS_DIGEST, LedgerDraft, LedgerEntry, LedgerHead,
    LedgerContractError, LedgerHeadChanged, LedgerIdempotencyConflict,
)
from weftmark.adapters.ledger_codec import (
    FORMAT, LedgerCodecError, canonical, decode_record, digest, draft_content, make_record, parse, verify_records,
)

COMPONENT = "weftmark.ledger-sql.v1"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class LedgerStorageError(RuntimeError):
    """Unavailable host, unknown schema, invalid batch or inconsistent storage."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("identifier must be 1-120 ASCII letters/digits/dots/dashes/underscores/colons")
    return value


@dataclass(frozen=True, slots=True)
class Statement:
    sql: str
    params: tuple = ()


@dataclass(frozen=True, slots=True)
class SqlPlan:
    statements: tuple[Statement, ...]

    def to_payload(self) -> dict:
        return {"schema": "weftmark.ledger-sql-plan.v1", "atomic": True,
                "statements": [{"sql": s.sql, "params": list(s.params)} for s in self.statements]}


Rows = tuple[Mapping, ...]


class AtomicBatchExecutor(Protocol):
    def batch(self, statements: tuple[Statement, ...], *, write: bool = False) -> tuple[Rows, ...]:
        """Execute in order, atomically; errors roll back the entire batch.

        Return one row-list per statement, including empty lists for DML.
        Writes and immediately subsequent reads must use the authoritative host.
        """


class CallbackBatchExecutor:
    """Adapt D1/libSQL bindings or another host without pretending to own SQL transactions."""

    def __init__(self, execute: Callable[[dict], list], *, atomic: bool):
        if atomic is not True:
            raise LedgerStorageError("a transactional batch host is required")
        self.execute = execute

    def batch(self, statements: tuple[Statement, ...], *, write: bool = False) -> tuple[Rows, ...]:
        # `write` is an executor routing hint, not a permission granted by a plan.
        payload = dict(SqlPlan(statements).to_payload(), write=write)
        try:
            results = self.execute(payload)
        except Exception as error:
            raise LedgerStorageError("atomic SQL host failed") from error
        if not isinstance(results, (list, tuple)) or len(results) != len(statements):
            raise LedgerStorageError("SQL host returned an incomplete batch")
        decoded = []
        for result in results:
            if not isinstance(result, Mapping) or result.get("success") is not True:
                raise LedgerStorageError("SQL host refused a batch statement")
            rows = result.get("results")
            if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
                raise LedgerStorageError("SQL host returned malformed rows")
            decoded.append(tuple(rows))
        return tuple(decoded)


def schema_sql() -> str:
    return files("weftmark.adapters").joinpath("sql/001_ledger.sql").read_text(encoding="utf-8")


def append_plan(head: LedgerHead, record: str, operation_key: str, request_json: str) -> SqlPlan:
    """Compile an already-validated candidate; callers cannot provide raw SQL."""
    entry = decode_record(record)
    content = parse(record)
    content.pop("digest")
    if digest(content) != entry.digest:
        raise ValueError("candidate digest does not match its content")
    request = parse(request_json)
    request_digest = digest(request)
    workspace, ledger = head.workspace_id, head.ledger_id
    if entry.sequence != head.sequence + 1 or entry.previous_digest != head.digest:
        raise ValueError("candidate does not extend the supplied head")
    return SqlPlan((
        Statement("""INSERT INTO weftmark_ledger_entries
          (workspace_id,ledger_id,sequence,previous_digest,digest,kind,entity_id,recorded_at,record_json,operation_key,request_digest)
          SELECT ?,?,?,?,?,?,?,?,?,?,? WHERE EXISTS (
            SELECT 1 FROM weftmark_ledger_streams WHERE workspace_id=? AND ledger_id=?
              AND head_sequence=? AND head_digest=? AND format=?
          ) AND NOT EXISTS (
            SELECT 1 FROM weftmark_ledger_entries WHERE workspace_id=? AND ledger_id=? AND operation_key=?
          )""", (workspace, ledger, entry.sequence, entry.previous_digest, entry.digest,
                   entry.draft.kind, entry.draft.entity_id, parse(record)["recorded_at"], record,
                   operation_key, request_digest, workspace, ledger, head.sequence, head.digest, FORMAT,
                   workspace, ledger, operation_key)),
        Statement("""INSERT INTO weftmark_ledger_receipts
          (workspace_id,ledger_id,operation_key,request_digest,sequence,principal_id,request_json)
          SELECT workspace_id,ledger_id,operation_key,request_digest,sequence,?,?
          FROM weftmark_ledger_entries WHERE workspace_id=? AND ledger_id=? AND operation_key=? AND request_digest=?
          ON CONFLICT(workspace_id,ledger_id,operation_key) DO NOTHING""",
          (request["principal_id"], request_json, workspace, ledger, operation_key, request_digest)),
        Statement("""UPDATE weftmark_ledger_streams SET head_sequence=?,head_digest=?
          WHERE workspace_id=? AND ledger_id=? AND head_sequence=? AND head_digest=? AND EXISTS (
            SELECT 1 FROM weftmark_ledger_entries e JOIN weftmark_ledger_receipts r
              ON r.workspace_id=e.workspace_id AND r.ledger_id=e.ledger_id AND r.operation_key=e.operation_key
            WHERE e.workspace_id=? AND e.ledger_id=? AND e.operation_key=? AND e.request_digest=?
              AND e.sequence=? AND e.digest=? AND r.request_digest=e.request_digest
          )""", (entry.sequence, entry.digest, workspace, ledger, head.sequence, head.digest,
                   workspace, ledger, operation_key, request_digest, entry.sequence, entry.digest)),
        Statement("""SELECT r.request_digest,e.record_json FROM weftmark_ledger_receipts r
          JOIN weftmark_ledger_entries e ON e.workspace_id=r.workspace_id AND e.ledger_id=r.ledger_id AND e.sequence=r.sequence
          WHERE r.workspace_id=? AND r.ledger_id=? AND r.operation_key=?""", (workspace, ledger, operation_key)),
    ))


class SqlLedger:
    """LedgerPort implementation whose only storage dependency is atomic batches."""

    def __init__(self, executor: AtomicBatchExecutor, *, workspace_id: str, ledger_id: str):
        self.executor = executor
        self.workspace_id = identifier(workspace_id)
        self.ledger_id = identifier(ledger_id)

    def _schema_statement(self) -> Statement:
        return Statement("SELECT singleton,version,component FROM weftmark_ledger_schema")

    @staticmethod
    def _check_schema(rows: Rows) -> None:
        if len(rows) != 1 or dict(rows[0]) != {"singleton": 1, "version": 1, "component": COMPONENT}:
            raise LedgerStorageError("unsupported WeftMark ledger schema")

    def create_stream(self) -> None:
        # Schema is checked before any mutation. DDL is explicitly provisioned by the host.
        self._check_schema(self.executor.batch((self._schema_statement(),))[0])
        self.executor.batch((Statement("""INSERT INTO weftmark_ledger_streams
          (workspace_id,ledger_id,format,head_sequence,head_digest) VALUES(?,?,?,0,?)
          ON CONFLICT(workspace_id,ledger_id) DO NOTHING""",
          (self.workspace_id, self.ledger_id, FORMAT, LEDGER_GENESIS_DIGEST)),), write=True)
        self.head()

    def snapshot(self) -> tuple[LedgerHead, tuple[LedgerEntry, ...], Rows]:
        try:
            return self._snapshot_checked()
        except (LedgerCodecError, LedgerContractError, KeyError, TypeError, IndexError) as error:
            raise LedgerStorageError("stored ledger content is invalid") from error

    def _snapshot_checked(self) -> tuple[LedgerHead, tuple[LedgerEntry, ...], Rows]:
        key = (self.workspace_id, self.ledger_id)
        result = self.executor.batch((self._schema_statement(),
            Statement("SELECT format,head_sequence,head_digest FROM weftmark_ledger_streams WHERE workspace_id=? AND ledger_id=?", key),
            Statement("""SELECT e.*,r.principal_id,r.request_json,r.request_digest AS receipt_digest,r.sequence AS receipt_sequence
              FROM weftmark_ledger_entries e LEFT JOIN weftmark_ledger_receipts r
                ON r.workspace_id=e.workspace_id AND r.ledger_id=e.ledger_id AND r.operation_key=e.operation_key
              WHERE e.workspace_id=? AND e.ledger_id=? ORDER BY e.sequence""", key),
            Statement("SELECT count(*) AS n FROM weftmark_ledger_receipts WHERE workspace_id=? AND ledger_id=?", key),
        ))
        self._check_schema(result[0])
        if len(result[1]) != 1 or result[1][0]["format"] != FORMAT:
            raise LedgerStorageError("unknown ledger stream or format")
        stream = result[1][0]
        head = LedgerHead(*key, stream["head_sequence"], stream["head_digest"])
        rows = result[2]
        entries = verify_records(tuple(row["record_json"] for row in rows))
        actual_digest = entries[-1].digest if entries else LEDGER_GENESIS_DIGEST
        if head.sequence != len(entries) or head.digest != actual_digest or result[3][0]["n"] != len(entries):
            raise LedgerStorageError("ledger head, entries or receipts are inconsistent")
        for row, entry in zip(rows, entries):
            data = parse(row["record_json"])
            for column in ("sequence", "previous_digest", "digest", "kind", "entity_id", "recorded_at"):
                if row[column] != data[column]:
                    raise LedgerStorageError("ledger index does not match canonical record")
            if row["request_json"] is None:
                raise LedgerStorageError("ledger entry has no atomic receipt")
            request = parse(row["request_json"])
            if canonical(request) != row["request_json"] or digest(request) != row["request_digest"]:
                raise LedgerStorageError("ledger receipt digest mismatch")
            if (row["receipt_digest"] != row["request_digest"] or row["receipt_sequence"] != entry.sequence
                or request.get("workspace_id") != self.workspace_id or request.get("ledger_id") != self.ledger_id
                or request.get("operation_key") != row["operation_key"] or request.get("principal_id") != row["principal_id"]):
                raise LedgerStorageError("ledger receipt identity mismatch")
            if request.get("schema") == "weftmark.ledger-append.v1":
                content = {name: data[name] for name in ("kind", "entity_id", "payload", "recorded_at")}
                if request.get("draft") != content or request.get("expected_digest") not in (None, entry.previous_digest):
                    raise LedgerStorageError("ledger receipt does not bind its entry")
            elif request.get("schema") == "weftmark.ledger-import.v1":
                if request.get("record_digest") != entry.digest:
                    raise LedgerStorageError("import receipt does not bind its entry")
            else:
                raise LedgerStorageError("unsupported ledger receipt version")
        return head, entries, rows

    def entries(self) -> tuple[LedgerEntry, ...]:
        return self.snapshot()[1]

    def head(self) -> LedgerHead:
        return self.snapshot()[0]

    def export_jsonl(self) -> str:
        """Nostoi-compatible validated export; does not overwrite a file."""
        return "".join(row["record_json"] + "\n" for row in self.snapshot()[2])

    def append(self, draft: LedgerDraft) -> LedgerEntry:
        return self.append_idempotent(draft, operation_id=uuid.uuid4().hex)

    def append_if_head(self, draft: LedgerDraft, *, expected_digest: str) -> LedgerEntry:
        return self.append_idempotent(draft, operation_id=uuid.uuid4().hex, expected_digest=expected_digest)

    def append_idempotent(self, draft: LedgerDraft, *, operation_id: str,
                          expected_digest: str | None = None, principal_id: str = "local") -> LedgerEntry:
        if not isinstance(operation_id, str) or not 1 <= len(operation_id.encode()) <= 256:
            raise ValueError("operation ID must be 1-256 UTF-8 bytes")
        identifier(principal_id)
        if expected_digest is not None and (not isinstance(expected_digest, str) or not DIGEST.fullmatch(expected_digest)):
            raise ValueError("expected digest must be lowercase SHA-256")
        operation_key = hashlib.sha256(operation_id.encode()).hexdigest()
        request_json = canonical({"schema": "weftmark.ledger-append.v1",
            "workspace_id": self.workspace_id, "ledger_id": self.ledger_id, "operation_key": operation_key,
            "principal_id": principal_id, "expected_digest": expected_digest, "draft": draft_content(draft)})
        request_digest = digest(parse(request_json))
        for _ in range(16):
            head, entries, rows = self.snapshot()
            for row, entry in zip(rows, entries):
                if row["operation_key"] == operation_key:
                    if row["request_digest"] != request_digest:
                        raise LedgerIdempotencyConflict("operation ID reused for a different request or principal")
                    return entry
            if expected_digest is not None and head.digest != expected_digest:
                raise LedgerHeadChanged("ledger head changed before append")
            if head.sequence >= 2**53 - 1:
                raise LedgerStorageError("ledger sequence limit reached")
            record = make_record(draft, head.sequence + 1, head.digest)
            results = self.executor.batch(append_plan(head, record, operation_key, request_json).statements, write=True)
            receipt = results[-1]
            if receipt:
                if receipt[0]["request_digest"] != request_digest:
                    raise LedgerIdempotencyConflict("operation ID reused for a different request or principal")
                return decode_record(receipt[0]["record_json"])
            if expected_digest is not None:
                raise LedgerHeadChanged("ledger head changed during append")
        raise LedgerHeadChanged("ledger remained busy after bounded append retries")

    def verify_checkpoint(self, trusted: LedgerHead) -> LedgerHead:
        """Compare an externally trusted head; this does not authenticate the checkpoint."""
        head, entries, _ = self.snapshot()
        if (trusted.workspace_id, trusted.ledger_id) != (self.workspace_id, self.ledger_id):
            raise LedgerStorageError("checkpoint belongs to another ledger")
        if head.sequence < trusted.sequence:
            raise LedgerStorageError("ledger is older than the trusted checkpoint")
        actual = entries[trusted.sequence - 1].digest if trusted.sequence else LEDGER_GENESIS_DIGEST
        if actual != trusted.digest:
            raise LedgerStorageError("ledger disagrees with the trusted checkpoint")
        return head
