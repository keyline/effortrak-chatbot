import json

import pytest

from app.task_report import trusted_query
from tests.test_backend import backend, headers
from tests.test_semantic_query import enable, request


def send(client, message, conversation_id=None, platform='web'):
    response = client.post('/v1/chat', headers=headers(), json={
        'message': message, 'conversation_id': conversation_id, 'client_platform': platform})
    assert response.status_code == 200, response.text
    return response.json()['data']


@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_employee_period_and_page_clarifications_complete_one_task_request(backend, platform):
    client, state = backend
    create = enable(client, state, [request(action='tasks', subject='unspecified', employee_name=None,
        start_date=None, end_date=None, period_kind='none')])
    state['task_scopes'] = ['company']
    first = send(client, 'Show task details', platform=platform)
    employee = send(client, 'Joydeep Ghosh', first['conversation_id'], platform)
    assert 'period' in employee['reply']
    report = send(client, 'For 2026-09-01', first['conversation_id'], platform)
    assert 'PRIVATE_TASK_NAME' in report['reply']
    send(client, 'Next page', first['conversation_id'], platform)
    assert state['last_task_body']['employee_id'] == 101
    assert state['last_task_body']['date'] == '2026-09-01'
    assert state['last_task_body']['page'] == 2
    assert create.await_count == 1


@pytest.mark.parametrize('file_format', ['PDF', 'Excel'])
def test_export_format_clarification_keeps_employee_and_period(backend, file_format):
    client, state = backend
    create = enable(client, state, [request(action='export_attendance', format=None, report_type='monthly_details')])
    first = send(client, 'Export Joydeep Ghosh attendance for September 2026')
    assert 'Which export format' in first['reply']
    result = send(client, file_format, first['conversation_id'])
    assert result['artifacts']
    assert state['last_export_body']['employee_id'] == 101
    assert state['last_export_body']['month'] == '2026-09'
    assert create.await_count == 1


def test_pronoun_report_change_keeps_subject_when_model_omits_followup_flag(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time'), request(action='late_punchin',
        employee_name=None, start_date=None, end_date=None, use_previous=False)])
    first = send(client, 'Show Joydeep booked time for September 2026')
    send(client, 'And his late punch-ins?', first['conversation_id'])
    assert state['last_manager_body']['report'] == 'late_punchin'
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['start_date'] == '2026-09-01'
    assert state['calls'].count('employee-search') == 1
    assert create.await_count == 2


@pytest.mark.parametrize('scope_message,scope', [('Only my team', 'team'), ('Only me', 'self')])
def test_scope_only_reply_replaces_employee_filter(backend, scope_message, scope):
    client, state = backend
    create = enable(client, state, [request(action='booked_time')])
    state['scopes'] = ['self', 'team', 'company']
    first = send(client, 'Show Joydeep booked time for September 2026')
    send(client, scope_message, first['conversation_id'])
    assert state['last_manager_body']['scope'] == scope
    assert state['last_manager_body']['employee_id'] is None
    assert state['last_manager_body']['start_date'] == '2026-09-01'
    assert create.await_count == 1


def test_project_name_clarification_keeps_period(backend):
    client, state = backend
    create = enable(client, state, [request(action='project_tasks', subject='company', employee_name=None)])
    state['task_scopes'] = ['company']
    first = send(client, 'Show project tasks for September 2026')
    assert 'Which project' in first['reply']
    send(client, 'IIHM', first['conversation_id'])
    assert state['last_task_body']['project_id'] == 7
    assert state['last_task_body']['start_date'] == '2026-09-01'
    assert create.await_count == 1


