from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from weftmark.adapters.sql_ledger import CallbackBatchExecutor, LedgerStorageError, SqlLedger
from weftmark.adapters.sqlite_ledger import SqliteLedger
from weftmark.application.ports.ledger import LedgerDraft, LEDGER_GENESIS_DIGEST


def test_d1_shaped_atomic_batch_contract_has_no_local_sql_extensions(tmp_path):
    local = SqliteLedger.create(tmp_path / "database.db", workspace_id="workspace", ledger_id="main")
    captured = []
    def d1_shape(plan):
        schema = json.loads((Path(__file__).parents[2] / 'docs/contracts/ledger-v1.schema.json').read_text())
        Draft202012Validator({'$ref':'#/$defs/SqlPlan','$defs':schema['$defs']}).validate(plan)
        assert plan['schema'] == 'weftmark.ledger-sql-plan.v1'
        assert plan['atomic'] is True
        captured.append(plan)
        from weftmark.adapters.sql_ledger import Statement
        statements = tuple(Statement(s['sql'],tuple(s['params'])) for s in plan['statements'])
        rows = local.executor.batch(statements,write=plan['write'])
        return [{'success':True,'results':list(r)} for r in rows]
    portable = SqlLedger(CallbackBatchExecutor(d1_shape,atomic=True),workspace_id='workspace',ledger_id='main')
    command = LedgerDraft('test','one','{}',datetime(2026,10,14,tzinfo=timezone.utc))
    first = portable.append_idempotent(command,operation_id='request',expected_digest=LEDGER_GENESIS_DIGEST)
    assert portable.append_idempotent(command,operation_id='request',expected_digest=LEDGER_GENESIS_DIGEST) == first
    assert portable.entries() == local.entries()
    for plan in captured:
        for statement in plan['statements']:
            sql = statement['sql'].upper()
            assert not any(word in sql for word in ('PRAGMA','BEGIN','COMMIT','RETURNING','NOSTOI_DIGEST'))


def test_nontransactional_callback_and_incomplete_results_fail_closed():
    with pytest.raises(LedgerStorageError, match='transactional'):
        CallbackBatchExecutor(lambda _:[],atomic=False)
    host = CallbackBatchExecutor(lambda _:[],atomic=True)
    from weftmark.adapters.sql_ledger import Statement
    with pytest.raises(LedgerStorageError,match='incomplete'):
        host.batch((Statement('SELECT 1'),))


@pytest.mark.parametrize('result',[
    [{'success':False,'results':[]}],
    [{'success':True,'results':None}],
    [{'success':True,'results':[42]}],
])
def test_callback_invalid_or_failed_response_is_not_a_success(result):
    from weftmark.adapters.sql_ledger import Statement
    host = CallbackBatchExecutor(lambda _:result,atomic=True)
    with pytest.raises(LedgerStorageError):
        host.batch((Statement('SELECT 1'),))
