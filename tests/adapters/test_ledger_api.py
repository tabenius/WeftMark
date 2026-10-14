from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from weftmark.adapters.ledger_api import LedgerApi, Principal
from weftmark.adapters.sqlite_ledger import SqliteLedger
from weftmark.application.ports.ledger import LedgerDraft, LEDGER_GENESIS_DIGEST

SCHEMA = json.loads((Path(__file__).parents[2] / 'docs/contracts/ledger-v1.schema.json').read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
NOW = datetime(2026,10,14,12,tzinfo=timezone.utc)


def request(operation='one', expected=LEDGER_GENESIS_DIGEST, payload='{}'):
    return {'schema':'weftmark.ledger-append.v1','operation_id':operation,'expected_digest':expected,
            'draft':{'kind':'test','entity_id':'repo:one','payload_json':payload,'recorded_at':NOW.isoformat()}}


@pytest.fixture
def context(tmp_path):
    ledger = SqliteLedger.create(tmp_path/'ledger.db',workspace_id='workspace',ledger_id='main')
    resolver = Mock(return_value=ledger)
    api = LedgerApi(resolver)
    principal = Principal('alice',{'workspace':frozenset({'read','append'})})
    return ledger,resolver,api,principal


def assert_schema(result):
    VALIDATOR.validate(result.body)
    return result


def test_api_contract_roundtrip_replay_and_pagination(context):
    ledger,_,api,principal = context
    body = request(payload='{"large":18446744073709551615}')
    VALIDATOR.validate(body)
    first = assert_schema(api.append(principal,'workspace','main',body))
    assert first.status == 200
    VALIDATOR.evolve(schema={'$ref':'#/$defs/Record','$defs':SCHEMA['$defs']}).validate(json.loads(first.body['data']['record_json']))
    assert json.loads(first.body['data']['record_json'])['payload']['large'] == 18446744073709551615
    second = api.append(principal,'workspace','main',request('two',ledger.head().digest))
    assert second.status == 200
    assert api.append(principal,'workspace','main',body) == first
    page = assert_schema(api.entries(principal,'workspace','main',limit=1))
    assert page.body['data']['next_cursor'] == '1'
    assert len(page.body['data']['records']) == 1
    assert page.body['data']['head']['sequence'] == 2
    last = assert_schema(api.entries(principal,'workspace','main',after='1',limit=1))
    assert last.body['data']['next_cursor'] is None
    assert json.loads(last.body['data']['records'][0])['sequence'] == 2
    assert_schema(api.head(principal,'workspace','main'))


def test_authorization_precedes_storage_resolution(context):
    _,resolver,api,_ = context
    assert assert_schema(api.head(None,'workspace','main')).status == 401
    denied = Principal('bob',{'other':frozenset({'read','append'})})
    assert assert_schema(api.entries(denied,'workspace','main')).status == 403
    assert assert_schema(api.append(denied,'workspace','main',request())).status == 403
    resolver.assert_not_called()


def test_read_and_append_permissions_are_distinct(context):
    _,_,api,_ = context
    reader = Principal('reader',{'workspace':frozenset({'read'})})
    writer = Principal('writer',{'workspace':frozenset({'append'})})
    assert api.append(reader,'workspace','main',request()).status == 403
    assert api.entries(writer,'workspace','main').status == 403
    assert api.append(writer,'workspace','main',request()).status == 200


def test_another_authorized_principal_cannot_replay_someone_elses_operation(context):
    ledger,_,api,alice = context
    assert api.append(alice,'workspace','main',request()).status == 200
    bob = Principal('bob',{'workspace':frozenset({'append'})})
    result = assert_schema(api.append(bob,'workspace','main',request()))
    assert result.status == 409
    assert result.body['error']['code'] == 'idempotency_conflict'
    assert 'data' not in result.body
    assert ledger.head().sequence == 1


@pytest.mark.parametrize('change',[
    lambda body: body.update(principal_id='admin'),
    lambda body: body.update(sql='DELETE FROM everything'),
    lambda body: body.update(expected_digest=None),
    lambda body: body.update(operation_id=''),
    lambda body: body['draft'].update(recorded_at='2026-10-14'),
    lambda body: body['draft'].update(payload_json='{"value":1.5}'),
    lambda body: body['draft'].update(payload_json='{"value":18446744073709551616}'),
    lambda body: body['draft'].update(payload_json='{"secret":"test-only"}'),
    lambda body: body['draft'].update(payload_json='{"x":1,"x":2}'),
    lambda body: body['draft'].update(payload_json='{"x": 1}'),
])
def test_malformed_unsafe_or_authority_bearing_requests_fail_before_write(context,change):
    ledger,_,api,principal = context
    body = request()
    change(body)
    result = assert_schema(api.append(principal,'workspace','main',body))
    assert result.status == 400
    assert ledger.head().sequence == 0


def test_conflict_has_an_explicit_result_and_no_mutation(context):
    ledger,_,api,principal = context
    api.append(principal,'workspace','main',request())
    result = assert_schema(api.append(principal,'workspace','main',request('stale')))
    assert result.status == 409 and result.body['error']['code'] == 'head_changed'
    assert ledger.head().sequence == 1


def test_resolution_cannot_cross_workspace_boundary(context):
    _,_,api,_ = context
    principal = Principal('admin',{'other':frozenset({'read'})})
    result = assert_schema(api.head(principal,'other','main'))
    assert result.status == 503


@pytest.mark.parametrize('after,limit',[('-1',10),('01',10),('9007199254740992',10),('0',0),('0',True),('0',201)])
def test_cursor_and_limit_bounds(context,after,limit):
    _,_,api,principal = context
    assert assert_schema(api.entries(principal,'workspace','main',after=after,limit=limit)).status == 400


def test_corrupt_storage_is_unavailable_not_an_invalid_client_request(context):
    ledger,_,api,principal = context
    ledger.append(LedgerDraft('test','one','{}',NOW))
    with ledger.executor.connect() as conn:
        conn.execute('DROP TRIGGER weftmark_ledger_entries_update')
        conn.execute("UPDATE weftmark_ledger_entries SET record_json='{}'")
    result = assert_schema(api.head(principal,'workspace','main'))
    assert result.status == 503 and result.body['error']['code'] == 'storage_unavailable'


def test_page_byte_cap_has_a_continuation_cursor(context):
    ledger,_,api,principal = context
    payload = json.dumps({'text':'x'*180000},sort_keys=True,separators=(',',':'))
    for i in range(8):
        ledger.append(LedgerDraft('large',str(i),payload,NOW))
    page = assert_schema(api.entries(principal,'workspace','main',limit=100))
    records = page.body['data']['records']
    assert 0 < len(records) < 8
    assert page.body['data']['next_cursor'] is not None
    assert sum(len(json.dumps(record).encode()) for record in records) <= 1048576