def test_thanks_preserves_report_for_export_without_model_call(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time')])
    first = send(client, 'Show Joydeep booked time for September 2026')
    send(client, 'Thank you', first['conversation_id'])
    exported = send(client, 'Make that a PDF', first['conversation_id'])
    assert exported['artifacts']
    assert create.await_count == 1


def test_longer_period_reply_keeps_report_even_if_model_changes_action(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', start_date=None, end_date=None),
        request(action='leave_balance', subject='unspecified', employee_name=None,
            start_date='2026-09-01', end_date='2026-09-30', use_previous=False)])
    first = send(client, 'Show Joydeep booked effort')
    send(client, 'For the past 30 days please', first['conversation_id'])
    assert state['last_manager_body']['report'] == 'booked_time'
    assert state['last_manager_body']['employee_id'] == 101
    assert 'leave-balance' not in state['calls']
    assert create.await_count == 2


def test_latest_contextless_answer_does_not_resurrect_older_selection():
    history = [{'role': 'assistant', 'query_context': json.dumps({'kind': 'semantic_question',
        'selection': {'action': 'booked_time', 'employee_id': 101}})},
        {'role': 'assistant', 'query_context': None}]
    assert trusted_query(history) == {}


def test_new_employee_request_requires_new_period_instead_of_reusing_old_subject(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time'), request(action='late_punchin',
        employee_name='Another Person', start_date=None, end_date=None, use_previous=False)])
    first = send(client, 'Show Joydeep booked effort for September 2026')
    state['search_matches'] = [{'employee_id': 101, 'name': 'Another Person'}]
    state['calls'].clear()
    result = send(client, 'Show Another Person late punch-ins', first['conversation_id'])
    assert 'period' in result['reply']
    assert 'manager-report' not in state['calls']
    assert create.await_count == 2


def test_scope_followup_cannot_grant_company_access(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', subject='self', employee_name=None)])
    state['scopes'] = ['self']
    first = send(client, 'Show my booked effort for September 2026')
    response = client.post('/v1/chat', headers=headers(), json={
        'message': 'The whole company', 'conversation_id': first['conversation_id']})
    assert response.status_code == 200
    assert 'only your own records' in response.json()['data']['reply']
    assert not response.json()['data']['charts']
    assert state['calls'].count('manager-report') == 1
    assert create.await_count == 1


def test_visit_to_employee_effort_followup_keeps_person(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', subject='previous', employee_name=None,
        use_previous=False)])
    state['modules'].append('visits')
    first = send(client, 'Joydeep Ghosh last visited client?')
    send(client, 'And his booked hours for September 2026?', first['conversation_id'])
    assert state['last_manager_body']['report'] == 'booked_time'
    assert state['last_manager_body']['employee_id'] == 101
    assert create.await_count == 1
    assert 'PRIVATE EMPLOYEE' not in str(create.call_args)


def test_explicit_employee_id_clarification_keeps_period(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', subject='unspecified', employee_name=None)])
    first = send(client, 'Show booked effort for September 2026')
    send(client, '101', first['conversation_id'])
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['start_date'] == '2026-09-01'
    assert 'employee-search' not in state['calls']
    assert create.await_count == 1


def test_scope_change_cannot_keep_old_employee_even_with_model_followup(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time'), request(action='booked_time', subject='team',
        employee_name=None, start_date=None, end_date=None, use_previous=True)])
    state['scopes'] = ['self', 'team', 'company']
    first = send(client, 'Show Joydeep booked effort for September 2026')
    send(client, 'Show the report for the people in my team instead', first['conversation_id'])
    assert state['last_manager_body']['scope'] == 'team'
    assert state['last_manager_body']['employee_id'] is None
    assert state['last_manager_body']['start_date'] == '2026-09-01'
    assert create.await_count == 2


def test_missing_report_person_and_period_can_be_supplied_in_separate_turns(backend):
    client, state = backend
    create = enable(client, state, [request(action='clarify', subject='unspecified', employee_name=None,
        start_date=None, end_date=None, period_kind='none')])
    first = send(client, 'I need a report')
    send(client, 'Joydeep Ghosh', first['conversation_id'])
    send(client, 'For September 2026', first['conversation_id'])
    send(client, 'Booked effort', first['conversation_id'])
    assert state['last_manager_body']['report'] == 'booked_time'
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['start_date'] == '2026-09-01'
    assert create.await_count == 1


