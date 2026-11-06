import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.semantic_query import Question
from tests.test_backend import backend, headers


def request(action='late_punchin', **changes):
    value = dict(action=action, subject='employee', employee_name='Joydeep Ghosh', project_name=None,
                 start_date='2026-09-01', end_date='2026-09-30', latest_limit=None, page=None,
                 threshold=None, format=None, report_type=None, use_previous=False, answer_style='details', period_kind='range')
    return {**value, **changes}


def enable(client, state, requests):
    state.update(modules=['attendance', 'tasks', 'leave'], scopes=['self', 'company'])
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    service.settings.semantic_questions = True
    service.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=[
        SimpleNamespace(status='completed', output=[SimpleNamespace(type='function_call', name='interpret_question',
            arguments=json.dumps(value))]) for value in requests])), close=AsyncMock())
    state['search_matches'] = [{'employee_id': 101, 'name': 'Joydeep Ghosh'}]
    return service.client.responses.create


def test_latest_employee_tasks_and_next_batch_need_no_dates(backend):
    client, state = backend
    state['task_scopes'] = ['company']
    call = enable(client, state, [
        request(action='tasks', start_date=None, end_date=None, latest_limit=10, period_kind='none'),
        request(action='tasks', subject='previous', employee_name=None, start_date=None,
                end_date=None, latest_limit=10, page=2, use_previous=True, period_kind='none'),
    ])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Give me Last 10 Task report of Joydeep Ghosh (Employee)'})
    assert first.status_code == 200
    assert 'all history (newest first)' in first.json()['data']['reply']
    assert 'Date: 2026-09-01' in first.json()['data']['reply']
    assert state['last_task_body']['employee_id'] == 101
    assert state['last_task_body']['latest_limit'] == 10
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Next 10 please',
        'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_task_body']['page'] == 2
    assert state['last_task_body']['employee_id'] == 101
    assert state['last_task_body']['start_date'] is None
    assert 'PRIVATE_TASK_DESCRIPTION' not in json.dumps(call.call_args.kwargs)


