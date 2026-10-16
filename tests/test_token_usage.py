import asyncio
import json
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
import httpx
from fastapi import HTTPException

from app.token_usage import TokenMeter
from tests.test_backend import backend, headers


@pytest.mark.parametrize(('token', 'authorization'), [('', None), ('employee-jwt', 'Bearer employee-jwt')])
def test_backend_omits_empty_bearer_header_for_durable_settlement(token, authorization):
    from app.backend_client import BackendClient
    from app.config import Settings

    def handle(request):
        assert request.headers.get('Authorization') == authorization
        assert request.headers['X-AI-Service-Key'] == 'x' * 32
        return httpx.Response(200, json={'success': True, 'data': {'accepted': True}})

    async def run():
        client = BackendClient(Settings(_env_file=None, backend_url='http://127.0.0.1', ai_service_secret='x' * 32))
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            assert await client.call('token-settle', token, {'id': 'test'}) == {'accepted': True}
        finally:
            await client.close()

    asyncio.run(run())


def setup(tmp_path, response=None):
    response = response or NS(id='resp_test', usage=NS(input_tokens=30, output_tokens=20, total_tokens=50,
                                                     input_tokens_details=NS(cached_tokens=10)))
    client = NS(responses=NS(input_tokens=NS(count=AsyncMock(return_value=NS(input_tokens=40))),
                            create=AsyncMock(return_value=response)))
    client.with_options = Mock(return_value=client)
    calls = []

    async def call(endpoint, token, body=None):
        calls.append((endpoint, token, body))
        return {'id': body['id']} if endpoint == 'token-reserve' else {'quota': {}}

    backend = NS(call=AsyncMock(side_effect=call))
    meter = TokenMeter(tmp_path / 'chat.sqlite3', backend)
    return meter, client, NS(credential='trusted-jwt'), calls


def create(meter, client, identity):
    return meter.create(client, identity, 'chat', model='test-model', input='Question',
                        instructions='Safe instructions', tools=[], max_output_tokens=100, store=False)


def rows(meter):
    with sqlite3.connect(meter.path) as db:
        return db.execute('SELECT reservation, settlement, delivered FROM ai_usage_outbox').fetchall()


def test_exact_budget_disabled_retries_and_cached_subset(tmp_path):
    meter, client, identity, calls = setup(tmp_path)
    asyncio.run(create(meter, client, identity))
    assert calls[0][0] == 'token-reserve'
    assert calls[0][2]['tokens'] == 140
    assert calls[0][1] == 'trusted-jwt'
    assert calls[1][0] == 'token-settle' and calls[1][1] == ''
    assert calls[1][2]['total_tokens'] == 50
    assert calls[1][2]['cached_tokens'] == 10
    client.with_options.assert_called_once_with(max_retries=0)
    assert 'store' not in client.responses.input_tokens.count.call_args.kwargs
    assert 'max_output_tokens' not in client.responses.input_tokens.count.call_args.kwargs
    assert 'Question' not in str(rows(meter)) and 'trusted-jwt' not in str(rows(meter))


def test_quota_failure_never_calls_model(tmp_path):
    meter, client, identity, calls = setup(tmp_path)
    meter.backend.call = AsyncMock(side_effect=HTTPException(429, 'Quota reached'))
    with pytest.raises(HTTPException):
        asyncio.run(create(meter, client, identity))
    client.responses.create.assert_not_awaited()
    assert json.loads(rows(meter)[0][1])['state'] == 'cancelled'


def test_delivery_retries_survive_restart_without_repeat_model_call(tmp_path):
    meter, client, identity, calls = setup(tmp_path)
    original = meter.backend.call.side_effect

    async def offline(endpoint, token, body):
        if endpoint == 'token-settle':
            raise HTTPException(502, 'Unavailable')
        return await original(endpoint, token, body)

    meter.backend.call.side_effect = offline
    asyncio.run(create(meter, client, identity))
    assert rows(meter)[0][2] == 0
    meter.backend.call.side_effect = original
    restarted = TokenMeter(meter.path, meter.backend)
    asyncio.run(restarted.flush())
    assert rows(meter)[0][2] == 1
    client.responses.create.assert_awaited_once()


def test_timeout_unknown_not_zero(tmp_path):
    meter, client, identity, calls = setup(tmp_path)
    client.responses.create.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        asyncio.run(create(meter, client, identity))
    settlement = json.loads(rows(meter)[0][1])
    assert settlement['state'] == 'unknown' and 'total_tokens' not in settlement


