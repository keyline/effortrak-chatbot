from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.employee_lookup import named_attendance, named_tasks
from tests.test_backend import backend, headers


@pytest.mark.parametrize('message', [
    'can you check joydee ghosh attendance history for 01/10/2026',
    'Show attendance for joydee ghosh on 2026-10-01',
    "What was joydee ghosh's attendance for 01/10/2026?",
])
def test_named_query_selects_one_verified_employee_locally_not_entire_company(backend, message):
    client, state = backend
    state.update(scopes=['self', 'company'], allow_company_read=True,
                 attendance_name='Joydeep Ghosh', search_matches=[{'employee_id': 101, 'name': 'Joydeep Ghosh'}])
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    create = AsyncMock(side_effect=AssertionError('Name lookup and rendering must remain local.'))
    service.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=AsyncMock())
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    data = response.json()['data']
    assert 'Employees included: 1' in data['reply']
    assert 'Joydeep Ghosh' in data['reply']
    assert 'employee #101' in data['reply']
    assert state['last_attendance_body'] == dict(scope='company', employee_id=101, date='2026-10-01', month='2026-10')
    assert data['tools_used'] == ['employee_search', 'get_daily_attendance']
    assert create.await_count == 0
    create = AsyncMock(return_value=SimpleNamespace(output=[], output_text='Hello.'))
    service.client.responses.create = create
    followup = client.post('/v1/chat', headers=headers(), json={'message': 'hi', 'conversation_id': data['conversation_id']})
    assert followup.status_code == 200
    payload = str(create.call_args_list)
    assert 'Joydeep Ghosh' not in payload
    assert '101' not in payload


@pytest.mark.parametrize('matches,has_more', [
    ([{'employee_id': 101, 'name': 'Joydeep Ghosh'}, {'employee_id': 102, 'name': 'Joydeep Ghosh'}], False),
    ([{'employee_id': 101, 'name': 'Joydeep Ghosh'}], True),
])
def test_ambiguous_and_truncated_name_matches_require_confirmation(backend, matches, has_more):
    client, state = backend
    state.update(search_matches=matches, search_has_more=has_more, scopes=['self', 'company'])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep Ghosh attendance for 01/10/2026'})
    assert response.status_code == 200
    assert 'Please provide a full name or employee ID' in response.json()['data']['reply']
    assert 'attendance' not in state['calls']


def test_no_name_match_does_not_fall_back_to_company_attendance(backend):
    client, state = backend
    state['search_matches'] = []
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep Ghosh attendance for 01/10/2026'})
    assert response.status_code == 200
    assert 'No employee matched' in response.json()['data']['reply']
    assert 'attendance' not in state['calls']


@pytest.mark.parametrize('message', ['Show my attendance for 2026-10', 'Show company attendance for 2026-10',
                                    'Show attendance for employee 101 for 2026-10', 'Show team attendance for yesterday',
                                    'Export attendance 2026-10 as pdf'])
def test_scope_and_id_requests_are_not_misinterpreted_as_names(message):
    assert named_attendance(message) is None


def test_invalid_named_query_date_does_not_search_or_read(backend):
    client, state = backend
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep Ghosh attendance for 30/02/2026'})
    assert response.status_code == 200
    assert 'valid attendance date' in response.json()['data']['reply']
    assert 'employee-search' not in state['calls']
    assert 'attendance' not in state['calls']


@pytest.mark.parametrize('scopes,scope', [(['self', 'company'], 'company'), (['self', 'team'], 'team'), (['self'], 'self')])
@pytest.mark.parametrize('message,daily', [
    ('Can you tell me Subhomoy Samanta task details for 01/10/2026', True),
    ("Show Subhomoy Samanta's tasks for 2026-10", False),
    ('Show task details for Subhomoy Samanta on 2026-10-01', True),
])
def test_named_tasks_resolve_locally_in_authorized_scope(backend, scopes, scope, message, daily):
    client, state = backend
    target = 101 if scope == 'self' else 102
    state.update(modules=['attendance', 'tasks'], scopes=scopes, task_scopes=[scope],
                 task_employee_id=target, search_matches=[{'employee_id': target, 'name': 'Subhomoy Samanta'}])
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    create = AsyncMock(side_effect=AssertionError('Named task queries must remain local.'))
    service.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=AsyncMock())
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    data = response.json()['data']
    assert 'Subhomoy Samanta' in data['reply']
    assert data['tools_used'] == ['employee_search', 'get_daily_tasks' if daily else 'get_task_summary']
    assert state['last_task_body']['scope'] == scope
    assert state['last_task_body']['employee_id'] == target
    assert all(body['module'] == 'tasks' for body in state['search_bodies'])
    assert create.await_count == 0
    if daily:
        assert state['last_task_body']['date'] == '2026-10-01'
        assert 'PRIVATE_TASK_DESCRIPTION' in data['reply']
        followup = client.post('/v1/chat', headers=headers(), json={
            'message': 'Show tasks for this employee on that day', 'conversation_id': data['conversation_id']})
        assert followup.status_code == 200
        assert state['last_task_body']['employee_id'] == (None if scope == 'self' else target)
        assert create.await_count == 0
    service.client.responses.create = AsyncMock(return_value=SimpleNamespace(output=[], output_text='Hello.'))
    response = client.post('/v1/chat', headers=headers(), json={'message': 'hi', 'conversation_id': data['conversation_id']})
    assert response.status_code == 200
    payload = str(service.client.responses.create.call_args_list)
    assert 'Subhomoy Samanta' not in payload
    assert 'PRIVATE_TASK_DESCRIPTION' not in payload
    assert 'PRIVATE_TASK_NAME' not in payload
    assert str(target) not in payload


