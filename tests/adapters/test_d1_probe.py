import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from weftmark.adapters.sql_ledger import Statement
from weftmark.adapters.sqlite_ledger import SqliteLedger

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("d1_probe", ROOT / "scripts/verify_d1_ledger.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_generated_allowlist_and_local_conformance_path(tmp_path):
    allowed = probe.allowed_statements()
    assert allowed == probe.allowed_statements()
    assert probe.FAILURE_SQL in allowed and probe.MUTATION_SQL in allowed
    assert not any("DROP TABLE" in sql for sql in allowed)
    local = SqliteLedger.create(tmp_path / "database", workspace_id="initial", ledger_id="main")

    class LocalProbe:
        url = "https://local-test.invalid"

        def request(self, path, plan=None, *, authenticated=True):
            if not authenticated:
                return 401, {}
            if path == "/identity":
                revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
                return 200, {"schema": "weftmark.d1-probe.v1", "code_digest": probe.code_digest(allowed), "source_revision": revision}
            if any(s["sql"] not in allowed for s in plan["statements"]):
                return 400, {}
            return 200, self.batch(plan)

        def batch(self, plan):
            statements = tuple(Statement(s["sql"], tuple(s["params"])) for s in plan["statements"])
            rows = local.executor.batch(statements, write=plan.get("write", False))
            return [{"success": True, "results": list(r)} for r in rows]

    report = probe.verify(LocalProbe())
    assert len(report["passed"]) == 19
    assert report["head"]["sequence"] == 5


@pytest.mark.parametrize("url", ["http://example.com", "https://user:password@example.com", "https://example.com/a", "https://example.com?token=test", "https://example.com#fragment"])
def test_credentials_only_sent_to_https_origin(url):
    with pytest.raises(ValueError):
        probe.ProbeClient(url, "test-only")


def test_worker_security_guards_and_atomic_session_contract(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the Worker guard test")
    source = (ROOT / "cloudflare/d1-probe/worker.mjs").read_text().replace(
        'import allowedSql from "./allowed-sql.json";', 'const allowedSql = ["SELECT 1"];')
    worker = tmp_path / "worker.mjs"
    worker.write_text(source)
    harness = tmp_path / "guards.mjs"
    harness.write_text('''
import assert from 'node:assert/strict';
import worker from './worker.mjs';
let batches = 0;
const env = {PROBE_TOKEN:'test-only', EXPIRES_AT:'2999-01-01T00:00:00Z', DB:{
  withSession(mode) { assert.equal(mode,'first-primary'); return {
    prepare(sql) { assert.equal(sql,'SELECT 1'); return { bind(...params) {return {sql,params}} } },
    async batch(statements) { batches++; return statements.map(() => ({success:true,results:[{one:1}]})) }
  }}
}};
const req = (path, body, auth=true) => new Request('https://example.invalid'+path, {
  method: body ? 'POST' : 'GET',
  headers: auth ? {Authorization:'Bearer test-only'} : {}, body:body && JSON.stringify(body)
});
assert.equal((await worker.fetch(req('/identity',null,false),env)).status,401);
assert.equal((await worker.fetch(req('/identity'),{...env,PROBE_TOKEN:undefined})).status,401);
assert.equal((await worker.fetch(req('/identity'),{...env,EXPIRES_AT:'2000-01-01'})).status,410);
assert.equal((await worker.fetch(req('/identity'),{...env,EXPIRES_AT:'invalid'})).status,410);
assert.equal((await worker.fetch(req('/other'),env)).status,404);
const plan={schema:'weftmark.ledger-sql-plan.v1',atomic:true,statements:[{sql:'SELECT 1',params:[]}]};
assert.equal((await worker.fetch(req('/batch',{...plan,atomic:false}),env)).status,400);
assert.equal((await worker.fetch(req('/batch',{...plan,statements:[{sql:'DROP TABLE x',params:[]}]}),env)).status,400);
assert.equal((await worker.fetch(req('/batch',{...plan,statements:[{sql:'SELECT 1',params:[1.5]}]}),env)).status,400);
assert.equal((await worker.fetch(req('/batch',{...plan,statements:[{sql:'SELECT 1',params:['x'.repeat(1048577)]}]}),env)).status,413);
assert.equal(batches,0);
const response=await worker.fetch(req('/batch',plan),env);
assert.equal(response.status,200);
assert.equal(response.headers.get('cache-control'),'no-store');
assert.deepEqual(await response.json(),[{success:true,results:[{one:1}]}]);
assert.equal(batches,1);
console.log('Worker guard assertions passed');
''')
    result = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
