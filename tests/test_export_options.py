from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas import ExportArgs
from tests.test_backend import backend, headers


@pytest.mark.parametrize('message,expected', [
    ('Export my attendance today as pdf', {'report_type': 'today'}),
    ('Download my attendance last 7 days as xlsx', {'report_type': 'last_7_days', 'format': 'xlsx'}),
    ('Export company attendance 2026-09 as pdf', {'report_type': 'monthly_details', 'month': '2026-09', 'scope': 'company'}),
    ('Export my attendance from 01/09/2026 to 12/09/2026 as pdf', {'report_type': 'custom_range', 'start_date': '2026-09-01', 'end_date': '2026-09-12'}),
    ('Export my attendance from 28/09/2026 to 04/10/2026 as pdf', {'report_type': 'custom_range', 'start_date': '2026-09-28', 'end_date': '2026-10-04'}),
    ('Export my attendance for 01/09/2026 as pdf', {'report_type': 'custom_range', 'start_date': '2026-09-01', 'end_date': '2026-09-01'}),
])
def test_export_options_are_resolved_locally(backend, message, expected):
    client, state = backend
    state['scopes'] = ['self', 'company']
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    create = AsyncMock(side_effect=AssertionError('Exports must not require OpenAI.'))
    service.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=AsyncMock())
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    data = response.json()['data']
    assert data['artifacts']
    assert all(state['last_export_body'][key] == value for key, value in expected.items())
    assert create.await_count == 0
    assert client.get(data['artifacts'][0]['download_url'], headers=headers(2)).status_code == 404


@pytest.mark.parametrize('message', [
    'Export my attendance from 12/09/2026 to 01/09/2026 as pdf',
    'Export my attendance from 30/02/2026 to 12/09/2026 as pdf',
    'Export my attendance from 01/01/2026 to 31/12/2026 as pdf',
    'Export my attendance as pdf',
    'Export unknown person report 2026-09 as pdf',
    'Export my attendance from 01/09/2026 to September 12 as pdf',
    'Export my attendance today for 2026-09 as pdf',
])
def test_invalid_or_ambiguous_export_never_fetches_report(backend, message):
    client, state = backend
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert response.json()['data']['artifacts'] == []
    assert 'export' not in state['calls']


@pytest.mark.parametrize('message', [
    'Export Joydeep Ghosh attendance from 01/09/2026 to 12/09/2026 as pdf',
    'I want Joydeep Ghosh attendance report for 01/09/2026 to 10/09/2026',
    'I want attendance report for Joydeep Ghosh from 01/09/2026 to 10/09/2026',
    'I need Joydeep Ghosh attendance report for 01/09/2026 to 10/09/2026',
])
def test_named_export_is_bound_to_verified_employee(backend, message):
    client, state = backend
    state.update(scopes=['self', 'company'], search_matches=[{'employee_id': 101, 'name': 'Joydeep Ghosh'}])
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    service.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=AssertionError('No model name guessing.'))), close=AsyncMock())
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert state['last_export_body']['employee_id'] == 101
    assert state['last_export_body']['scope'] == 'company'
    assert response.json()['data']['tools_used'] == ['employee_search', 'export_attendance']
    followup = client.post('/v1/chat', headers=headers(), json={
        'message': '01/09/2026 to 10/09/2026', 'conversation_id': response.json()['data']['conversation_id']})
    assert followup.status_code == 200
    assert state['last_export_body']['employee_id'] == 101
    assert state['last_export_body']['scope'] == 'company'


def test_screenshot_subhomoy_report_resolves_exact_subject(backend):
    client, state = backend
    state.update(scopes=['self', 'company'], search_matches=[{'employee_id': 101, 'name': 'Subhomoy Samanta'}])
    response = client.post('/v1/chat', headers=headers(), json={
        'message': 'I want Subhomoy Samanta attendance report for 01/09/2026 to 10/09/2026'})
    assert response.status_code == 200
    assert state['last_export_body']['employee_id'] == 101
    assert state['last_export_body']['start_date'] == '2026-09-01'
    assert state['last_export_body']['end_date'] == '2026-09-10'


@pytest.mark.parametrize('matches', [[], [{'employee_id': 101, 'name': 'Subhomoy Samanta'},
                                      {'employee_id': 102, 'name': 'Subhomoy Samanta'}]])
def test_screenshot_unresolved_subject_never_exports_all_employees(backend, matches):
    client, state = backend
    state.update(scopes=['self', 'company'], search_matches=matches)
    response = client.post('/v1/chat', headers=headers(), json={
        'message': 'I want Subhomoy Samanta attendance report for 01/09/2026 to 10/09/2026'})
    assert response.status_code == 200
    assert not response.json()['data']['artifacts']
    assert 'export' not in state['calls']


def test_model_export_guard_does_not_use_old_employee_for_new_unknown_subject():
    from app.chat_service import ChatService
    from app.auth import Identity
    identity = Identity(1, 101, 'Synthetic', 'Admin', ('self', 'company'), True)
    context = {'scope': 'company', 'employee_ids': [102]}
    args = {'scope': 'company', 'employee_id': None}
    assert ChatService.export_arguments('What about Another Person?', args, context, identity) is None
    assert ChatService.export_arguments('01/09/2026 to 10/09/2026', args, context, identity)['employee_id'] == 102
    assert ChatService.export_arguments('employee ID 201', args, context, identity)['employee_id'] == 201


def test_no_name_match_does_not_export_entire_company(backend):
    client, state = backend
    state.update(scopes=['self', 'company'], search_matches=[])
    response = client.post('/v1/chat', headers=headers(), json={
        'message': 'Export attendance for Joydeep Ghosh from 01/09/2026 to 12/09/2026 as pdf'})
    assert response.status_code == 200
    assert 'export' not in state['calls']


def test_export_rejects_scope_expansion(backend):
    client, state = backend
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Export company attendance today as pdf'})
    assert response.status_code == 403
    assert 'export' not in state['calls']


@pytest.mark.parametrize('args', [
    dict(report_type='custom_range', start_date='2026-09-12', end_date='2026-09-01'),
    dict(report_type='today', month='2026-09'),
    dict(report_type='monthly_details'),
    dict(report_type='custom_range', start_date='2026-09-01'),
])
def test_export_schema_rejects_conflicting_periods(args):
    with pytest.raises(ValidationError):
        ExportArgs(scope='self', format='pdf', **args)


def test_export_backend_rejects_foreign_employee_payload(backend):
    client, state = backend
    state['scopes'] = ['self', 'company']
    response = client.post('/v1/chat', headers=headers(), json={
        'message': 'Export company attendance for employee ID 201 today as pdf'})
    assert response.status_code == 502
    with client.app.state.store.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM reports').fetchone()[0] == 0