@pytest.mark.parametrize('matches', [[], [{'employee_id': 101, 'name': 'Subhomoy Samanta'},
                                       {'employee_id': 102, 'name': 'Subhomoy Samanta'}]])
def test_named_task_missing_or_ambiguous_employee_never_reads_all_tasks(backend, matches):
    client, state = backend
    state.update(modules=['tasks'], scopes=['self', 'company'], search_matches=matches)
    response = client.post('/v1/chat', headers=headers(), json={
        'message': 'Can you tell me Subhomoy Samanta task details for 01/10/2026'})
    assert response.status_code == 200
    assert 'daily-tasks' not in state['calls']
    assert 'task-summary' not in state['calls']


def test_named_task_permission_denial_does_not_return_details(backend):
    client, state = backend
    state.update(modules=['tasks'], search_matches=[{'employee_id': 101, 'name': 'Subhomoy Samanta'}], tasks_denied=True)
    response = client.post('/v1/chat', headers=headers(), json={
        'message': 'Show Subhomoy Samanta tasks for 01/10/2026'})
    assert response.status_code == 403
    assert 'PRIVATE_TASK_DESCRIPTION' not in response.text


@pytest.mark.parametrize('message', ['Show my tasks for yesterday', 'Show team task details for 2026-10-01',
                                    'Show tasks for employee 101 on 2026-10-01'])
def test_task_scope_and_id_queries_are_not_names(message):
    assert named_tasks(message) is None


@pytest.mark.parametrize('period', ['01/10/2026', '2026-10-01', '2026-10'])
def test_named_task_clarification_retains_one_employee(backend, period):
    client, state = backend
    state.update(modules=['tasks'], scopes=['self', 'company'], task_scopes=['company'], task_employee_id=102,
                 search_matches=[{'employee_id': 102, 'name': 'Subhomoy Samanta'}])
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    create = AsyncMock(side_effect=AssertionError('Clarification must retain employee without OpenAI.'))
    service.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=AsyncMock())
    first = client.post('/v1/chat', headers=headers(), json={
        'message': 'Can you give me Subhomoy Samanta Task history for last 7 days'})
    assert first.status_code == 200
    assert 'Selected Subhomoy Samanta' in first.json()['data']['reply']
    assert 'daily-tasks' not in state['calls']
    response = client.post('/v1/chat', headers=headers(), json={
        'message': period, 'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200
    assert state['last_task_body']['employee_id'] == 102
    assert state['last_task_body']['scope'] == 'company'
    assert create.await_count == 0
    if period != '2026-10':
        second = client.post('/v1/chat', headers=headers(), json={
            'message': '02/10/2026', 'conversation_id': first.json()['data']['conversation_id']})
        assert second.status_code == 200
        assert state['last_task_body']['employee_id'] == 102
        assert state['last_task_body']['date'] == '2026-10-02'


@pytest.mark.parametrize('matches', [[], [{'employee_id': 101, 'name': 'Subhomoy Samanta'},
                                      {'employee_id': 102, 'name': 'Subhomoy Samanta'}]])
def test_date_reply_does_not_expand_unresolved_employee_selection(backend, matches):
    client, state = backend
    state.update(modules=['tasks'], scopes=['self', 'company'], search_matches=matches)
    first = client.post('/v1/chat', headers=headers(), json={
        'message': 'Can you give me Subhomoy Samanta Task history for last 7 days'})
    response = client.post('/v1/chat', headers=headers(), json={
        'message': '01/10/2026', 'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200
    assert 'daily-tasks' not in state['calls']
    assert 'task-summary' not in state['calls']


def test_invalid_date_keeps_pending_employee_for_retry(backend):
    client, state = backend
    state.update(modules=['tasks'], scopes=['self', 'company'], task_scopes=['company'], task_employee_id=102,
                 search_matches=[{'employee_id': 102, 'name': 'Subhomoy Samanta'}])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show Subhomoy Samanta task history'})
    cid = first.json()['data']['conversation_id']
    response = client.post('/v1/chat', headers=headers(), json={'message': '30/02/2026', 'conversation_id': cid})
    assert response.status_code == 200
    assert 'daily-tasks' not in state['calls']
    response = client.post('/v1/chat', headers=headers(), json={'message': '01/10/2026', 'conversation_id': cid})
    assert response.status_code == 200
    assert state['last_task_body']['employee_id'] == 102