def test_latest_employee_correction_clears_previous_period_not_employee(backend):
    client, state = backend
    state['task_scopes'] = ['company']
    enable(client, state, [request(action='tasks'), request(action='tasks', subject='previous',
        employee_name=None, start_date=None, end_date=None, latest_limit=10,
        use_previous=True, period_kind='none')])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep tasks for September'})
    second = client.post('/v1/chat', headers=headers(), json={'message': 'I want latest 10 tasks no date range',
        'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_task_body']['employee_id'] == 101
    assert state['last_task_body']['start_date'] is None
    assert state['last_task_body']['end_date'] is None
    assert state['last_task_body']['latest_limit'] == 10


def test_latest_employee_tasks_reject_foreign_backend_rows(backend):
    client, state = backend
    state.update(task_scopes=['company'], bad_task_response=True)
    enable(client, state, [request(action='tasks', start_date=None, end_date=None,
                                  latest_limit=10, period_kind='none')])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep latest ten tasks'})
    assert response.status_code == 502


@pytest.mark.parametrize('text', [
    'Was Joydeep late arriving during September?',
    'Count Joydeep Ghosh late entries last month',
    'September late checkins for Joydeep please',
])
def test_paraphrases_execute_structured_intent_not_regex(backend, text):
    client, state = backend
    call = enable(client, state, [request()])
    response = client.post('/v1/chat', headers=headers(), json={'message': text})
    assert response.status_code == 200
    assert state['last_manager_body']['report'] == 'late_punchin'
    assert state['last_manager_body']['employee_id'] == 101
    assert '7 minutes after the cutoff' in response.json()['data']['reply']
    assert call.await_count == 1
    assert call.call_args.kwargs['store'] is False


def test_followup_keeps_selected_employee_and_dates_without_records_in_model_input(backend):
    client, state = backend
    call = enable(client, state, [request(), request(action='booked_time', subject='previous', employee_name=None,
        start_date=None, end_date=None, use_previous=True)])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep late last month'})
    second = client.post('/v1/chat', headers=headers(), json={'message': 'And his booked hours?',
        'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['start_date'] == '2026-09-01'
    assert state['last_manager_body']['report'] == 'booked_time'
    assert 'PRIVATE_MANAGER_NAME' not in json.dumps(call.call_args.kwargs)


def test_ambiguous_employee_never_reads_company(backend):
    client, state = backend
    enable(client, state, [request()])
    state['search_matches'] = [{'employee_id': 101, 'name': 'Joydeep'}, {'employee_id': 102, 'name': 'Joydeep'}]
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep late last month'})
    assert response.status_code == 200
    assert 'manager-report' not in state['calls']
    assert 'unique authorized employee' in response.json()['data']['reply']


def test_named_subject_cannot_expand_to_all_company(backend):
    client, state = backend
    enable(client, state, [request(subject='company')])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep late last month'})
    assert response.status_code == 200
    assert state['last_manager_body']['employee_id'] == 101


def test_model_cannot_supply_employee_ids(backend):
    client, state = backend
    enable(client, state, [request(employee_id=602)])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep late last month'})
    assert response.status_code == 502
    assert 'manager-report' not in state['calls']


def test_unauthorized_company_scope_is_not_changed_to_self(backend):
    client, state = backend
    enable(client, state, [request(subject='company', employee_name=None)])
    state['scopes'] = ['self']
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show all company late punch-ins'})
    assert response.status_code == 200
    assert 'only your own records' in response.json()['data']['reply']
    assert 'manager-report' not in state['calls']


@pytest.mark.parametrize('platform', ['web', 'mobile'])
@pytest.mark.parametrize('scope', ['team', 'company'])
@pytest.mark.parametrize('action', ['late_punchin', 'attendance', 'tasks', 'booked_time', 'export_attendance'])
def test_unavailable_scope_is_a_chat_reply_without_record_reads(backend, platform, scope, action):
    client, state = backend
    enable(client, state, [request(action=action, subject=scope, employee_name=None,
        format='pdf' if action == 'export_attendance' else None)])
    state['scopes'] = ['self']
    response = client.post('/v1/chat', headers=headers(), json={
        'message': 'Show all employees punching in after 10:30 AM last month', 'client_platform': platform})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert 'only your own records' in data['reply']
    assert 'Please ask about your own records instead.' in data['reply']
    assert not data['artifacts']
    assert set(state['calls']) <= {'context'}


def test_team_access_does_not_allow_company_scope_or_downgrade_report(backend):
    client, state = backend
    enable(client, state, [request(subject='company', employee_name=None)])
    state['scopes'] = ['self', 'team']
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show all company late punch-ins'})
    assert response.status_code == 200
    assert 'requested employee scope' in response.json()['data']['reply']
    assert 'only your own records' not in response.json()['data']['reply']
    assert set(state['calls']) <= {'context'}


@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_personal_request_after_scope_denial_reads_only_self(backend, platform):
    client, state = backend
    enable(client, state, [request(subject='company', employee_name=None),
        request(subject='self', employee_name=None)])
    state['scopes'] = ['self']
    first = client.post('/v1/chat', headers=headers(), json={
        'message': 'Show company late punch-ins', 'client_platform': platform})
    assert first.status_code == 200
    assert set(state['calls']) <= {'context'}
    result = client.post('/v1/chat', headers=headers(), json={
        'message': 'Show my late punch-ins for September', 'client_platform': platform,
        'conversation_id': first.json()['data']['conversation_id']})
    assert result.status_code == 200, result.text
    assert state['last_manager_body']['scope'] == 'self'
    assert state['last_manager_body']['employee_id'] is None
    assert 'employee-search' not in state['calls']


def test_personal_questions_use_authenticated_user(backend):
    client, state = backend
    enable(client, state, [request(subject='self', employee_name=None)])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Did I check in late last month?'})
    assert response.status_code == 200
    assert state['last_manager_body']['scope'] == 'self'
    assert state['last_manager_body']['employee_id'] is None
    assert 'employee-search' not in state['calls']


@pytest.mark.parametrize('platform', ['web', 'mobile'])
@pytest.mark.parametrize('file_format', [None, 'pdf'])
def test_self_only_employee_export_denial_keeps_name_followup_on_attendance(backend, platform, file_format):
    client, state = backend
    create = enable(client, state, [request(action='export_attendance', employee_name='Anirban Singh',
        start_date='2026-10-01', end_date='2026-10-31', format=file_format, report_type='monthly_details')])
    state.update(scopes=['self'], search_matches=[])
    first = client.post('/v1/chat', headers=headers(), json={
        'message': 'Export attendance of Anirban Singh of October month' + (' as PDF' if file_format else ''),
        'client_platform': platform})
    assert first.status_code == 200
    assert 'only your own records' in first.json()['data']['reply']
    assert 'full name or employee ID' not in first.json()['data']['reply']
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Anirban Singh',
        'conversation_id': first.json()['data']['conversation_id'], 'client_platform': platform})
    assert second.status_code == 200
    assert 'only your own records' in second.json()['data']['reply']
    assert second.json()['data']['artifacts'] == []
    assert not {'attendance', 'export', 'leave-balance', 'manager-report'}.intersection(state['calls'])
    assert create.await_count == 1
    with client.app.state.store.connect() as database:
        saved = database.execute('SELECT query_context FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1',
            (first.json()['data']['conversation_id'],)).fetchone()
    context = json.loads(saved['query_context'])
    assert context['selection']['action'] == 'export_attendance'
    assert context['selection']['start_date'] == '2026-10-01'
    assert context['selection']['format'] == file_format


def test_self_only_employee_id_is_denied_before_report_read(backend):
    client, state = backend
    enable(client, state, [request(employee_name=None)])
    state['scopes'] = ['self']
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show employee ID 602 late last month'})
    assert response.status_code == 200
    assert 'only your own records' in response.json()['data']['reply']
    assert not {'employee-search', 'manager-report'}.intersection(state['calls'])


@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_self_only_effort_history_denies_both_short_and_full_names(backend, platform):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', employee_name=name,
        start_date=None, end_date=None, period_kind='none') for name in ('Anirban', 'Anirban Singh')])
    state.update(scopes=['self'], search_matches=[])
    conversation_id = None
    for name in ('Anirban', 'Anirban Singh'):
        response = client.post('/v1/chat', headers=headers(), json={
            'message': f'Can you tell me {name} Effort history?',
            'conversation_id': conversation_id, 'client_platform': platform})
        assert response.status_code == 200
        data = response.json()['data']
        assert 'only your own records' in data['reply']
        assert 'full name or employee ID' not in data['reply']
        assert data['artifacts'] == []
        conversation_id = data['conversation_id']
    assert 'manager-report' not in state['calls']
    assert create.await_count == 2


