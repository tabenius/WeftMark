import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from weftmark.adapters.sql_ledger import Statement
from weftmark.adapters.sqlite_ledger import SqliteLedger

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("d1_probe", ROOT / "scripts/verify_d1_ledger.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_generated_allowlist_and_local_conformance_path(tmp_path, monkeypatch):
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    monkeypatch.setattr(probe, "clean_source_revision", lambda: revision)
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
    assert report["schema"] == "weftmark.d1-verification.v2"
    assert len(report["passed"]) == 19
    assert report["head"]["sequence"] == 5
    assert report["source_clean"] is True
    assert report["artifact_provenance_verified"] is False
    assert "deployed_code_matches" not in report["passed"]
    assert report["identity_evidence"] == "authenticated_worker_configuration_self_report"
    monkeypatch.setattr(probe, "clean_source_revision", Mock(side_effect=[revision, "0" * 40]))
    with pytest.raises(ValueError, match="source changed"):
        probe.verify(LocalProbe())


@pytest.fixture
def clean_git_source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / "source.txt").write_text("Committed source\n")
    subprocess.run(["git", "add", "source.txt"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-m", "Test source fixture"], cwd=root, check=True, capture_output=True)
    monkeypatch.setattr(probe, "ROOT", root)
    return root


def test_source_identity_requires_an_exact_clean_repository(clean_git_source):
    revision = probe.clean_source_revision()
    assert len(revision) == 40
    assert revision == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clean_git_source, text=True).strip()


@pytest.mark.parametrize("dirty_kind", ["tracked", "staged", "untracked"])
def test_dirty_source_is_refused_before_prepare_outputs_or_network(clean_git_source, tmp_path, dirty_kind):
    target = clean_git_source / ("untracked.txt" if dirty_kind == "untracked" else "source.txt")
    target.write_text("Uncommitted input\n")
    if dirty_kind == "staged":
        subprocess.run(["git", "add", "source.txt"], cwd=clean_git_source, check=True)
    destination = tmp_path / "deployment"
    with pytest.raises(ValueError, match="clean source"):
        probe.prepare(SimpleNamespace(directory=destination))
    assert not destination.exists()
    client = SimpleNamespace(request=Mock(), batch=Mock())
    with pytest.raises(ValueError, match="clean source"):
        probe.verify(client)
    client.request.assert_not_called()
    client.batch.assert_not_called()


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
