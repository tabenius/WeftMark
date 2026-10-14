from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import multiprocessing
import os
import sqlite3

import nostoi
import pytest

from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.adapters.ledger_codec import LedgerCodecError
from weftmark.adapters.sql_ledger import LedgerStorageError, Statement
from weftmark.adapters.sqlite_ledger import SqliteLedger
from weftmark.application.ledger import LedgerService
from weftmark.application.ports.ledger import (
    LEDGER_GENESIS_DIGEST, IdempotentLedgerPort, LedgerDraft, LedgerHeadChanged,
    LedgerIdempotencyConflict, LedgerPort,
)

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def draft(entity="one", payload="{}"):
    return LedgerDraft("test", entity, payload, NOW)


def process_append(path, entity, queue):
    try:
        storage = SqliteLedger(path, workspace_id="workspace", ledger_id="main")
        entry = storage.append_idempotent(draft(entity), operation_id="shared-operation")
        queue.put(("ok", entry.sequence, entry.digest))
    except Exception as error:
        queue.put((type(error).__name__,))


@pytest.fixture
def ledger(tmp_path):
    return SqliteLedger.create(tmp_path / "ledger.db", workspace_id="workspace", ledger_id="main")


def test_port_roundtrip_wal_durability_and_nostoi_verification(ledger):
    assert isinstance(ledger, LedgerPort)
    assert isinstance(ledger, IdempotentLedgerPort)
    assert ledger.head().sequence == 0
    first = ledger.append(draft(payload='{"text":"\\u03bb","value":18446744073709551615}'))
    second = LedgerService(ledger).record(kind="evidence", entity_id="repo:two", payload={"passed": True}, recorded_at=NOW)
    assert (first.sequence, second.sequence) == (1, 2)
    assert second.previous_digest == first.digest
    reopened = SqliteLedger(ledger.executor.path, workspace_id="workspace", ledger_id="main")
    assert reopened.entries() == (first, second)
    assert nostoi.verify_jsonl(reopened.export_jsonl(), "weftmark-ledger-v1")["ok"]
    with ledger.executor.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert os.stat(ledger.executor.path).st_mode & 0o077 == 0


def test_cas_and_idempotency_replay_after_head_advances(ledger):
    command = draft()
    first = ledger.append_idempotent(command, operation_id="request", expected_digest=LEDGER_GENESIS_DIGEST, principal_id="alice")
    ledger.append(draft("two"))
    assert ledger.append_idempotent(command, operation_id="request", expected_digest=LEDGER_GENESIS_DIGEST, principal_id="alice") == first
    with pytest.raises(LedgerHeadChanged):
        ledger.append_if_head(draft("stale"), expected_digest=LEDGER_GENESIS_DIGEST)
    with pytest.raises(LedgerIdempotencyConflict):
        ledger.append_idempotent(draft("different"), operation_id="request", expected_digest=LEDGER_GENESIS_DIGEST, principal_id="alice")
    with pytest.raises(LedgerIdempotencyConflict):
        ledger.append_idempotent(command, operation_id="request", expected_digest=LEDGER_GENESIS_DIGEST, principal_id="bob")
    assert len(ledger.entries()) == 2


def test_concurrent_unconditional_appends_have_no_gaps(ledger):
    def append(index):
        independent = SqliteLedger(ledger.executor.path, workspace_id="workspace", ledger_id="main")
        return independent.append(draft(f"worker-{index}"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append, range(8)))
    assert sorted(e.sequence for e in results) == list(range(1, 9))
    assert len(ledger.entries()) == 8


def test_concurrent_cas_has_one_winner(ledger):
    def append(index):
        try:
            return ledger.append_if_head(draft(f"worker-{index}"), expected_digest=LEDGER_GENESIS_DIGEST)
        except LedgerHeadChanged:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(ledger.entries()) == 1


