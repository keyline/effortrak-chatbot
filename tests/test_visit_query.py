import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.auth import Identity
from app.visit_query import parse_question, visit_reply
from tests.test_backend import backend, headers


@pytest.mark.parametrize('question,employee,client,latest', [
    ('Joydeep Ghosh last visited client?', 'Joydeep Ghosh', None, True),
    ("Today's Joydeep Ghosh Visit list?", 'Joydeep Ghosh', None, False),
    ('abc Client visited employee list along with date, time', None, 'abc', False),
    ('ABC Client Joydeep Ghosh Visited list', 'Joydeep Ghosh', 'ABC', False),
    ('ABC Client visited by Joydeep Ghosh last 7 days', 'Joydeep Ghosh', 'ABC', False),
    ('List employees who visited ABC Client today', None, 'ABC', False),
    ('Show all visits by Joydeep Ghosh to ABC Client last 30 days', 'Joydeep Ghosh', 'ABC', False),
    ('Who visited ABC Client in last 7 days?', None, 'ABC', False),
    ('Who visited client ABC in the past 10 days?', None, 'ABC', False),
    ('Who went to ABC yesterday?', None, 'ABC', False),
    ('Which employees have visited ABC Client in the past 30 days?', None, 'ABC', False),
    ('Which client did Joydeep Ghosh last visit?', 'Joydeep Ghosh', None, True),
    ('Which client has Joydeep Ghosh visited most recently?', 'Joydeep Ghosh', None, True),
    ("Show Joydeep Ghosh's visits to ABC Client this week", 'Joydeep Ghosh', 'ABC', False),
    ("Show Joydeep Ghosh's latest client visit", 'Joydeep Ghosh', None, True),
    ('Show Joydeep Ghosh visits from 01/10/2026 to 15/10/2026', 'Joydeep Ghosh', None, False),
    ('Visitors to ABC Client today', None, 'ABC', False),
    ('Joydeep Ghosh last visited ABC Client', 'Joydeep Ghosh', 'ABC', True),
    ('Where is Joydeep Ghosh now?', 'Joydeep Ghosh', None, True),
    ('Show Joydeep Ghosh current location', 'Joydeep Ghosh', None, True),
    ('Can you tell me Joydeep Ghosh 06/10/2026 current location', 'Joydeep Ghosh', None, True),
])
def test_wordings(question, employee, client, latest):
    parsed = parse_question(question)
    assert parsed['employee_name'] == employee
    assert parsed['client_name'] == client
    assert parsed['latest'] == latest


@pytest.mark.parametrize('days', ['0', '94', '10000', 'x'])
def test_invalid_day_counts_require_clarification(days):
    assert parse_question(f'Who visited ABC Client in last {days} days?')['error']


@pytest.mark.parametrize('days', [1, 7, 30, 93])
def test_day_ranges_are_inclusive(days):
    from datetime import date
    parsed = parse_question(f'Who visited ABC Client in last {days} days?')
    assert (date.fromisoformat(parsed['dates'][1]) - date.fromisoformat(parsed['dates'][0])).days == days - 1


@pytest.mark.parametrize('question', ['Show my attendance today', 'How do I add a client visit?',
                                      'Delete ABC client', 'Show working hours for Joydeep'])
def test_other_features_not_intercepted(question):
    assert parse_question(question) is None


def identity():
    return Identity(1, 101, 'Actor', 'ADMIN', ('self', 'company'), credential='local-test')


def response(action='visits', more=False):
    return dict(status='answered', action=action, start_date='2026-10-01', end_date='2026-10-31',
                timezone='Asia/Kolkata', as_of='2026-11-03 12:00:00', has_more=more, page=1,
                rows=[dict(employee_name='PRIVATE EMPLOYEE', client_name='PRIVATE CLIENT', recorded_at='2026-10-06 10:00:00',
                           location=dict(address='PRIVATE ADDRESS', coordinates=[22.5, 88.3], maps_url='https://www.google.com/maps/search/?api=1&query=22.5%2C88.3'))])


def test_visit_is_local_and_contains_facts_not_completion_claims():
    backend = SimpleNamespace(call=AsyncMock(return_value=response()))
    reply, used, _ = asyncio.run(visit_reply('Joydeep Ghosh last visited client?', {}, identity(), backend))
    assert backend.call.call_args.args[:2] == ('visit-location', 'local-test')
    assert 'PRIVATE ADDRESS' in reply
    assert 'Check-out time is not stored' in reply
    assert 'PRIVATE' not in reply.model_content
    assert used == ['get_visit_location']


def test_period_clarification_then_pagination_retains_selection():
    backend = SimpleNamespace(call=AsyncMock(return_value=response(more=True)))
    first = asyncio.run(visit_reply('ABC Client Joydeep Ghosh Visited list', {}, identity(), backend))[0]
    assert first.query_context['pending'] == 'period'
    backend.call.assert_not_awaited()
    second = asyncio.run(visit_reply('last 7 days', first.query_context, identity(), backend))[0]
    args = backend.call.call_args.args[2]
    assert args['client_name'] == 'ABC' and args['employee_name'] == 'Joydeep Ghosh'
    asyncio.run(visit_reply('next page', second.query_context, identity(), backend))
    assert backend.call.call_args.args[2]['page'] == 2


