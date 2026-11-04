import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.auth import Identity
from app.leave_balance_query import parse_question, leave_balance_reply
from tests.test_backend import backend, headers
from tests.test_semantic_query import enable, request


@pytest.mark.parametrize('question', [
    'Show me employees with the highest leave balance this year',
    'Who has the most leave remaining?',
    'List the top 10 employees by available leave.',
    'Which employees have the highest annual leave balance?',
    'Rank employees by unused leave balance',
    'Who has the largest leave balance in my team?',
    'Show the greatest remaining leave balances for 2026-27',
])
def test_paraphrases(question):
    assert parse_question(question) is not None


@pytest.mark.parametrize('question', [
    'How do I adjust leave balance?', 'Increase the highest leave balance',
    'Show approved leave today', 'Who is absent?', 'Show my working hours',
])
def test_other_features_not_intercepted(question):
    assert parse_question(question) is None


def identity(scopes=('self', 'company'), modules=('leave',)):
    return Identity(1, 101, 'Actor', 'ADMIN', scopes, credential='local-test', modules=modules)


def response():
    return dict(status='answered', data_source='effortrak_backend', scope='company',
                financial_year='2026-27', start_date='2026-04-01', end_date='2027-03-31', limit=10,
                groups=[dict(leave_type='Annual Leave', employee_count=1, unlimited_count=2,
                             rows=[dict(employee_id=102, employee_name='PRIVATE EMPLOYEE', allocated_days=20,
                                        used_days=4.5, remaining_days=15.5)])], notice='Stored balances, not snapshots.')


def test_local_report_renders_balances_without_sending_employee_records_to_llm():
    backend = SimpleNamespace(call=AsyncMock(return_value=response()))
    reply, used, artifacts = asyncio.run(leave_balance_reply('Who has the most leave remaining?', {}, identity(), backend))
    assert '15.5 days remaining' in reply
    assert 'Unlimited entitlement: 2' in reply
    assert 'PRIVATE' not in reply.model_content
    assert used == ['get_leave_balance_ranking']
    assert artifacts == []
    backend.call.assert_awaited_once_with('leave-balance', 'local-test',
                                        dict(scope='company', financial_year=None, leave_type=None, limit=10, balance_filter='positive', ranking='highest'))


@pytest.mark.parametrize('question', ['Top 0 employees by leave balance', 'Top 51 employees by remaining leave',
                                      'Highest leave balance last year', 'Highest leave balance in calendar year 2026'])
def test_unsupported_periods_and_limits_do_not_query(question):
    backend = SimpleNamespace(call=AsyncMock())
    reply = asyncio.run(leave_balance_reply(question, {}, identity(), backend))
    assert reply is not None
    backend.call.assert_not_awaited()


def test_year_type_and_top_are_preserved():
    args = parse_question('Show top 5 employees with highest sick leave balance for 2026/27')
    assert args == dict(scope=None, financial_year='2026-27', leave_type='Sick Leave', limit=5, balance_filter='positive', ranking='highest')


@pytest.mark.parametrize('question', ['Who has highest special leave balance?', 'Top five employees by leave balance'])
def test_uncertain_type_or_spelled_limit_defers_to_semantic_interpreter(question):
    assert parse_question(question) is None


def test_followup_preserves_financial_year_and_leave_type():
    data = response()
    data['limit'] = 5
    backend = SimpleNamespace(call=AsyncMock(return_value=data))
    context = dict(kind='leave_balance', args=dict(scope='company', financial_year='2026-27', leave_type='Annual Leave', limit=10))
    asyncio.run(leave_balance_reply('Top 5 instead', context, identity(), backend))
    assert backend.call.call_args.args[2] == dict(scope='company', financial_year='2026-27', leave_type='Annual Leave', limit=5, balance_filter='positive', ranking='highest')


@pytest.mark.parametrize('question,limit', [
    ('Show employees with the lowest leave balance this year', 10),
    ('Who has the least leave remaining?', 10),
    ('List the bottom 5 employees by remaining leave balance', 5),
    ('Show 5 employees with the lowest Paid Leave balance', 5),
    ('Show lowest 50 leave balances', 50),
    ('Who has the smallest available leave balance?', 10),
])
def test_lowest_paraphrases_include_zero_by_default(question, limit):
    args = parse_question(question)
    assert args['ranking'] == 'lowest'
    assert args['balance_filter'] == 'nonnegative'
    assert args['limit'] == limit


def lowest_response():
    data = response()
    data.update(ranking='lowest', balance_filter='nonnegative')
    data['groups'][0].update(employee_count=2, rows=[
        dict(employee_id=101, employee_name='Zero Balance', allocated_days=12, used_days=12, remaining_days=0),
        dict(employee_id=102, employee_name='Small Balance', allocated_days=12, used_days=11.5, remaining_days=.5),
    ])
    return data


def test_lowest_execution_and_direction_only_followup():
    backend = SimpleNamespace(call=AsyncMock(return_value=lowest_response()))
    reply, _, _ = asyncio.run(leave_balance_reply('Lowest leave balances', {}, identity(), backend))
    assert reply.startswith('Lowest remaining leave balances')
    assert 'Bottom 10 employees' in reply
    assert reply.index('Zero Balance') < reply.index('Small Balance')
    assert 'Zero and positive remaining balances only.' in reply
    backend.call.return_value = response()
    second = asyncio.run(leave_balance_reply('Highest instead', reply.query_context, identity(), backend))[0]
    assert second.startswith('Highest remaining leave balances')
    assert backend.call.call_args.args[2]['ranking'] == 'highest'
    assert backend.call.call_args.args[2]['balance_filter'] == 'positive'