def test_self_only_own_name_remains_available(backend):
    client, state = backend
    enable(client, state, [request(action='attendance', end_date='2026-09-01', period_kind='day')])
    state['scopes'] = ['self']
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep Ghosh attendance for 2026-09-01'})
    assert response.status_code == 200
    assert state['last_attendance_body']['scope'] == 'self'
    assert state['last_attendance_body']['employee_id'] == 101


@pytest.mark.parametrize('scope', ['team', 'company'])
def test_authorized_name_clarification_keeps_report_period_and_format(backend, scope):
    client, state = backend
    create = enable(client, state, [request(action='export_attendance', employee_name='Joydeep',
        format='pdf', report_type='monthly_details')])
    state.update(scopes=['self', scope], search_matches=[])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Export Joydeep attendance for September as PDF'})
    assert first.status_code == 200
    state['search_matches'] = [{'employee_id': 101, 'name': 'Joydeep Ghosh'}]
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep Ghosh',
        'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200, second.text
    assert second.json()['data']['artifacts']
    assert state['last_export_body']['scope'] == scope
    assert state['last_export_body']['employee_id'] == 101
    assert state['last_export_body']['month'] == '2026-09'
    assert state['last_export_body']['format'] == 'pdf'
    assert create.await_count == 1


def test_new_question_after_employee_denial_can_change_report(backend):
    client, state = backend
    create = enable(client, state, [request(employee_name='Anirban Singh'),
        request(action='attendance', subject='self', employee_name=None, end_date='2026-09-01', period_kind='day')])
    state.update(scopes=['self'], search_matches=[])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show Anirban Singh late last month'})
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my attendance for 2026-09-01',
        'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200
    assert state['last_attendance_body']['scope'] == 'self'
    assert state['last_attendance_body']['employee_id'] is None
    assert create.await_count == 2


