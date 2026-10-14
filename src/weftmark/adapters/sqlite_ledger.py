"""Local libsqlite3 adapter: private files, WAL/FULL durability and atomic batches.

Litestream operates on this WAL-backed file as a separate replication process;
it is not a SQL dialect, distributed lock or tamper-proof trust anchor.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from typing import Callable

from weftmark.application.ports.ledger import LEDGER_GENESIS_DIGEST, LedgerHead
from weftmark.adapters.ledger_codec import canonical, decode_record, parse, verify_records
from weftmark.adapters.sql_ledger import (
    LedgerStorageError, SqlLedger, Statement, append_plan, identifier, schema_sql,
)


def _path(path: str | Path) -> Path:
    result = Path(path).expanduser().absolute()
    if result.is_symlink() or result.parent.is_symlink():
        raise LedgerStorageError("refusing a symlinked ledger database or directory")
    return result


def _create_private(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)


class SqliteExecutor:
    def __init__(self, path: str | Path, *, timeout: float = 5):
        self.path = _path(path)
        self.timeout = timeout
        if not self.path.is_file():
            raise LedgerStorageError("ledger database is missing; create it explicitly")
        metadata = self.path.stat()
        if metadata.st_uid != os.geteuid() or not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise LedgerStorageError("ledger database must be an owner-only regular file")

    @contextmanager
    def connect(self):
        _path(self.path)
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                               timeout=self.timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    def transaction(self, callback: Callable, *, write: bool = False):
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    result = callback(conn)
                    conn.commit()
                    return result
                except Exception:
                    conn.rollback()
                    raise
        except sqlite3.Error as error:
            raise LedgerStorageError("SQLite ledger transaction failed") from error

    @staticmethod
    def execute(conn: sqlite3.Connection, statement: Statement) -> tuple[dict, ...]:
        cursor = conn.execute(statement.sql, statement.params)
        return tuple(dict(row) for row in cursor.fetchall()) if cursor.description else ()

    def batch(self, statements: tuple[Statement, ...], *, write: bool = False):
        return self.transaction(lambda conn: tuple(self.execute(conn, s) for s in statements), write=write)


class SqliteLedger(SqlLedger):
    def __init__(self, path: str | Path, *, workspace_id: str, ledger_id: str, timeout: float = 5):
        super().__init__(SqliteExecutor(path, timeout=timeout), workspace_id=workspace_id, ledger_id=ledger_id)
        self._check_schema(self.executor.batch((self._schema_statement(),))[0])

    @classmethod
    def create(cls, path: str | Path, *, workspace_id: str, ledger_id: str, timeout: float = 5):
        # Never create tables in an arbitrary existing database.
        identifier(workspace_id)
        identifier(ledger_id)
        path = _path(path)
        _create_private(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript("BEGIN IMMEDIATE;\n" + schema_sql() + "\nCOMMIT;")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        ledger = cls(path, workspace_id=workspace_id, ledger_id=ledger_id, timeout=timeout)
        ledger.create_stream()
        return ledger

    def import_jsonl(self, text: str) -> LedgerHead:
        """Explicit, atomic import into an empty stream, preserving all v1 digests.

        Validate the whole source before writing. Do not modify, replace or repair
        the source. Whitespace is normalized; the record content and hashes are not.
        """
        lines = text.splitlines()
        if any(not line for line in lines):
            raise LedgerStorageError("blank JSONL records are not importable")
        records = tuple(canonical(parse(line)) for line in lines)
        entries = verify_records(records)

        def import_locked(conn):
            version = tuple(dict(row) for row in conn.execute("SELECT * FROM weftmark_ledger_schema"))
            self._check_schema(version)
            key = (self.workspace_id, self.ledger_id)
            stream = conn.execute("SELECT * FROM weftmark_ledger_streams WHERE workspace_id=? AND ledger_id=?", key).fetchone()
            count = conn.execute("SELECT count(*) FROM weftmark_ledger_entries WHERE workspace_id=? AND ledger_id=?", key).fetchone()[0]
            receipts = conn.execute("SELECT count(*) FROM weftmark_ledger_receipts WHERE workspace_id=? AND ledger_id=?", key).fetchone()[0]
            if stream is None or stream["format"] != "weftmark-ledger-v1" or stream["head_sequence"] != 0 or stream["head_digest"] != LEDGER_GENESIS_DIGEST or count or receipts:
                raise LedgerStorageError("import requires an existing empty stream")
            head = LedgerHead(*key, 0, LEDGER_GENESIS_DIGEST)
            for record, entry in zip(records, entries):
                operation_key = hashlib.sha256(f"import:{entry.sequence}:{entry.digest}".encode()).hexdigest()
                request = canonical({"schema": "weftmark.ledger-import.v1", "workspace_id": key[0],
                    "ledger_id": key[1], "operation_key": operation_key, "principal_id": "import",
                    "record_digest": entry.digest})
                results = tuple(self.executor.execute(conn, s) for s in append_plan(head, record, operation_key, request).statements)
                if not results[-1]:
                    raise LedgerStorageError("import append was not accepted")
                head = LedgerHead(*key, entry.sequence, entry.digest)
            return head

        return self.executor.transaction(import_locked, write=True)

    def backup(self, destination: str | Path) -> Path:
        """Verified SQLite online backup, with no overwrite or source writes."""
        destination = _path(destination)
        self.head()  # Refuse known-corrupt storage before copying it.
        _create_private(destination)
        with closing(sqlite3.connect(self.executor.path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                # A standalone backup must not depend on a staging-name WAL.
                target.execute("PRAGMA journal_mode=DELETE")
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchall():
                    raise LedgerStorageError("SQLite backup integrity check failed")
        copied = SqliteLedger(destination, workspace_id=self.workspace_id, ledger_id=self.ledger_id)
        copied.head()  # Validate the snapshot actually copied, including its chain.
        return destination