def test_processes_replay_one_atomic_operation(ledger):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [context.Process(target=process_append, args=(str(ledger.executor.path), "one", queue)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        results = [queue.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
        assert all(result == results[0] for result in results)
        assert results[0][0] == "ok"
        assert ledger.head().sequence == 1
    finally:
        # Only these test-owned children can be stopped; never sweep processes.
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        queue.close()


def test_batch_failure_rolls_back_entry_receipt_and_head(ledger):
    original = ledger.executor.batch
    def fail(statements, *, write=False):
        if write:
            statements = statements[:2] + (Statement("SELECT * FROM missing_table"),) + statements[2:]
        return original(statements, write=write)
    ledger.executor.batch = fail
    with pytest.raises(LedgerStorageError):
        ledger.append_idempotent(draft(), operation_id="failed")
    ledger.executor.batch = original
    assert ledger.head().sequence == 0
    assert ledger.entries() == ()
    # A rolled-back operation does not reserve or poison its idempotency key.
    assert ledger.append_idempotent(draft(), operation_id="failed").sequence == 1


def test_workspaces_and_streams_are_isolated(ledger):
    other = SqliteLedger(ledger.executor.path, workspace_id="other", ledger_id="main")
    other.create_stream()
    first = ledger.append_idempotent(draft(), operation_id="same", principal_id="alice")
    second = other.append_idempotent(draft("other"), operation_id="same", principal_id="bob")
    assert first.sequence == second.sequence == 1
    assert ledger.entries() == (first,)
    assert other.entries() == (second,)
    with pytest.raises(LedgerStorageError, match="another ledger"):
        other.verify_checkpoint(ledger.head())


def test_triggers_refuse_ordinary_mutation(ledger):
    ledger.append(draft())
    for sql in (
        "UPDATE weftmark_ledger_entries SET kind='forged'",
        "DELETE FROM weftmark_ledger_entries",
        "UPDATE weftmark_ledger_receipts SET principal_id='forged'",
        "DELETE FROM weftmark_ledger_receipts",
        "UPDATE weftmark_ledger_streams SET ledger_id='forged'",
        "DELETE FROM weftmark_ledger_streams",
    ):
        with pytest.raises(LedgerStorageError):
            ledger.executor.batch((Statement(sql),), write=True)
    assert len(ledger.entries()) == 1


def test_owner_can_bypass_triggers_but_content_tampering_is_refused(ledger):
    ledger.append(draft())
    with ledger.executor.connect() as conn:
        conn.execute("DROP TRIGGER weftmark_ledger_entries_update")
        record = json.loads(conn.execute("SELECT record_json FROM weftmark_ledger_entries").fetchone()[0])
        record["payload"] = {"forged": True}
        conn.execute("UPDATE weftmark_ledger_entries SET record_json=?", (nostoi.canonical(record),))
    with pytest.raises(LedgerStorageError):
        ledger.entries()
    with pytest.raises(LedgerStorageError):
        ledger.append(draft("must-not-extend-corruption"))


def test_consistent_tail_removal_requires_external_checkpoint(ledger):
    first = ledger.append(draft())
    ledger.append(draft("two"))
    trusted = ledger.head()
    with ledger.executor.connect() as conn:
        for trigger in ("weftmark_ledger_entries_delete", "weftmark_ledger_receipts_delete", "weftmark_ledger_streams_update"):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute("DELETE FROM weftmark_ledger_receipts WHERE sequence=2")
        conn.execute("DELETE FROM weftmark_ledger_entries WHERE sequence=2")
        conn.execute("UPDATE weftmark_ledger_streams SET head_sequence=1,head_digest=?", (first.digest,))
    # A perfectly valid prefix is not distinguishable from a shorter history.
    assert ledger.head().sequence == 1
    with pytest.raises(LedgerStorageError, match="older"):
        ledger.verify_checkpoint(trusted)


def test_recomputed_fork_disagrees_with_external_checkpoint(ledger, tmp_path):
    ledger.append(draft())
    trusted = ledger.head()
    forged = SqliteLedger.create(tmp_path / "forged.db", workspace_id="workspace", ledger_id="main")
    forged.append(draft("rewritten"))
    assert forged.head().sequence == trusted.sequence
    with pytest.raises(LedgerStorageError, match="disagrees"):
        forged.verify_checkpoint(trusted)


def test_record_checkpoint_does_not_authenticate_receipts_against_owner(ledger):
    ledger.append_idempotent(draft(), operation_id="one", principal_id="alice")
    trusted = ledger.head()
    with ledger.executor.connect() as conn:
        conn.execute("DROP TRIGGER weftmark_ledger_receipts_update")
        conn.execute("DROP TRIGGER weftmark_ledger_entries_update")
        request = json.loads(conn.execute("SELECT request_json FROM weftmark_ledger_receipts").fetchone()[0])
        request["principal_id"] = "forged"
        text = nostoi.canonical(request)
        import hashlib
        request_digest = hashlib.sha256(text.encode()).hexdigest()
        conn.execute("UPDATE weftmark_ledger_receipts SET principal_id='forged',request_json=?,request_digest=?", (text, request_digest))
        conn.execute("UPDATE weftmark_ledger_entries SET request_digest=?", (request_digest,))
    # This explicit limit prevents callers mistaking record coverage for
    # externally authenticated receipt/principal coverage.
    assert ledger.verify_checkpoint(trusted) == trusted


def test_jsonl_import_export_preserves_digests_and_atomic_empty_stream(ledger, tmp_path):
    source = JsonlLedger(tmp_path / "source.jsonl")
    expected = (source.append(draft()), source.append(draft("two", '{"unicode":"\\ud83d\\ude00"}')))
    original = source.path.read_bytes()
    head = ledger.import_jsonl(original.decode())
    assert head.digest == expected[-1].digest
    assert ledger.entries() == expected
    assert ledger.export_jsonl().encode() == original
    assert source.path.read_bytes() == original
    with pytest.raises(LedgerStorageError, match="empty"):
        ledger.import_jsonl(original.decode())


def test_import_refuses_corruption_before_mutation(ledger):
    with pytest.raises(LedgerCodecError):
        ledger.import_jsonl('{"sequence":1}\n')
    assert ledger.head().sequence == 0


def test_import_mid_batch_failure_rolls_back_every_record(ledger, tmp_path):
    source = JsonlLedger(tmp_path / "source.jsonl")
    source.append(draft())
    source.append(draft("two"))
    with ledger.executor.connect() as conn:
        conn.execute("""CREATE TRIGGER injected_failure BEFORE INSERT ON weftmark_ledger_entries
                     WHEN NEW.sequence=2 BEGIN SELECT RAISE(ABORT,'test failure'); END""")
    with pytest.raises(LedgerStorageError):
        ledger.import_jsonl(source.path.read_text())
    assert ledger.head().sequence == 0


@pytest.mark.parametrize("damage", [
    "UPDATE weftmark_ledger_streams SET head_sequence=2",
    "DELETE FROM weftmark_ledger_receipts",
    "UPDATE weftmark_ledger_entries SET kind='changed-index'",
    "UPDATE weftmark_ledger_receipts SET request_json='{}'",
])
def test_inconsistent_head_receipt_or_index_fails_closed(ledger, damage):
    ledger.append(draft())
    with ledger.executor.connect() as conn:
        for trigger in ("weftmark_ledger_streams_update", "weftmark_ledger_receipts_delete",
                        "weftmark_ledger_receipts_update", "weftmark_ledger_entries_update"):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute(damage)
    with pytest.raises(LedgerStorageError):
        ledger.head()


def test_foreign_component_version_is_refused_before_write(ledger):
    with ledger.executor.connect() as conn:
        conn.execute("DROP TRIGGER weftmark_ledger_schema_update")
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE weftmark_ledger_schema SET version=2")
    with pytest.raises(LedgerStorageError, match="schema"):
        ledger.append(draft())
    with ledger.executor.connect() as conn:
        assert conn.execute("SELECT count(*) FROM weftmark_ledger_entries").fetchone()[0] == 0


@pytest.mark.parametrize("payload", ['{"value":1.5}', '{"value":18446744073709551616}'])
def test_nostoi_numeric_refusals_never_coerce_or_write(ledger, payload):
    with pytest.raises(LedgerCodecError):
        ledger.append(draft(payload=payload))
    assert ledger.head().sequence == 0


def test_verified_backup_does_not_overwrite_or_depend_on_wal(ledger, tmp_path):
    ledger.append(draft())
    destination = ledger.backup(tmp_path / "backup.db")
    copied = SqliteLedger(destination, workspace_id="workspace", ledger_id="main")
    assert copied.head() == ledger.head()
    assert not (tmp_path / "backup.db-wal").exists()
    with pytest.raises(FileExistsError):
        ledger.backup(destination)


def test_refuse_missing_existing_foreign_and_symlinked_databases(tmp_path):
    with pytest.raises(LedgerStorageError, match="missing"):
        SqliteLedger(tmp_path / "missing.db", workspace_id="workspace", ledger_id="main")
    foreign = tmp_path / "foreign.db"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE other(value TEXT)")
    foreign.chmod(0o600)
    with pytest.raises(LedgerStorageError):
        SqliteLedger(foreign, workspace_id="workspace", ledger_id="main")
    with pytest.raises(FileExistsError):
        SqliteLedger.create(foreign, workspace_id="workspace", ledger_id="main")
    link = tmp_path / "link.db"
    link.symlink_to(foreign)
    with pytest.raises(LedgerStorageError, match="symlink"):
        SqliteLedger(link, workspace_id="workspace", ledger_id="main")
