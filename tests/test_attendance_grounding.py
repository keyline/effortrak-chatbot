from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_backend import backend, headers
from tests.test_task_chat import model_turn


def test_model_cannot_replace_daily_record_with_invented_employee_or_status(backend):
    client, state = backend
    service = client.app.state.chat
    model_turn(service, 'get_daily_attendance', dict(scope='self', employee_id=None, date='2026-10-01'),
               'Joydeep (employee ID 602) was absent and did not work.')
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my attendance for 01/10/2026'})
    assert response.status_code == 200
    reply = response.json()['data']['reply']
    assert 'employee #101' in reply
    assert '602' not in reply
    assert 'Status: Present' in reply
    assert '12h 49m (769 minutes)' in reply
    assert '09:52:18' in reply
    assert '22:41:18' in reply
    assert service.client.responses.create.await_count == 1


def test_wrong_answer_challenge_rereads_same_employee_date_without_model_guess(backend):
    client, state = backend
    service = client.app.state.chat
    model_turn(service, 'get_daily_attendance', dict(scope='self', employee_id=None, date='2026-10-01'), 'Present.')
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my attendance for 01/10/2026'})
    cid = response.json()['data']['conversation_id']
    create = AsyncMock(side_effect=AssertionError('Corrections must not use the model to guess an employee.'))
    service.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=AsyncMock())
    calls = state['calls'].count('attendance')
    response = client.post('/v1/chat', headers=headers(), json={'message': 'you are saying wrong', 'conversation_id': cid})
    assert response.status_code == 200
    assert state['calls'].count('attendance') == calls + 1
    assert 'same employee and date' in response.json()['data']['reply']
    assert 'employee #101' in response.json()['data']['reply']
    assert response.json()['data']['tools_used'] == ['get_daily_attendance']
    assert create.await_count == 0


@pytest.mark.parametrize('answer', [
    'Joydeep employee ID 602 was absent.',
    'He was present and worked for 769 minutes.',
    'He was marked as Absent on that day.',
])
def test_no_tool_attendance_claims_are_not_accepted(backend, answer):
    client, state = backend
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    service.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output=[], output_text=answer))), close=AsyncMock())
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Show attendance for this employee'})
    assert result.status_code == 200
    assert 'not verified attendance' in result.json()['data']['reply']
    assert '602' not in result.json()['data']['reply']
    assert result.json()['data']['tools_used'] == []


def test_model_guessed_id_is_not_sent_to_backend(backend):
    client, state = backend
    service = client.app.state.chat
    model_turn(service, 'get_daily_attendance', dict(scope='self', employee_id=602, date='2026-10-01'), 'Employee ID 602 was absent.')
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Check punch timings on 01/10/2026'})
    assert result.status_code == 200
    assert 'unverified employee ID' in result.json()['data']['reply']
    assert 'attendance' not in state['calls']


def test_old_unverified_reply_is_not_used_as_identity_context(backend):
    client, state = backend
    response = client.post('/v1/chat', headers=headers(), json={'message': 'you are saying wrong'})
    assert response.status_code == 200
    assert 'will not guess or replace' in response.json()['data']['reply']
    assert 'attendance' not in state['calls']


def test_backend_daily_rows_for_a_different_employee_fail_validation(backend):
    client, state = backend
    service = client.app.state.chat
    import httpx
    original = client.app.state.backend.client

    def bad_response(request):
        if request.url.path.endswith('/context'):
            return original._transport.handler(request)
        return httpx.Response(200, json=dict(success=True, data=dict(data_source='effortrak_backend',
            scope='self', month='2026-10', date='2026-10-01', employee_ids=[101],
            rows=[dict(employee_id=602, employee_name='False employee', date='2026-10-01', status='Absent', worked_minutes=0)])))

    client.app.state.backend.client = httpx.AsyncClient(transport=httpx.MockTransport(bad_response))
    model_turn(service, 'get_daily_attendance', dict(scope='self', employee_id=None, date='2026-10-01'))
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my attendance for 2026-10-01'})
    assert response.status_code == 502
    assert '602' not in response.text