def test_missing_usage_retains_unknown_reservation(tmp_path):
    meter, client, identity, calls = setup(tmp_path, NS(id='resp_test', usage=None))
    asyncio.run(create(meter, client, identity))
    assert json.loads(rows(meter)[0][1])['state'] == 'unknown'
    assert json.loads(rows(meter)[0][1])['response_id'] == 'resp_test'


def test_incomplete_response_still_records_usage(tmp_path):
    meter, client, identity, calls = setup(tmp_path)
    client.responses.create.return_value.status = 'incomplete'
    response = asyncio.run(create(meter, client, identity))
    assert response.status == 'incomplete'
    assert json.loads(rows(meter)[0][1])['total_tokens'] == 50


def test_crash_drafts_never_auto_release(tmp_path):
    meter, client, identity, calls = setup(tmp_path)
    with meter.connect() as db:
        db.execute('INSERT INTO ai_usage_outbox(id,reservation) VALUES (?,?)', ('crashed', '{}'))
    asyncio.run(meter.flush())
    assert calls == []
    assert rows(meter)[0][1] is None


def test_cancellation_retains_unknown_usage(tmp_path):
    meter, client, identity, calls = setup(tmp_path)
    client.responses.create.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(create(meter, client, identity))
    assert json.loads(rows(meter)[0][1])['state'] == 'unknown'


def test_invalid_preflight_never_reserves_or_generates(tmp_path):
    meter, client, identity, calls = setup(tmp_path)
    client.responses.input_tokens.count.return_value.input_tokens = -1
    with pytest.raises(HTTPException):
        asyncio.run(create(meter, client, identity))
    assert calls == []
    client.responses.create.assert_not_awaited()


def test_semantic_chat_wiring_records_before_bad_model_output(tmp_path, backend):
    from tests.test_semantic_query import enable, request
    client, state = backend
    mock = enable(client, state, [request(action='employee_count', subject='company', employee_name=None,
                                         start_date=None, end_date=None, period_kind='none')])
    service = client.app.state.chat
    service.client.responses.input_tokens = NS(count=AsyncMock(return_value=NS(input_tokens=40)))
    service.client.with_options = Mock(return_value=service.client)
    mock.side_effect = None
    mock.return_value = NS(id='resp_bad_parse', status='completed', usage=NS(input_tokens=30, output_tokens=20,
        total_tokens=50, input_tokens_details=NS(cached_tokens=10)),
        output=[NS(type='function_call', name='interpret_question', arguments='not valid JSON')])
    original = service.backend.call
    accounting = []

    async def call(endpoint, token, body=None):
        if endpoint == 'token-reserve':
            accounting.append((endpoint, body))
            return {'id': body['id']}
        if endpoint == 'token-settle':
            accounting.append((endpoint, body))
            return {'quota': {}}
        return await original(endpoint, token, body)

    service.backend.call = call
    service.token_meter = TokenMeter(tmp_path / 'usage.sqlite3', service.backend)
    result = client.post('/v1/chat', headers=headers(), json={'message': 'How many employees?'})
    assert result.status_code in (200, 502)
    assert accounting[0][0] == 'token-reserve'
    assert accounting[0][1]['conversation_id'] is not None
    assert accounting[1][0] == 'token-settle'
    assert accounting[1][1]['total_tokens'] == 50
    assert json.loads(rows(service.token_meter)[0][1])['state'] == 'settled'


def test_legacy_tool_loop_reserves_and_records_every_model_call(tmp_path):
    from app.auth import Identity
    from app.chat_service import ChatService
    from app.config import Settings
    meter, client, _, calls = setup(tmp_path)
    usage = NS(input_tokens=30, output_tokens=20, total_tokens=50, input_tokens_details=NS(cached_tokens=10))
    client.responses.create.side_effect = [
        NS(id='resp_first', usage=usage, output=[NS(type='function_call', name='get_employee_count',
            call_id='call_count', arguments='{"scope":"self"}')]),
        NS(id='resp_second', usage=usage, output=[], output_text='Sample employee count: 1.'),
    ]
    service = ChatService(Settings(_env_file=None, openai_model='test-model'))
    service.client = client
    service.token_meter = meter
    identity = Identity(1, 1, 'Alice', 'Employee', ('self',))
    asyncio.run(service.model_reply('How many employees?', [], identity))
    assert [row[0] for row in calls] == ['token-reserve', 'token-settle', 'token-reserve', 'token-settle']
    assert calls[0][2]['id'] != calls[2][2]['id']
    assert calls[0][2]['tokens'] == 1840 and calls[2][2]['tokens'] == 1840
    assert sum(json.loads(row[1])['total_tokens'] for row in rows(meter)) == 100