@pytest.mark.parametrize('platform', ['web', 'mobile'])
@pytest.mark.parametrize('action', ['booked_time', 'effort_by_employee', 'late_punchin'])
def test_period_clarification_retains_verified_employee_and_report_without_model(backend, platform, action):
    from app.manager_query import report_period
    client, state = backend
    create = enable(client, state, [request(action=action, start_date=None, end_date=None, period_kind='none')])
    first = client.post('/v1/chat', headers=headers(), json={
        'message': 'Can you tell me Joydeep Ghosh Effort history?', 'client_platform': platform})
    assert first.status_code == 200
    assert 'Which date or period' in first.json()['data']['reply']
    assert 'manager-report' not in state['calls']
    second = client.post('/v1/chat', headers=headers(), json={'message': 'For Last month',
        'conversation_id': first.json()['data']['conversation_id'], 'client_platform': platform})
    assert second.status_code == 200, second.text
    start, end = report_period('last month')
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['scope'] == 'company'
    assert state['last_manager_body']['report'] == action
    assert state['last_manager_body']['start_date'] == start
    assert state['last_manager_body']['end_date'] == end
    assert create.await_count == 1
    assert state['calls'].count('employee-search') == 1


@pytest.mark.parametrize('period,expected', [
    ('For 2026-09', ('2026-09-01', '2026-09-30')),
    ('September 2026', ('2026-09-01', '2026-09-30')),
    ('Sep 2026', ('2026-09-01', '2026-09-30')),
    ('On 2026-09-08', ('2026-09-08', '2026-09-08')),
    ('For 08/09/2026', ('2026-09-08', '2026-09-08')),
    ('From 2026-09-01 to 2026-09-10', ('2026-09-01', '2026-09-10')),
])
def test_period_only_replies_keep_named_employee(period, expected, backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', start_date=None, end_date=None, period_kind='none')])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep booked effort'})
    response = client.post('/v1/chat', headers=headers(), json={'message': period,
        'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200, response.text
    assert state['last_manager_body']['employee_id'] == 101
    assert (state['last_manager_body']['start_date'], state['last_manager_body']['end_date']) == expected
    assert create.await_count == 1


def test_invalid_period_keeps_employee_for_next_clarification(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', start_date=None, end_date=None, period_kind='none')])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep booked effort'}).json()['data']
    invalid = client.post('/v1/chat', headers=headers(), json={'message': '2026-02-31',
        'conversation_id': first['conversation_id']})
    assert invalid.status_code == 200
    assert 'valid period' in invalid.json()['data']['reply']
    assert 'manager-report' not in state['calls']
    corrected = client.post('/v1/chat', headers=headers(), json={'message': 'For 2026-09',
        'conversation_id': first['conversation_id']})
    assert corrected.status_code == 200
    assert state['last_manager_body']['employee_id'] == 101
    assert create.await_count == 1


def test_period_clarification_keeps_export_format(backend):
    client, state = backend
    create = enable(client, state, [request(action='export_attendance', start_date=None, end_date=None,
        period_kind='none', format='pdf')])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Export Joydeep attendance as PDF'})
    response = client.post('/v1/chat', headers=headers(), json={'message': 'For September 2026',
        'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200, response.text
    assert response.json()['data']['artifacts']
    assert state['last_export_body']['employee_id'] == 101
    assert state['last_export_body']['month'] == '2026-09'
    assert state['last_export_body']['format'] == 'pdf'
    assert create.await_count == 1


def test_same_employee_subject_followup_retains_verified_id(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', start_date=None, end_date=None, period_kind='none'),
        request(action='booked_time', employee_name=None, use_previous=True)])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep booked effort'})
    response = client.post('/v1/chat', headers=headers(), json={'message': 'I need the September 2026 figures please',
        'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200, response.text
    assert state['last_manager_body']['employee_id'] == 101
    assert state['calls'].count('employee-search') == 1
    assert create.await_count == 2


def test_period_reply_retains_project_selection(backend):
    client, state = backend
    create = enable(client, state, [request(action='project_tasks', subject='company', employee_name=None,
        project_name='IIHM', start_date=None, end_date=None, period_kind='none')])
    state['task_scopes'] = ['company']
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show IIHM project tasks'})
    response = client.post('/v1/chat', headers=headers(), json={'message': 'For September 2026',
        'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200, response.text
    assert state['last_task_body']['project_id'] == 7
    assert state['last_task_body']['start_date'] == '2026-09-01'
    assert state['last_task_body']['end_date'] == '2026-09-30'
    assert create.await_count == 1


def test_period_reply_does_not_reuse_selection_after_permission_change(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', start_date=None, end_date=None, period_kind='none')])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep booked effort'})
    state['policy'] = 'b' * 64
    response = client.post('/v1/chat', headers=headers(), json={'message': 'For last month',
        'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 409
    assert 'manager-report' not in state['calls']
    assert create.await_count == 1


def test_schema_requires_all_fields():
    schema = Question.model_json_schema()
    assert set(schema['required']) == set(schema['properties'])
    assert schema['additionalProperties'] is False


def test_unsupported_request_has_no_data_read(backend):
    client, state = backend
    enable(client, state, [request(action='unsupported')])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Approve his leave and export all LMS progress'})
    assert response.status_code == 200
    assert 'not supported' in response.json()['data']['reply']
    assert set(state['calls']) == {'context'}


def test_attendance_export_uses_existing_attachment(backend):
    client, state = backend
    enable(client, state, [request(action='export_attendance', subject='self', employee_name=None,
        format='pdf', report_type='custom_range', end_date='2026-09-10')])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Make a PDF of my September 1 to 10 attendance'})
    assert response.status_code == 200
    assert len(response.json()['data']['artifacts']) == 1


@pytest.mark.parametrize(('action', 'file_format'), [('attendance', 'pdf'), ('attendance_totals', 'xlsx')])
def test_attendance_file_format_cannot_be_ignored(backend, action, file_format):
    client, state = backend
    enable(client, state, [request(action=action, subject='company', employee_name=None,
        format=file_format, report_type='monthly_details', period_kind='month')])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Can you give me last month attendance report in PDF?'})
    assert response.status_code == 200
    assert state['last_export_body']['scope'] == 'company'
    assert state['last_export_body']['format'] == file_format
    assert state['last_export_body']['month'] == '2026-09'
    artifact = response.json()['data']['artifacts'][0]
    assert artifact['format'] == file_format
    download = client.get('/v1/reports/' + artifact['report_id'] + '/download', headers=headers())
    assert download.status_code == 200
    assert download.content.startswith(b'%PDF' if file_format == 'pdf' else b'PK')


def test_pdf_followup_retains_employee_and_period(backend):
    client, state = backend
    enable(client, state, [request(action='attendance_totals'), request(action='attendance', subject='previous',
        employee_name=None, start_date=None, end_date=None, format='pdf', use_previous=True)])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep attendance last month'})
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Make that a PDF',
        'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['start_date'] == '2026-09-01'
    assert state['last_manager_body']['end_date'] == '2026-09-30'
    assert second.json()['data']['tools_used'] == ['export_report']
    assert len(second.json()['data']['artifacts']) == 1


def test_attendance_format_does_not_bypass_export_permission(backend):
    client, state = backend
    enable(client, state, [request(action='attendance', subject='self', employee_name=None, format='pdf')])
    state['export'] = False
    response = client.post('/v1/chat', headers=headers(), json={'message': 'My attendance in PDF'})
    assert response.status_code == 403
    assert 'export' not in state['calls']


def test_ordinary_summary_does_not_inherit_export_behavior(backend):
    client, state = backend
    enable(client, state, [request(action='export_attendance', subject='self', employee_name=None,
        format='pdf'), request(action='attendance_totals', subject='previous', employee_name=None,
        use_previous=True)])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'My attendance PDF'})
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Show the summary instead',
        'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert second.json()['data']['artifacts'] == []
    assert state['calls'].count('export') == 1


def test_project_newest_batch_uses_existing_tool(backend):
    client, state = backend
    enable(client, state, [request(action='project_tasks', subject='company', employee_name=None,
        project_name='ARC Promotion', start_date=None, end_date=None, latest_limit=5)])
    state['project_matches'] = [{'project_id': 7, 'name': 'ARC Promotion'}]
    state['task_scopes'] = ['company']
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Five newest ARC Promotion tasks please'})
    assert response.status_code == 200
    assert state['last_task_body']['latest_limit'] == 5


def test_daily_work_mode_cannot_be_rendered_as_current_configuration(backend):
    client, state = backend
    enable(client, state, [request(action='work_arrangements', subject='self', employee_name=None)])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Was I working from home last month?'})
    assert response.status_code == 200
    assert 'Historical daily WFH/WFO' in response.json()['data']['reply']
    assert 'manager-report' not in state['calls']


def test_employee_change_does_not_reuse_old_resolved_id(backend):
    client, state = backend
    enable(client, state, [request(), request(employee_name='Different Employee', subject='employee',
        start_date=None, end_date=None, use_previous=True)])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep late last month'})
    state['search_matches'] = []
    state['calls'].clear()
    response = client.post('/v1/chat', headers=headers(), json={'message': 'What about Different Employee?',
        'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200
    assert 'employee-search' in state['calls']
    assert 'manager-report' not in state['calls']


def test_legacy_named_report_selection_can_be_reused(backend):
    import asyncio
    from app.semantic_query import interpret
    client, state = backend
    enable(client, state, [request(subject='previous', employee_name=None, start_date=None, end_date=None, use_previous=True)])
    context = {'kind': 'manager_report', 'report': 'late', 'scope': 'company', 'employee_ids': [101],
               'start_date': '2026-09-01', 'end_date': '2026-09-30'}
    _, previous = asyncio.run(interpret(client.app.state.chat, 'Punch-in late instead', context,
        SimpleNamespace(modules=['attendance'], scopes=['self', 'company'], can_export=True)))
    assert previous['employee_id'] == 101
    assert previous['subject'] == 'employee'


def test_explicit_employee_id_is_parsed_locally(backend):
    client, state = backend
    enable(client, state, [request(subject='employee', employee_name=None)])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Late punch-ins for employee ID 101 in September'})
    assert response.status_code == 200
    assert state['last_manager_body']['employee_id'] == 101
    assert 'employee-search' not in state['calls']


def test_provider_incomplete_response_never_reads_data(backend):
    client, state = backend
    call = enable(client, state, [])
    call.side_effect = None
    call.return_value = SimpleNamespace(status='incomplete', output=[])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my report'})
    assert response.status_code == 502
    assert set(state['calls']) == {'context'}


def test_named_project_does_not_require_user_to_supply_scope(backend):
    client, state = backend
    enable(client, state, [request(action='project_tasks', subject='unspecified', employee_name=None,
        project_name='Example Demo Project', start_date=None, end_date=None, latest_limit=5)])
    state['task_scopes'] = ['company']
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Five newest tasks for Example Demo Project'})
    assert response.status_code == 200
    assert state['last_task_body']['latest_limit'] == 5


def test_relative_export_does_not_require_model_supplied_dates(backend):
    client, state = backend
    enable(client, state, [request(action='export_attendance', subject='self', employee_name=None,
        start_date=None, end_date=None, report_type='last_7_days', format='pdf')])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'PDF of my last seven days attendance'})
    assert response.status_code == 200
    assert state['last_export_body']['report_type'] == 'last_7_days'
    assert state['last_export_body'].get('start_date') is None


@pytest.mark.parametrize('message', [
    'Can you tell me total booked hours for iihm projects in last month',
    'Can you tell me total effort booked hours for iihm projects for past month',
    'How much time was logged against IIHM in September?',
])
def test_project_actual_booking_totals_use_effort_lookup(backend, message):
    client, state = backend
    enable(client, state, [request(action='booked_time', subject='unspecified', employee_name=None, project_name='IIHM')])
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert state['last_project_search']['report'] == 'booked_time'
    assert state['last_manager_body']['project_id'] == 7
    assert state['last_manager_body']['report'] == 'booked_time'
    assert 'Total booked: 1h 30m' in response.json()['data']['reply']
    assert 'Billable: 1h 0m | Non-billable: 0h 30m' in response.json()['data']['reply']


def test_ambiguous_effort_project_does_not_read_all_bookings(backend):
    client, state = backend
    enable(client, state, [request(action='booked_time', subject='unspecified', employee_name=None, project_name='IIHM')])
    state['project_matches'] = [{'project_id': 7, 'name': 'IIHM'}, {'project_id': 8, 'name': 'IIHM Support'}]
    response = client.post('/v1/chat', headers=headers(), json={'message': 'IIHM booked hours last month'})
    assert response.status_code == 200
    assert 'manager-report' not in state['calls']
    assert 'IIHM Support' in response.json()['data']['reply']
    assert 'Several authorized projects' in response.json()['data']['reply']


def test_no_effort_project_match_is_not_described_as_multiple_projects(backend):
    client, state = backend
    enable(client, state, [request(action='booked_time', subject='unspecified', employee_name=None, project_name='Missing')])
    state['project_matches'] = []
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Missing project hours last month'})
    assert response.status_code == 200
    assert 'No project matched' in response.json()['data']['reply']
    assert 'manager-report' not in state['calls']


def test_project_name_clarification_keeps_requested_month(backend):
    client, state = backend
    enable(client, state, [request(action='booked_time', subject='unspecified', employee_name=None, project_name='IIHM'),
        request(action='booked_time', subject='previous', employee_name=None, project_name='IIHM Support',
                start_date=None, end_date=None, use_previous=True)])
    state['project_matches'] = [{'project_id': 7, 'name': 'IIHM Design'}, {'project_id': 8, 'name': 'IIHM Support'}]
    first = client.post('/v1/chat', headers=headers(), json={'message': 'IIHM project hours last month'})
    state['project_matches'] = [{'project_id': 8, 'name': 'IIHM Support'}]
    second = client.post('/v1/chat', headers=headers(), json={'message': 'IIHM Support',
        'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_manager_body']['project_id'] == 8
    assert state['last_manager_body']['start_date'] == '2026-09-01'


def test_present_followup_keeps_company_scope_after_headcount(backend):
    client, state = backend
    state['count_scopes'] = ['company']
    enable(client, state, [request(action='employee_count', subject='company', employee_name=None,
        start_date=None, end_date=None), request(action='present', subject='previous', employee_name=None,
        start_date='2026-10-13', end_date='2026-10-13', use_previous=True)])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'How many active employees are in the company scope?'})
    second = client.post('/v1/chat', headers=headers(), json={'message': 'how are present today?',
        'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_manager_body']['scope'] == 'company'
    assert state['last_manager_body']['report'] == 'present'
    assert 'Employees present: 1.' in second.json()['data']['reply']


def test_absence_question_answers_count_and_explains_unclassified_data(backend):
    client, state = backend
    enable(client, state, [request(action='absent', subject='company', employee_name=None,
        start_date='2026-10-13', end_date='2026-10-13')])
    state['manager_rows'] = []
    state['attendance_coverage'] = dict(employee_count=1, unclassified_employee_count=1, punch_in_employee_count=0)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'how many employee are absent today'})
    assert response.status_code == 200
    text = response.json()['data']['reply']
    assert 'Employees marked absent: 0.' in text
    assert 'remain unclassified; absence has not been confirmed' in text
    assert 'No matching records' not in text


def test_invalid_backend_attendance_coverage_is_not_rendered(backend):
    client, state = backend
    enable(client, state, [request(action='absent', subject='company', employee_name=None,
        start_date='2026-10-13', end_date='2026-10-13')])
    state['attendance_coverage'] = dict(employee_count=27, unclassified_employee_count=27, punch_in_employee_count=0)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'absent employees today'})
    assert response.status_code == 502


def test_operational_absence_is_not_reported_as_unconfirmed_calendar_absence():
    from app.manager_query import render_report
    data = dict(report='absent', absence_basis='not_checked_in', start_date='2026-10-13', end_date='2026-10-13',
        rows=[dict(employee_name='Example', count=1, dates=['2026-10-13'])], details_truncated=False,
        attendance_coverage=dict(employee_count=2, expected_employee_count=1, excluded_employee_count=1,
                                 unclassified_employee_count=2, punch_in_employee_count=0), notice='Calendar statuses are unchanged.')
    text = render_report(data)
    assert 'Employees not checked in: 1.' in text
    assert 'Excluded for leave, holiday or week-off: 1.' in text
    assert 'absence has not been confirmed' not in text


def test_count_then_names_keeps_date_and_scope_without_list_in_count_reply(backend):
    client, state = backend
    enable(client, state, [request(action='absent', subject='company', employee_name=None,
        start_date='2026-10-13', end_date='2026-10-13', answer_style='summary'),
        request(action='absent', subject='previous', employee_name=None, start_date=None, end_date=None,
                use_previous=True, answer_style='names')])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'how many employees are absent today'})
    assert first.status_code == 200
    assert 'Employees marked absent: 1.' in first.json()['data']['reply']
    assert 'PRIVATE_MANAGER_NAME' not in first.json()['data']['reply']
    state['calls'].clear()
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Show their names',
        'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert 'PRIVATE_MANAGER_NAME' in second.json()['data']['reply']
    assert state['last_manager_body']['scope'] == 'company'
    assert state['last_manager_body']['start_date'] == '2026-10-13'
    assert 'manager-report' in state['calls']