def test_ambiguous_employee_never_guesses_id():
    backend = SimpleNamespace(call=AsyncMock(return_value=dict(status='clarify', subject='employee', rows=[])))
    first = asyncio.run(visit_reply('Show Joydeep visits today', {}, identity(), backend))[0]
    assert first.query_context['pending'] == 'employee'
    backend.call.return_value = response()
    asyncio.run(visit_reply('Joydeep Ghosh', first.query_context, identity(), backend))
    assert backend.call.call_args.args[2]['employee_name'] == 'Joydeep Ghosh'
    assert backend.call.call_args.args[2]['employee_id'] is None


def test_invalid_days_recover_with_names_preserved():
    backend = SimpleNamespace(call=AsyncMock(return_value=response()))
    first = asyncio.run(visit_reply('Who visited ABC Client in last x days?', {}, identity(), backend))[0]
    backend.call.assert_not_awaited()
    asyncio.run(visit_reply('7', first.query_context, identity(), backend))
    assert backend.call.call_args.args[2]['client_name'] == 'ABC'


def test_latest_location_labels_stale_and_mock_data():
    data = response('location')
    data['rows'][0].update(is_mock=True, accuracy_m=25)
    backend = SimpleNamespace(call=AsyncMock(return_value=data))
    reply = asyncio.run(visit_reply('Where is Joydeep Ghosh now?', {}, identity(), backend))[0]
    assert 'not a live device request' in reply
    assert 'mock GPS' in reply
    assert '2026-10-06' in reply


def test_lms_schedule_is_not_claimed_to_be_a_visit():
    backend = SimpleNamespace(call=AsyncMock())
    result = asyncio.run(visit_reply('Show LMS visits today', {}, identity(), backend))
    assert 'not GPS-confirmed visits' in result[0]
    backend.call.assert_not_awaited()


def test_lms_saved_location():
    parsed = parse_question('Show LMS lead ABC location')
    assert parsed['action'] == 'lms_location' and parsed['client_name'] == 'ABC'


def test_other_question_can_interrupt_clarification():
    backend = SimpleNamespace(call=AsyncMock())
    result = asyncio.run(visit_reply('Show my attendance today', {'kind': 'visit_location', 'pending': 'employee'}, identity(), backend))
    assert result is None
    backend.call.assert_not_awaited()


def test_personal_followup_does_not_expand_to_company():
    backend = SimpleNamespace(call=AsyncMock(return_value=response(more=True)))
    first = asyncio.run(visit_reply('Show my visits today', {}, identity(), backend))[0]
    asyncio.run(visit_reply('next page', first.query_context, identity(), backend))
    assert backend.call.call_args.args[2]['scope'] == 'self'


@pytest.mark.parametrize('platform', ['web', 'android', 'ios'])
def test_shared_chat_endpoint_avoids_model_calls_and_redacts_history(backend, platform):
    client, state = backend
    state.update(scopes=['self', 'company'], modules=['attendance', 'visits', 'location'])
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    service.settings.semantic_questions = True
    service.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=AssertionError('Unexpected LLM call'))), close=AsyncMock())
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep Ghosh last visited client?', 'client_platform': platform})
    assert result.status_code == 200, result.text
    assert 'PRIVATE ADDRESS' in result.json()['data']['reply']
    assert state['last_visit_body']['employee_name'] == 'Joydeep Ghosh'
    assert service.client.responses.create.await_count == 0
    cid = result.json()['data']['conversation_id']
    owner = Identity(1, 101, 'Actor', 'ADMIN', ('self', 'company'), data_source='effortrak_backend',
                     policy_version=state['policy'], modules=tuple(state['modules']))
    history = client.app.state.store.history(cid, owner)
    assert 'Joydeep' not in history[0]['model_content']
    assert 'PRIVATE' not in history[1]['model_content']


def test_visit_permission_denial_is_clear(backend):
    client, state = backend
    state.update(visit_denied=True)
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Show my visits today'})
    assert result.status_code == 403
    assert 'subscription and role' in result.json()['message']


def test_later_model_calls_do_not_receive_local_visit_selection(backend):
    client, state = backend
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    service.settings.semantic_questions = False
    create = AsyncMock(return_value=SimpleNamespace(output=[], output_text='Hello.'))
    service.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=AsyncMock())
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep Ghosh last visited ABC Client'})
    assert first.status_code == 200, first.text
    second = client.post('/v1/chat', headers=headers(), json={'message': 'hello', 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200, second.text
    payload = str(create.call_args_list)
    assert 'Joydeep' not in payload and 'PRIVATE' not in payload and 'ABC' not in payload
