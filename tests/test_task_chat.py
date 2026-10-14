import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tools import definitions
from app.task_report import AssistantReply
from tests.test_backend import backend, headers


def model_turn(service, name, arguments, answer='Task details are unavailable.'):
    service.settings.chat_mode = 'openai'
    service.settings.openai_model = 'isolated-test-model'
    call = SimpleNamespace(type='function_call', name=name, call_id='one', arguments=json.dumps(arguments))
    create = AsyncMock(side_effect=[SimpleNamespace(output=[call], output_text=''),
                                   SimpleNamespace(output=[], output_text=answer)])
    service.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=AsyncMock())
    return create


@pytest.mark.parametrize('booked,label', [(95, '1h 35m'), (0, '0h 0m'), (None, 'Not available')])
def test_daily_tasks_show_booked_time_separately_from_planned(backend, booked, label):
    client, state = backend
    state.update(modules=['tasks'], booked_minutes=booked)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my tasks for 01/10/2026'})
    assert response.status_code == 200
    reply = response.json()['data']['reply']
    assert 'Planned: 1h 15m' in reply
    assert f'Booked: {label}' in reply
    assert ('Total booked time' in reply) == (booked is not None)


@pytest.mark.parametrize('field', ['booked_minutes', 'row_booked_minutes'])
@pytest.mark.parametrize('value', [-1, True, '95'])
def test_invalid_booked_minutes_are_rejected(backend, field, value):
    client, state = backend
    state.update(modules=['tasks'], **{field: value})
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my tasks for 01/10/2026'})
    assert response.status_code == 502


def test_daily_task_details_are_local_and_never_enter_model_history(backend):
    client, state = backend
    state['modules'] = ['attendance', 'tasks']
    service = client.app.state.chat
    create = model_turn(service, 'get_daily_tasks', dict(scope='self', employee_id=None, date='2026-10-01', page=1))
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my tasks for 01/10/2026'})
    assert response.status_code == 200
    data = response.json()['data']
    assert 'PRIVATE_TASK_DESCRIPTION' in data['reply']
    assert '1h 15m' in data['reply']
    assert data['tools_used'] == ['get_daily_tasks']
    for value in ('PRIVATE_TASK_DESCRIPTION', 'PRIVATE_TASK_NAME', 'PRIVATE_STATUS_NAME'):
        assert value not in str(create.call_args_list)
    history = client.get('/v1/conversations/' + data['conversation_id'], headers=headers()).json()['data']['messages']
    assert 'PRIVATE_TASK_DESCRIPTION' in history[-1]['content']
    assert 'model_content' not in history[-1]
    create = model_turn(service, 'get_employee_count', dict(scope='self'), 'Employees: 1.')
    followup = client.post('/v1/chat', headers=headers(), json={'message': 'How many employees?', 'conversation_id': data['conversation_id']})
    assert followup.status_code == 200
    for value in ('PRIVATE_TASK_DESCRIPTION', 'PRIVATE_TASK_NAME', 'PRIVATE_STATUS_NAME'):
        assert value not in str(create.call_args_list)


def test_attendance_to_task_followup_uses_trusted_employee_date_and_self_scope(backend):
    client, state = backend
    state['modules'] = ['attendance', 'tasks']
    service = client.app.state.chat
    model_turn(service, 'get_daily_attendance', dict(scope='self', employee_id=None, date='2026-10-01'), 'Attendance loaded.')
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my attendance for 01/10/2026'})
    assert response.status_code == 200
    cid = response.json()['data']['conversation_id']
    model_turn(service, 'get_daily_tasks', dict(scope='company', employee_id=201, date='2026-09-01', page=1))
    followup = client.post('/v1/chat', headers=headers(), json={
        'message': 'Can you give me task details of this employee for that day?', 'conversation_id': cid})
    assert followup.status_code == 200
    assert state['last_task_body'] == dict(scope='self', employee_id=None, date='2026-10-01', page=1)
    assert 'PRIVATE_TASK_DESCRIPTION' in followup.json()['data']['reply']
    model_turn(service, 'get_daily_tasks', dict(scope='company', employee_id=201, date='2026-09-01', page=1))
    page = client.post('/v1/chat', headers=headers(), json={'message': 'Next page', 'conversation_id': cid})
    assert page.status_code == 200
    assert state['last_task_body'] == dict(scope='self', employee_id=101, date='2026-10-01', page=2)


def test_monthly_tasks_work_in_demo_and_openai_modes(backend):
    client, state = backend
    state['modules'] = ['tasks']
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Show my tasks for 2026-10'})
    assert result.status_code == 200
    assert 'Tasks: 2' in result.json()['data']['reply']
    assert '1h 35m' in result.json()['data']['reply']
    service = client.app.state.chat
    model_turn(service, 'get_task_summary', dict(scope='self', employee_id=None, month='2026-10'))
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Show my task summary for October 2026'})
    assert result.status_code == 200
    assert result.json()['data']['tools_used'] == ['get_task_summary']
    assert 'Task summary for 2026-10' in result.json()['data']['reply']


def test_ambiguous_employee_followup_does_not_guess_or_read_tasks(backend):
    client, state = backend
    state['modules'] = ['tasks']
    identity = client.app.state.backend
    import asyncio
    actor = asyncio.run(identity.identity(headers()['Authorization'][7:], 1, 101))
    store = client.app.state.store
    cid = store.create(actor)
    store.save_turn(cid, actor, 'Two employees', AssistantReply('Two employee results.',
                    query_context=dict(scope='team', employee_ids=[101, 102], date='2026-10-01')), [])
    service = client.app.state.chat
    model_turn(service, 'get_daily_tasks', dict(scope='self', employee_id=None, date='2026-10-01', page=1),
               'Which employee do you mean?')
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Tasks for this employee for that day', 'conversation_id': cid})
    assert response.status_code == 200
    assert 'Which employee' in response.json()['data']['reply']
    assert 'daily-tasks' not in state['calls']


@pytest.mark.parametrize('flag,status', [('tasks_denied', 403), ('revoke_during_task', 401), ('bad_task_response', 502)])
def test_task_permission_revocation_and_invalid_backend_rows_fail_closed(backend, flag, status):
    client, state = backend
    state['modules'] = ['tasks']
    state[flag] = True
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my tasks for 2026-10-01'})
    assert response.status_code == status
    assert 'PRIVATE_TASK_DESCRIPTION' not in response.text


def test_missing_task_module_hides_tools_and_empty_tasks_are_not_a_permission_error(backend):
    client, state = backend
    import asyncio
    identity = asyncio.run(client.app.state.backend.identity(headers()['Authorization'][7:], 1, 101))
    assert 'get_daily_tasks' not in [item['name'] for item in definitions(identity)]
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my tasks for 2026-10-01'})
    assert 'TASK ASSIGN' in response.json()['data']['reply']
    assert 'daily-tasks' not in state['calls']
    state['modules'] = ['tasks']
    state['empty_tasks'] = True
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my tasks for 2026-10-01'})
    assert response.status_code == 200
    assert 'No tasks were found' in response.json()['data']['reply']


def test_task_formatter_distinguishes_empty_page_from_no_tasks():
    from app.task_report import task_result
    result = task_result(dict(scope='self', date='2026-10-01', employee_ids=[101],
                              task_count=2, planned_minutes=90, page=2, page_size=50, has_more=False, rows=[]), daily=True)
    assert 'No tasks are on this page' in result.local_reply
    assert 'No tasks were found' not in result.local_reply
