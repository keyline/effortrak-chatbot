from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_backend import backend, headers


def local_only(client, state):
    state.update(modules=['attendance', 'tasks', 'leave'], scopes=['self', 'company'], task_scopes=['company'])
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    service.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=AssertionError('Manager data must stay local.'))), close=AsyncMock())


@pytest.mark.parametrize('message,report', [
    ('Who was late in my company for 2026-09?', 'late'),
    ('Who was present yesterday?', 'present'),
    ('Who was absent yesterday?', 'absent'),
    ('Show missing punch-outs for 2026-09', 'missing_punchout'),
    ('Show my permitted work arrangements', 'work_arrangements'),
    ('Where did I mark attendance yesterday?', 'locations'),
    ('Show attendance with missing GPS yesterday', 'missing_location'),
    ('Show planned versus booked time for 2026-09', 'effort_by_employee'),
    ('Compare planned and booked time across projects for 2026-09', 'effort_by_project'),
    ('Which tasks have booked time greater than planned for 2026-09?', 'task_overruns'),
    ('Who has the highest planned workload this week?', 'workload'),
    ('Who is on approved leave tomorrow?', 'approved_leave'),
    ('Compare company attendance with last month', 'attendance_totals'),
])
def test_manager_queries_use_verified_local_tools(backend, message, report):
    client, state = backend
    local_only(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert state['last_manager_body']['report'] == report
    assert 'get_manager_report' in response.json()['data']['tools_used']
    if 'did I' in message or 'my permitted' in message:
        assert state['last_manager_body']['scope'] == 'self'
    assert client.app.state.chat.client.responses.create.await_count == 0


def test_missing_date_and_employee_selection_retained(backend):
    client, state = backend
    local_only(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show missing punch-outs for employee ID 101'})
    assert first.status_code == 200
    assert 'manager-report' not in state['calls']
    second = client.post('/v1/chat', headers=headers(), json={'message': '2026-09', 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['report'] == 'missing_punchout'


def test_location_permission_denial_is_clear(backend):
    client, state = backend
    local_only(client, state)
    state['location_denied'] = True
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Where did I mark attendance yesterday?'})
    assert response.status_code == 403
    assert 'separate attendance-location permission' in response.json()['message']


def test_location_and_employee_names_never_reach_model_history(backend):
    client, state = backend
    local_only(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Where did I mark attendance yesterday?'})
    assert 'PRIVATE_CAPTURE_ADDRESS' in first.json()['data']['reply']
    create = AsyncMock(return_value=SimpleNamespace(output=[], output_text='Hello.'))
    client.app.state.chat.client.responses.create = create
    second = client.post('/v1/chat', headers=headers(), json={'message': 'hi', 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    payload = str(create.call_args_list)
    assert 'PRIVATE_CAPTURE_ADDRESS' not in payload
    assert 'PRIVATE_MANAGER_NAME' not in payload
    assert '22.5' not in payload


@pytest.mark.parametrize('message', [
    'Who is working from home today?', 'Was WFH approved yesterday?',
    'Who has capacity for another task?', 'Which tasks are overdue?',
    'Export tasks as PDF', 'Export attendance with locations as PDF',
    'Approve this leave request', 'Send me a weekly team summary automatically',
    'Which WFO punch-ins were outside the office boundary?',
])
def test_unavailable_capabilities_never_guess_or_execute(backend, message):
    client, state = backend
    local_only(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert response.json()['data']['tools_used'] == []
    assert response.json()['data']['artifacts'] == []
    assert 'manager-report' not in state['calls']


def test_foreign_manager_response_is_rejected(backend):
    client, state = backend
    local_only(client, state)
    state['bad_manager_employee'] = True
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Who was late yesterday?'})
    assert response.status_code == 502


def test_explicit_team_scope_is_not_replaced_with_company(backend):
    client, state = backend
    local_only(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my team workload yesterday'})
    assert response.status_code == 403
    assert 'manager-report' not in state['calls']


def test_my_company_means_company_not_personal(backend):
    client, state = backend
    local_only(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Who was late in my company yesterday?'})
    assert response.status_code == 200
    assert state['last_manager_body']['scope'] == 'company'


def test_workload_hours_are_converted_to_minutes(backend):
    client, state = backend
    local_only(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Which employees have more than 10 planned hours tomorrow?'})
    assert response.status_code == 200
    assert state['last_manager_body']['report'] == 'workload'
    assert state['last_manager_body']['threshold'] == 600


def test_daily_wfh_is_not_inferred_without_an_explicit_date(backend):
    client, state = backend
    local_only(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Who is working from home?'})
    assert response.status_code == 200
    assert 'Daily WFH/WFO' in response.json()['data']['reply']
    assert 'manager-report' not in state['calls']


def test_unsupported_location_query_is_also_redacted_from_history(backend):
    client, state = backend
    local_only(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Was PRIVATE_PERSON outside the office boundary at latitude 22.555 yesterday?'})
    assert first.status_code == 200
    create = AsyncMock(return_value=SimpleNamespace(output=[], output_text='Hello.'))
    client.app.state.chat.client.responses.create = create
    response = client.post('/v1/chat', headers=headers(), json={'message': 'hi', 'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200
    assert 'PRIVATE_PERSON' not in str(create.call_args_list)
    assert '22.555' not in str(create.call_args_list)


@pytest.mark.parametrize('message', [
    'Tell me Subhomoy Samanta (Employee) last month total booking hours?',
    'Tell me Subhomoy Samanta (Employee) last month total effort booking hours?',
    'Show Subhomoy Samanta booked hours last month',
    "Subhomoy Samanta's logged time for 2026-09",
    'Total booking hours for Subhomoy Samanta last month',
    'How many hours did Subhomoy Samanta book last month?',
])
def test_named_booking_questions_use_timesheet_totals_not_attendance_or_headcount(backend, message):
    client, state = backend
    local_only(client, state)
    state['search_matches'] = [{'employee_id': 101, 'name': 'Subhomoy Samanta'}]
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    body = state['last_manager_body']
    assert body['report'] == 'booked_time'
    assert body['employee_id'] == 101
    assert '1h 30m booked' in response.json()['data']['reply']
    assert 'employee-count' not in state['calls']
    assert 'attendance' not in state['calls']
    assert client.app.state.chat.client.responses.create.await_count == 0


@pytest.mark.parametrize('message', ['Show my booking hours last month', 'How many hours did I book last month?'])
def test_personal_bookings_do_not_request_employee_name_or_expand_scope(backend, message):
    client, state = backend
    local_only(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert state['last_manager_body']['scope'] == 'self'
    assert state['last_manager_body']['employee_id'] is None


def test_booked_name_clarification_retains_last_month(backend):
    from app.manager_query import report_period
    client, state = backend
    local_only(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show total booking hours last month'})
    assert 'Whose booked hours' in first.json()['data']['reply']
    assert 'manager-report' not in state['calls']
    state['search_matches'] = [{'employee_id': 101, 'name': 'Subhomoy Samanta'}]
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Subhomoy Samanta', 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_manager_body']['start_date'] == report_period('last month')[0]
    assert state['last_manager_body']['employee_id'] == 101


def test_timesheet_permission_denial_is_explicit(backend):
    client, state = backend
    local_only(client, state)
    state['effort_denied'] = True
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my booking hours last month'})
    assert response.status_code == 403
    assert 'Effort Report permission' in response.json()['message']


def test_unrecognized_booking_wording_cannot_receive_headcount(backend):
    from tests.test_task_chat import model_turn
    client, state = backend
    local_only(client, state)
    model_turn(client.app.state.chat, 'get_employee_count', dict(scope='self'), 'Active employees: 27.')
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Explain this booking'})
    assert response.status_code == 200
    assert 'not employee headcount' in response.json()['data']['reply']
    assert 'employee-count' not in state['calls']


def test_booked_report_includes_billable_and_nonbillable(backend):
    client, state = backend
    local_only(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my booked hours last month'})
    assert response.status_code == 200
    reply = response.json()['data']['reply']
    assert 'Billable: 1h 0m' in reply
    assert 'Non-billable: 0h 30m' in reply
    assert 'Unclassified billing' not in reply


def test_billing_followup_retains_employee_and_period(backend):
    client, state = backend
    local_only(client, state)
    state['search_matches'] = [{'employee_id': 101, 'name': 'Subhomoy Samanta'}]
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show Subhomoy Samanta booked hours for 2026-09'})
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Also show billable and non-billable', 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['start_date'] == '2026-09-01'
    assert 'Billable: 1h 0m' in second.json()['data']['reply']


@pytest.mark.parametrize('message', ['Tell me Subhomoy Samanta last month billable and non-billable hours',
                                   'Billable hours for Subhomoy Samanta last month'])
def test_named_billing_wordings_keep_employee_filter(backend, message):
    client, state = backend
    local_only(client, state)
    state['search_matches'] = [{'employee_id': 101, 'name': 'Subhomoy Samanta'}]
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert state['last_manager_body']['employee_id'] == 101
    assert 'Non-billable: 0h 30m' in response.json()['data']['reply']


def test_invalid_billing_totals_are_rejected(backend):
    client, state = backend
    local_only(client, state)
    state['manager_rows'] = [{'employee_id': 101, 'employee_name': 'Local', 'booked_minutes': 90,
        'entry_count': 2, 'unavailable_count': 0, 'billing': {
            'billable': {'minutes': 100, 'entry_count': 1, 'unavailable_count': 0},
            'non_billable': {'minutes': 30, 'entry_count': 1, 'unavailable_count': 0},
            'unclassified': {'minutes': 0, 'entry_count': 0, 'unavailable_count': 0}}}]
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my booking hours last month'})
    assert response.status_code == 502


@pytest.mark.parametrize('message', [
    'Can you tell me how days Joydeep Ghosh late for last month',
    'How many days was Joydeep Ghosh late last month?',
    'How often was Joydeep Ghosh late last month?',
    'Can you tell me how days Joydeep Ghosh attendance late for last month',
    'Show Joydeep Ghosh late days for 2026-09',
    'Late days for Joydeep Ghosh last month',
    'How many late days did Joydeep Ghosh have last month?',
])
def test_named_late_counts_never_expand_to_company_daily_attendance(backend, message):
    from app.manager_query import report_period
    client, state = backend
    local_only(client, state)
    state['search_matches'] = [{'employee_id': 101, 'name': 'Joydeep Ghosh'}]
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert state['last_manager_body']['report'] == 'late'
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['start_date'] == ('2026-09-01' if '2026-09' in message else report_period('last month')[0])
    assert '1 late day' in response.json()['data']['reply']
    assert 'attendance' not in state['calls']
    assert 'employee-count' not in state['calls']


def test_named_zero_late_days_are_reported_as_zero_not_missing_data(backend):
    client, state = backend
    local_only(client, state)
    state['search_matches'] = [{'employee_id': 101, 'name': 'Joydeep Ghosh'}]
    state['manager_rows'] = [{'employee_id': 101, 'employee_name': 'Joydeep Ghosh', 'count': 0, 'dates': []}]
    response = client.post('/v1/chat', headers=headers(), json={'message': 'How many days was Joydeep Ghosh late last month?'})
    assert response.status_code == 200
    assert 'Joydeep Ghosh: 0 late days.' in response.json()['data']['reply']


def test_ambiguous_late_employee_does_not_read_company_counts(backend):
    client, state = backend
    local_only(client, state)
    state['search_matches'] = [{'employee_id': 101, 'name': 'Joydeep Ghosh'}, {'employee_id': 102, 'name': 'Joydeep Ghosh'}]
    first = client.post('/v1/chat', headers=headers(), json={'message': 'How many days was Joydeep Ghosh late last month?'})
    assert first.status_code == 200
    assert 'manager-report' not in state['calls']
    state['search_matches'] = [{'employee_id': 101, 'name': 'Joydeep Ghosh'}]
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep Ghosh', 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['report'] == 'late'


def test_missing_subject_late_count_requires_clarification(backend):
    client, state = backend
    local_only(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'How many late days last month?'})
    assert response.status_code == 200
    assert 'Which employee' in response.json()['data']['reply']
    assert 'manager-report' not in state['calls']


def test_late_count_clarification_accepts_employee_id_without_losing_month(backend):
    client, state = backend
    local_only(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'How many late days last month?'})
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Employee ID 101', 'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['report'] == 'late'


def test_switching_selected_employee_to_late_count_updates_followup_report(backend):
    client, state = backend
    local_only(client, state)
    state['search_matches'] = [{'employee_id': 101, 'name': 'Joydeep Ghosh'}]
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep Ghosh booking hours last month'})
    cid = first.json()['data']['conversation_id']
    second = client.post('/v1/chat', headers=headers(), json={'message': 'How many days was this employee late last month?', 'conversation_id': cid})
    assert second.status_code == 200
    third = client.post('/v1/chat', headers=headers(), json={'message': '2026-09', 'conversation_id': cid})
    assert third.status_code == 200
    assert state['last_manager_body']['report'] == 'late'
    assert state['last_manager_body']['employee_id'] == 101


@pytest.mark.parametrize('message', [
    'How many days did Joydeep Ghosh punch in late last month?',
    'Show late punch-ins for Joydeep Ghosh last month',
])
def test_named_late_punchins_use_shift_report(backend, message):
    client, state = backend
    local_only(client, state)
    state['search_matches'] = [{'employee_id': 101, 'name': 'Joydeep Ghosh'}]
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert state['last_manager_body']['report'] == 'late_punchin'
    assert state['last_manager_body']['employee_id'] == 101
    assert '7 minutes after the cutoff' in response.json()['data']['reply']


def test_punchin_followup_keeps_employee_and_month(backend):
    from app.manager_query import report_period
    client, state = backend
    local_only(client, state)
    state['search_matches'] = [{'employee_id': 101, 'name': 'Joydeep Ghosh'}]
    first = client.post('/v1/chat', headers=headers(), json={'message': 'How many days was Joydeep Ghosh late last month?'})
    second = client.post('/v1/chat', headers=headers(), json={'message': 'I want to know about punch in late',
                         'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_manager_body']['report'] == 'late_punchin'
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['start_date'] == report_period('last month')[0]


def test_missing_punchin_policy_is_not_zero_late_days(backend):
    client, state = backend
    local_only(client, state)
    state['manager_rows'] = [{'employee_id': 101, 'employee_name': 'Employee A', 'assessable': False,
                              'late_after': None, 'count': 0, 'entries': []}]
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my late punch-ins last month'})
    assert response.status_code == 200
    assert 'no late cutoff is configured' in response.json()['data']['reply']