def test_operational_count_response_excludes_names():
    from app.manager_query import render_report
    data = dict(report='absent', absence_basis='not_checked_in', start_date='2026-10-13', end_date='2026-10-13',
        rows=[dict(employee_name='Private Employee', count=1, dates=['2026-10-13'])], details_truncated=False,
        attendance_coverage=dict(employee_count=1, expected_employee_count=1, excluded_employee_count=0,
                                 unclassified_employee_count=1, punch_in_employee_count=0), notice='Operational absence only.')
    assert 'Employees not checked in: 1.' in render_report(data, include_names=False)
    assert 'Private Employee' not in render_report(data, include_names=False)
    assert 'Private Employee' in render_report(data, include_names=True)


@pytest.mark.parametrize('message', ['Show my attendance for this month.', 'Show my attendance report for this month.'])
def test_calendar_month_with_month_to_date_end_executes_monthly_tool(backend, message):
    client, state = backend
    enable(client, state, [request(action='attendance', subject='self', employee_name=None,
        start_date='2026-10-01', end_date='2026-10-14', period_kind='month')])
    call = AsyncMock(return_value=dict(scope='self', month='2026-10',
        period=dict(start='2026-10-01', end='2026-10-31', as_of='2026-10-14'), employee_count=1,
        summary={'Present': 2}, unclassified_employee_days=29, worked_minutes=480,
        employee_summaries=[], notice='Recorded attendance only.'))
    client.app.state.backend.attendance = call
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert call.await_count == 1
    assert call.call_args.args[1].month == '2026-10'
    assert call.call_args.args[1].scope == 'self'
    assert 'Period: 2026-10-01 to 2026-10-31' in response.json()['data']['reply']
    assert 'get_attendance' in response.json()['data']['tools_used']


def test_custom_attendance_range_is_summarized_without_expansion(backend):
    client, state = backend
    enable(client, state, [request(action='attendance', subject='self', employee_name=None,
        start_date='2026-10-02', end_date='2026-10-14', period_kind='range')])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my attendance from October 2 to 14'})
    assert response.status_code == 200
    assert state['last_manager_body']['report'] == 'attendance_totals'
    assert state['last_manager_body']['start_date'] == '2026-10-02'
    assert state['last_manager_body']['end_date'] == '2026-10-14'
    assert state['last_manager_body']['scope'] == 'self'


def test_wrong_project_effort_response_is_rejected(backend):
    client, state = backend
    enable(client, state, [request(action='booked_time', subject='unspecified', employee_name=None, project_name='IIHM')])
    state['returned_effort_project_id'] = 99
    response = client.post('/v1/chat', headers=headers(), json={'message': 'IIHM booked hours last month'})
    assert response.status_code == 502