def test_memory_is_isolated_between_conversations(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time'),
        request(action='booked_time', subject='unspecified', employee_name=None)])
    first = send(client, 'Show Joydeep booked effort for September 2026')
    independent = send(client, 'Show booked effort for September 2026')
    assert independent['conversation_id'] != first['conversation_id']
    assert 'Whose records' in independent['reply']
    assert state['calls'].count('manager-report') == 1
    assert create.await_count == 2


def test_period_can_be_supplied_before_requested_employee(backend):
    client, state = backend
    create = enable(client, state, [request(action='tasks', subject='unspecified', employee_name=None,
        start_date=None, end_date=None, period_kind='none')])
    state['task_scopes'] = ['company']
    first = send(client, 'Show task details')
    send(client, 'For September 2026', first['conversation_id'])
    send(client, 'Joydeep Ghosh', first['conversation_id'])
    assert state['last_task_body']['employee_id'] == 101
    assert state['last_task_body']['month'] == '2026-09'
    assert create.await_count == 1


def test_named_followup_preserves_period_but_rechecks_new_employee(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time')])
    first = send(client, 'Show Joydeep booked effort for September 2026')
    state['search_matches'] = []
    result = send(client, 'What about Anirban Singh', first['conversation_id'])
    assert 'unique authorized employee' in result['reply']
    assert any(body['name'] == 'Anirban Singh' for body in state['search_bodies'])
    assert state['calls'].count('manager-report') == 1
    assert create.await_count == 1


def test_visit_period_change_keeps_visit_report_and_employee(backend):
    from app.manager_query import report_period
    client, state = backend
    create = enable(client, state, [])
    state['modules'].append('visits')
    first = send(client, 'Show Joydeep Ghosh visits today')
    send(client, 'For last month', first['conversation_id'])
    start, end = report_period('last month')
    assert state['last_visit_body']['action'] == 'visits'
    assert state['last_visit_body']['employee_name'] == 'Joydeep Ghosh'
    assert (state['last_visit_body']['start_date'], state['last_visit_body']['end_date']) == (start, end)
    assert 'attendance' not in state['calls']
    create.assert_not_awaited()


def test_visit_scope_change_does_not_change_report_type(backend):
    client, state = backend
    create = enable(client, state, [])
    state['modules'].append('visits')
    first = send(client, 'Show Joydeep Ghosh visits today')
    send(client, 'Only me', first['conversation_id'])
    assert state['last_visit_body']['action'] == 'visits'
    assert state['last_visit_body']['scope'] == 'self'
    assert state['last_visit_body']['employee_name'] is None
    assert 'attendance' not in state['calls']
    create.assert_not_awaited()


def test_leave_filter_correction_keeps_year_and_ranking(backend):
    from tests.test_leave_balance_query import response
    client, state = backend
    create = enable(client, state, [])
    state['balance_data'] = response()
    first = send(client, 'Show highest leave balances for 2026-27')
    invalid = send(client, 'Show highest leave balance last year', first['conversation_id'])
    assert 'financial year' in invalid['reply']
    send(client, 'Annual Leave', first['conversation_id'])
    assert state['last_balance_body']['financial_year'] == '2026-27'
    assert state['last_balance_body']['leave_type'] == 'Annual Leave'
    assert state['last_balance_body']['ranking'] == 'highest'
    create.assert_not_awaited()


def test_invalid_leave_limit_can_be_corrected_without_reentering_year(backend):
    from tests.test_leave_balance_query import response
    client, state = backend
    create = enable(client, state, [])
    state['balance_data'] = response()
    first = send(client, 'Show top 51 highest leave balances for 2026-27')
    assert 'between 1 and 50' in first['reply']
    send(client, 'Top 10', first['conversation_id'])
    assert state['last_balance_body']['financial_year'] == '2026-27'
    assert state['last_balance_body']['limit'] == 10
    create.assert_not_awaited()
