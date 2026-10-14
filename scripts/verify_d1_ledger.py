"""Prepare a synthetic D1 probe deployment and verify its actual remote batches.

No Cloudflare credentials are read here. Wrangler owns account authentication;
this client only reads a private, resource-specific temporary probe token file.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

from weftmark.adapters.ledger_codec import canonical
from weftmark.adapters.sql_ledger import CallbackBatchExecutor, LedgerStorageError, SqlLedger, Statement, schema_sql
from weftmark.adapters.sqlite_ledger import SqliteLedger
from weftmark.application.ports.ledger import LEDGER_GENESIS_DIGEST, LedgerDraft, LedgerHeadChanged, LedgerIdempotencyConflict

ROOT = Path(__file__).resolve().parents[1]
FAILURE_SQL = "SELECT * FROM weftmark_deliberately_missing_probe_table"
MUTATION_SQL = "UPDATE weftmark_ledger_entries SET kind=? WHERE workspace_id=? AND ledger_id=?"


def code_digest(allowed):
    parts = [Path(__file__).read_bytes(), (ROOT / "cloudflare/d1-probe/worker.mjs").read_bytes(),
             canonical(allowed).encode(), schema_sql().encode()]
    return hashlib.sha256(b"\0".join(parts)).hexdigest()


def allowed_statements():
    captured = set()
    with tempfile.TemporaryDirectory() as directory:
        local = SqliteLedger.create(Path(directory) / "db", workspace_id="probe", ledger_id="main")
        execute = local.executor.batch
        def capture(statements, *, write=False):
            captured.update(s.sql for s in statements)
            return execute(statements, write=write)
        local.executor.batch = capture
        local.create_stream()
        local.append(LedgerDraft("probe", "synthetic", "{}", datetime(2026, 10, 15, tzinfo=timezone.utc)))
        local.head()
    return sorted(captured | {FAILURE_SQL, MUTATION_SQL})


def prepare(args):
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=False)
    allowed = allowed_statements()
    shutil.copyfile(ROOT / "cloudflare/d1-probe/worker.mjs", directory / "worker.mjs")
    (directory / "allowed-sql.json").write_text(json.dumps(allowed))
    (directory / "schema.sql").write_text(schema_sql())
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    config = {"name": args.worker_name, "main": "worker.mjs", "compatibility_date": "2026-10-01",
        "account_id": args.account_id, "workers_dev": True, "preview_urls": False,
        "observability": {"enabled": False},
        "d1_databases": [{"binding": "DB", "database_name": args.database_name, "database_id": args.database_id}],
        "vars": {"SOURCE_REVISION": revision, "PROBE_CODE_DIGEST": code_digest(allowed),
                 "EXPIRES_AT": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()}}
    (directory / "wrangler.json").write_text(json.dumps(config, indent=2) + "\n")
    print(json.dumps({"directory": str(directory), "source_revision": revision, "code_digest": code_digest(allowed)}))


class ProbeClient:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("probe URL must be an HTTPS origin")
        self.url, self.token = url.rstrip("/"), token.strip()
        if not self.token:
            raise ValueError("empty probe credential")

    def request(self, path, plan=None, *, authenticated=True):
        data = json.dumps(plan).encode() if plan is not None else None
        headers = {"Content-Type": "application/json", "User-Agent": "weftmark-d1-conformance/1.0"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.url + path, data=data, headers=headers)
        # Never forward credentials to an HTTP redirect or a different origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=30) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError("probe response exceeded limit")
                return response.status, json.loads(body)
        except urllib.error.HTTPError as error:
            return error.code, {"error": "remote_request_refused"}

    def batch(self, plan):
        status, result = self.request("/batch", plan)
        if status != 200:
            raise LedgerStorageError("remote probe batch failed")
        return result


def verify(client):
    results = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        results.append(name)
    check("unauthenticated_refused", client.request("/identity", authenticated=False)[0] == 401)
    status, identity = client.request("/identity")
    check("authenticated_identity", status == 200 and identity["schema"] == "weftmark.d1-probe.v1")
    check("deployed_code_matches", identity["code_digest"] == code_digest(allowed_statements()))
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    check("deployed_revision_matches", identity["source_revision"] == revision)
    check("unallowlisted_sql_refused", client.request("/batch", {"schema": "weftmark.ledger-sql-plan.v1",
        "atomic": True, "statements": [{"sql": "DROP TABLE weftmark_ledger_entries", "params": []}]})[0] == 400)
    host = CallbackBatchExecutor(client.batch, atomic=True)
    workspace = "probe-" + uuid.uuid4().hex
    ledger = SqlLedger(host, workspace_id=workspace, ledger_id="main")
    ledger.create_stream()
    check("empty_genesis", ledger.head().sequence == 0 and ledger.head().digest == LEDGER_GENESIS_DIGEST)
    now = datetime.now(timezone.utc)
    command = LedgerDraft("probe", "synthetic", canonical({"large": 2**64 - 1, "unicode": "λ😀"}), now)
    first = ledger.append_idempotent(command, operation_id="one", expected_digest=LEDGER_GENESIS_DIGEST, principal_id="alice")
    check("exact_64_bit_unicode_roundtrip", first.payload == {"large": 2**64 - 1, "unicode": "λ😀"})
    ledger.append(LedgerDraft("probe", "two", "{}", now))
    check("replay_after_head_advances", ledger.append_idempotent(command, operation_id="one",
        expected_digest=LEDGER_GENESIS_DIGEST, principal_id="alice") == first)
    try:
        ledger.append_idempotent(command, operation_id="one", expected_digest=LEDGER_GENESIS_DIGEST, principal_id="bob")
    except LedgerIdempotencyConflict:
        check("principal_conflict", True)
    else:
        check("principal_conflict", False)
    try:
        ledger.append_if_head(command, expected_digest=LEDGER_GENESIS_DIGEST)
    except LedgerHeadChanged:
        check("stale_cas_refused", True)
    else:
        check("stale_cas_refused", False)
    before = ledger.head()
    def fail_batch(plan):
        if plan.get("write"):
            plan["statements"].insert(2, {"sql": FAILURE_SQL, "params": []})
        return client.batch(plan)
    broken = SqlLedger(CallbackBatchExecutor(fail_batch, atomic=True), workspace_id=workspace, ledger_id="main")
    try:
        broken.append_idempotent(command, operation_id="rollback")
    except LedgerStorageError:
        check("failed_batch_reported", True)
    else:
        check("failed_batch_reported", False)
    check("entry_receipt_head_rollback", ledger.head() == before)
    check("failed_operation_can_retry", ledger.append_idempotent(command, operation_id="rollback").sequence == before.sequence + 1)
    try:
        host.batch((Statement(MUTATION_SQL, ("forged", workspace, "main")),), write=True)
    except LedgerStorageError:
        check("immutability_trigger", True)
    else:
        check("immutability_trigger", False)
    other = SqlLedger(host, workspace_id=workspace + "-other", ledger_id="main")
    other.create_stream()
    check("workspace_isolation", other.entries() == () and ledger.head().sequence == 3)
    head = ledger.head()
    def compete(index):
        try:
            return ledger.append_if_head(LedgerDraft("race", str(index), "{}", now), expected_digest=head.digest)
        except LedgerHeadChanged:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(compete, range(4)))
    check("concurrent_cas_one_winner", sum(e is not None for e in entries) == 1 and ledger.head().sequence == head.sequence + 1)
    def replay(_):
        return ledger.append_idempotent(command, operation_id="concurrent-replay", principal_id="alice")
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(replay, range(4)))
    check("concurrent_replay_one_entry", len({e.digest for e in entries}) == 1 and ledger.head().sequence == head.sequence + 2)
    check("native_nostoi_full_chain_verification", len(ledger.entries()) == 5)
    exported = ledger.export_jsonl()
    import nostoi
    check("export_nostoi_verification", nostoi.verify_jsonl(exported, "weftmark-ledger-v1")["ok"])
    return {"schema": "weftmark.d1-verification.v1", "verified_at": datetime.now(timezone.utc).isoformat(),
        "worker": client.url, "deployment": identity, "workspace_id": workspace,
        "head": {"sequence": ledger.head().sequence, "digest": ledger.head().digest},
        "passed": results, "export_sha256": hashlib.sha256(exported.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare")
    for name in ("directory", "database-id", "database-name", "account-id", "worker-name"):
        setup.add_argument("--" + name, required=True)
    run = commands.add_parser("verify")
    run.add_argument("--url", required=True)
    run.add_argument("--token-file", required=True)
    run.add_argument("--report")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    else:
        token = Path(args.token_file)
        if token.is_symlink() or not token.is_file() or token.stat().st_mode & 0o077:
            raise ValueError("probe token must be a private regular file")
        report = verify(ProbeClient(args.url, token.read_text()))
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.report:
            Path(args.report).write_text(text)
        print(text, end="")


if __name__ == "__main__":
    main()