def test_wrong_lowest_backend_sort_is_rejected():
    data = lowest_response()
    data['groups'][0]['rows'].reverse()
    backend = SimpleNamespace(call=AsyncMock(return_value=data))
    with pytest.raises(HTTPException) as error:
        asyncio.run(leave_balance_reply('Lowest leave balances', {}, identity(), backend))
    assert error.value.status_code == 502


def test_lowest_over_limit_does_not_call_backend():
    backend = SimpleNamespace(call=AsyncMock())
    reply, _, _ = asyncio.run(leave_balance_reply('Show bottom 51 employees by leave balance', {}, identity(), backend))
    assert 'between 1 and 50' in reply
    backend.call.assert_not_awaited()


def test_semantic_lowest_executes_same_report(backend):
    client, state = backend
    state['balance_data'] = lowest_response()
    call = enable(client, state, [request(action='leave_balance', subject='company', employee_name=None,
        start_date=None, end_date=None, period_kind='none', ranking='lowest', balance_filter='nonnegative')])
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Which staff have the smallest unused holiday allowance?'})
    assert result.status_code == 200
    assert result.json()['data']['reply'].startswith('Lowest remaining leave balances')
    assert state['last_balance_body']['ranking'] == 'lowest'
    assert call.await_count == 1


@pytest.mark.parametrize('question,expected', [
    ('Show employees with zero leave balance', 'zero'),
    ('Show employees with negative leave balances', 'negative'),
    ('Show all leave balances', 'all'),
    ('Show highest leave balance including zero', 'all'),
    ('Show highest remaining leave balances', 'positive'),
])
def test_explicit_balance_filters(question, expected):
    assert parse_question(question)['balance_filter'] == expected


@pytest.mark.parametrize('question,expected', [('Only zero', 'zero'), ('Only negative', 'negative'), ('Show all', 'all')])
def test_filter_followup_keeps_year_and_type(question, expected):
    data = response()
    data['balance_filter'] = expected
    if expected == 'zero':
        data['groups'][0]['rows'][0].update(used_days=20, remaining_days=0)
    elif expected == 'negative':
        data['groups'][0]['rows'][0].update(used_days=21, remaining_days=-1)
    backend = SimpleNamespace(call=AsyncMock(return_value=data))
    context = dict(kind='leave_balance', args=dict(scope='company', financial_year='2026-27', leave_type='Annual Leave', limit=10))
    reply, _, _ = asyncio.run(leave_balance_reply(question, context, identity(), backend))
    args = backend.call.call_args.args[2]
    assert args['balance_filter'] == expected
    assert args['financial_year'] == '2026-27'
    assert args['leave_type'] == 'Annual Leave'
    assert 'Balance filter:' in reply


def test_zero_backend_row_is_rejected_for_default_highest_request():
    data = response()
    data['groups'][0]['rows'][0].update(used_days=20, remaining_days=0)
    backend = SimpleNamespace(call=AsyncMock(return_value=data))
    with pytest.raises(HTTPException) as error:
        asyncio.run(leave_balance_reply('Highest leave balance', {}, identity(), backend))
    assert error.value.status_code == 502


@pytest.mark.parametrize('actor', [identity(scopes=('self',)), identity(modules=('attendance',))])
def test_permissions_before_read(actor):
    backend = SimpleNamespace(call=AsyncMock())
    with pytest.raises(HTTPException) as error:
        asyncio.run(leave_balance_reply('Who has the most leave remaining?', {}, actor, backend))
    assert error.value.status_code == 403
    backend.call.assert_not_awaited()


def test_backend_permission_denial_is_not_replaced_with_fabricated_answer():
    backend = SimpleNamespace(call=AsyncMock(side_effect=HTTPException(403, 'Leave balance permission is required.')))
    with pytest.raises(HTTPException):
        asyncio.run(leave_balance_reply('Who has the most leave remaining?', {}, identity(), backend))


def test_invalid_backend_arithmetic_is_rejected():
    data = response()
    data['groups'][0]['rows'][0]['remaining_days'] = 99
    backend = SimpleNamespace(call=AsyncMock(return_value=data))
    with pytest.raises(HTTPException) as error:
        asyncio.run(leave_balance_reply('Who has the most leave remaining?', {}, identity(), backend))
    assert error.value.status_code == 502


@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_shared_chat_endpoint_routes_locally_for_both_platforms(backend, platform):
    client, state = backend
    state.update(modules=['leave'], scopes=['self', 'company'], balance_data=response())
    call = enable(client, state, [])
    result = client.post('/v1/chat', headers=headers(), json={
        'message': 'Show me employees with the highest leave balance this year', 'client_platform': platform})
    assert result.status_code == 200
    assert '15.5 days remaining' in result.json()['data']['reply']
    assert result.json()['data']['tools_used'] == ['get_leave_balance_ranking']
    assert state['last_balance_body']['financial_year'] is None
    call.assert_not_awaited()


def test_semantic_paraphrase_executes_same_authorized_backend(backend):
    client, state = backend
    state['balance_data'] = response()
    call = enable(client, state, [request(action='leave_balance', subject='company', employee_name=None,
        start_date=None, end_date=None, financial_year='2026-27', leave_type='Annual Leave', period_kind='none')])
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Which staff have the biggest unused holiday allowance?'})
    assert result.status_code == 200
    assert state['last_balance_body']['financial_year'] == '2026-27'
    assert state['last_balance_body']['leave_type'] == 'Annual Leave'
    assert call.await_count == 1
    assert 'PRIVATE EMPLOYEE' not in str(call.call_args)


def test_shared_endpoint_preserves_permission_denial(backend):
    client, state = backend
    state.update(modules=['leave'], scopes=['self', 'company'], balance_denied=True)
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Who has the most leave remaining?'})
    assert result.status_code == 403
    assert 'Leave Balance permission' in result.json()['message']
