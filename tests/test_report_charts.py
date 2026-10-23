import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas import ReportChart
from app.report_charts import build_report_chart, make_chart, restyle
from tests.test_backend import backend, headers
from tests.test_semantic_query import enable, request
from tests.test_work_report import wire


@pytest.mark.parametrize('kind', ['pie', 'doughnut', 'bar', 'table', 'auto', 'line'])
def test_booked_effort_charts_use_billing_not_attendance_or_planned_time(backend, kind):
    client, state = backend
    mock = enable(client, state, [request(action='booked_time', subject='self', employee_name=None,
        chart_type=kind, chart_metric='booked_effort')])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'My booked effort chart for September'})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    chart = data['charts'][0]
    assert chart['type'] == ('doughnut' if kind == 'auto' else 'bar' if kind == 'line' else kind)
    assert chart['labels'] == ['Billable', 'Non-billable']
    assert chart['series'][0]['values'] == [1, 0.5]
    assert 'Total booked: 1h 30m' in chart['subtitle']
    assert 'PRIVATE_MANAGER_NAME' in chart['title']
    assert state['last_manager_body']['report'] == 'booked_time'
    assert 'attendance' not in state['calls']
    assert 'PRIVATE_MANAGER_NAME' not in str(mock.call_args)
    saved = client.get('/v1/conversations/' + data['conversation_id'], headers=headers()).json()['data']
    assert saved['messages'][-1]['charts'] == data['charts']


def test_convert_plain_booked_report_to_pie_retains_employee_and_period(backend):
    client, state = backend
    mock = enable(client, state, [request(action='booked_time'),
        request(action='work_summary', subject='previous', employee_name=None,
                start_date=None, end_date=None, use_previous=False)])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep Ghosh booked effort for September'}).json()['data']
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Convert it into Pie Chart',
        'conversation_id': first['conversation_id']})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['charts'][0]['type'] == 'pie'
    assert state['last_manager_body']['employee_id'] == 101
    assert state['last_manager_body']['start_date'] == '2026-09-01'
    assert state['last_manager_body']['end_date'] == '2026-09-30'
    third = client.post('/v1/chat', headers=headers(), json={'message': 'Change it to a bar chart',
        'conversation_id': first['conversation_id']})
    assert third.status_code == 200, third.text
    assert third.json()['data']['charts'][0]['series'] == data['charts'][0]['series']
    assert mock.await_count == 2


def test_booked_effort_chart_respects_backend_permission(backend):
    client, state = backend
    enable(client, state, [request(action='booked_time', subject='self', employee_name=None, chart_type='pie')])
    state['effort_denied'] = True
    response = client.post('/v1/chat', headers=headers(), json={'message': 'My booked effort pie chart for September'})
    assert response.status_code == 403


@pytest.mark.parametrize('ambiguous', [False, True])
def test_booked_effort_chart_resolves_project_before_fetching(backend, ambiguous):
    client, state = backend
    enable(client, state, [request(action='booked_time', subject='unspecified', employee_name=None,
        project_name='IIHM', chart_type='pie', chart_metric='booked_effort')])
    if ambiguous:
        state['project_matches'] = [{'project_id': 7, 'name': 'IIHM'}, {'project_id': 8, 'name': 'IIHM Support'}]
    response = client.post('/v1/chat', headers=headers(), json={'message': 'IIHM booked effort pie chart for September'})
    assert response.status_code == 200, response.text
    assert state['last_project_search']['report'] == 'booked_time'
    if ambiguous:
        assert 'manager-report' not in state['calls']
        assert not response.json()['data']['charts']
    else:
        assert state['last_manager_body']['project_id'] == 7
        assert response.json()['data']['charts'][0]['type'] == 'pie'


@pytest.mark.parametrize('case', ['unclassified', 'missing', 'truncated', 'empty'])
def test_booked_effort_chart_coverage(case):
    billing = {'billable': {'minutes': 60, 'entry_count': 1},
               'non_billable': {'minutes': 30, 'entry_count': 1},
               'unclassified': {'minutes': 15 if case == 'unclassified' else 0,
                                'entry_count': 1 if case == 'unclassified' else 0}}
    row = dict(employee_id=1, employee_name='Employee', booked_minutes=105 if case == 'unclassified' else 90,
               entry_count=3 if case == 'unclassified' else 2, unavailable_count=1 if case == 'missing' else 0,
               billing=billing)
    data = dict(report='booked_time', start_date='2026-09-01', end_date='2026-09-30', scope='self',
                rows=[] if case == 'empty' else [row], matched_count=2 if case == 'truncated' else 1,
                details_truncated=case == 'truncated', notice='')
    service = SimpleNamespace(backend=SimpleNamespace(manager_report=AsyncMock(return_value=data)))
    selected = dict(start_date='2026-09-01', end_date='2026-09-30', scope='self',
                    chart_metric='booked_effort', chart_type='pie')
    text, charts, used = asyncio.run(build_report_chart(service, None, selected))
    assert used == ['get_manager_report']
    if case == 'unclassified':
        assert charts[0]['labels'] == ['Billable', 'Non-billable', 'Unclassified billing']
        assert charts[0]['series'][0]['values'] == [1, 0.5, 0.25]
    else:
        assert not charts


@pytest.mark.parametrize('text', [
    'Show my attendance as a pie chart for September 1',
    'Make a circle chart of my attendance for 01/09/2026',
    'Attendance breakdown visually please, mine, 2026-09-01',
    'My attendnce in a pi chart for 01/09/2026',
])
def test_chart_paraphrases_use_authorized_tool_and_persist_history(backend, text):
    client, state = backend
    mock = enable(client, state, [request(action='attendance', subject='self', employee_name=None,
        start_date='2026-09-01', end_date='2026-09-01', chart_type='pie', chart_metric='attendance_breakdown')])
    response = client.post('/v1/chat', headers=headers(), json={'message': text})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    chart = data['charts'][0]
    assert chart['labels'] == ['Present'] and chart['series'][0]['values'] == [1.0]
    assert chart['unit'] == 'employee-days' and chart['type'] == 'pie'
    assert 'Real test employee' in chart['title']
    assert '1 employee | 1 calendar day' in chart['subtitle']
    assert 'attendance' in state['calls']
    assert 'Real test employee' not in str(mock.call_args)
    schema = mock.call_args.kwargs['tools'][0]['parameters']
    assert set(schema['required']) == set(schema['properties'])
    saved = client.get('/v1/conversations/' + data['conversation_id'], headers=headers()).json()['data']
    assert saved['messages'][-1]['charts'] == data['charts']
    assert client.get('/v1/conversations/' + data['conversation_id'], headers=headers(company=2)).status_code == 404


def test_type_only_followup_reuses_values_without_model_or_tool_call(backend):
    client, state = backend
    mock = enable(client, state, [request(action='attendance', subject='self', employee_name=None,
        start_date='2026-09-01', end_date='2026-09-01', chart_type='pie', chart_metric='attendance_breakdown')])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'My attendance pie chart September 1'}).json()['data']
    reads = state['calls'].count('attendance')
    for phrase, kind in [('Change it to a bar chart', 'bar'), ('Show the same in a table', 'table')]:
        response = client.post('/v1/chat', headers=headers(), json={'message': phrase, 'conversation_id': first['conversation_id']})
        assert response.status_code == 200, response.text
        assert response.json()['data']['charts'][0]['type'] == kind
        assert response.json()['data']['charts'][0]['series'] == first['charts'][0]['series']
    assert mock.await_count == 1 and state['calls'].count('attendance') == reads


def test_followup_period_and_scope_keep_chart_selection(backend):
    client, state = backend
    enable(client, state, [request(action='attendance', subject='self', employee_name=None,
        start_date='2026-09-01', end_date='2026-09-01', chart_type='bar', chart_metric='attendance_breakdown'),
        request(action='attendance', subject='previous', employee_name=None, start_date='2026-09-02',
                end_date='2026-09-02', use_previous=True)])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'My attendance bar chart September 1'}).json()['data']
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Same for September 2', 'conversation_id': first['conversation_id']})
    assert second.status_code == 200, second.text
    chart = second.json()['data']['charts'][0]
    assert chart['type'] == 'bar' and '2026-09-02' in chart['title']
    assert state['last_attendance_body']['scope'] == 'self'


@pytest.mark.parametrize('metric,kind', [('working_hours', 'bar'), ('productivity', 'doughnut'), ('activity', 'line')])
def test_work_charts_keep_records_local_and_no_detailed_review(backend, metric, kind):
    client, state = backend
    mock, seen = wire(client, state, [request(action='work_summary', subject='self', employee_name=None,
        start_date='2026-09-01', end_date='2026-09-02', chart_type='auto', chart_metric=metric)])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my working chart September 1 to 2'})
    assert response.status_code == 200, response.text
    chart = response.json()['data']['charts'][0]
    assert chart['type'] == kind
    assert seen[0]['include_daily'] is True and seen[0]['detailed_review'] is False
    assert 'Private Employee' not in str(mock.call_args)
    assert 'charts' not in json.dumps(mock.call_args.kwargs['input'])
    if metric == 'productivity':
        assert chart['series'][0]['values'] == [10.0, 2.0]


def test_missing_work_values_are_gaps_not_zero_and_pie_falls_back(backend):
    client, state = backend
    def change(data):
        data['rows'][0]['daily'][0]['attendance_minutes'] = None
    wire(client, state, [request(action='work_summary', subject='self', employee_name=None,
        start_date='2026-09-01', end_date='2026-09-02', chart_type='pie', chart_metric='working_hours')], change)
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Show my working hours pie chart September 1 to 2'})
    assert result.status_code == 200, result.text
    chart = result.json()['data']['charts'][0]
    assert chart['type'] == 'bar' and chart['series'][0]['values'][0] is None


def test_tracking_leak_in_requested_daily_data_is_rejected(backend):
    client, state = backend
    def change(data):
        data['rows'][0]['daily'][0]['date'] = '2026-08-01'
    wire(client, state, [request(action='work_summary', subject='self', employee_name=None,
        start_date='2026-09-01', end_date='2026-09-01', chart_type='line', chart_metric='activity')], change)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'My activity chart'})
    assert response.status_code == 502


def test_forbidden_chart_scope_never_calls_report_tool(backend):
    client, state = backend
    enable(client, state, [request(action='attendance', subject='company', employee_name=None,
        chart_type='pie', chart_metric='attendance_breakdown')])
    state['scopes'] = ['self']
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Company attendance chart'})
    assert response.status_code == 403 and 'attendance' not in state['calls']


def test_chart_schema_rejects_nonfinite_negative_and_mismatched_values():
    chart = make_chart('bar', 'Test', 'hours', ['A'], [{'name': 'Hours', 'values': [1]}], '')
    for values in [[float('nan')], [-1], [1, 2]]:
        with pytest.raises(ValidationError):
            ReportChart.model_validate({**chart, 'series': [{'name': 'Hours', 'values': values}]})
    chart = make_chart('line', 'Activity', 'percent', ['A'], [{'name': 'Activity', 'values': [75]}], '')
    text, charts = restyle('Make it a pie chart', [chart])
    assert not charts and 'parts-of-a-whole' in text


def test_monthly_chart_uses_full_summary_not_truncated_rows():
    data = {'employee_ids': [101, 102], 'employee_count': 2, 'summary': {'Present': 40, 'Absent': 10},
            'unclassified_employee_days': 10, 'rows': [{'status': 'Present'}]}
    backend = SimpleNamespace(attendance=AsyncMock(return_value=data))
    selected = dict(start_date='2026-09-01', end_date='2026-09-30', scope='company', chart_type='pie', chart_metric='attendance_breakdown')
    _, charts, _ = asyncio.run(build_report_chart(SimpleNamespace(backend=backend), None, selected))
    assert sum(charts[0]['series'][0]['values']) == 60
    assert charts[0]['title'].startswith('Company attendance')
    assert charts[0]['subtitle'] == '2 employees | 30 calendar days | 60 employee-days'
    assert charts[0]['labels'] == ['Absent', 'Present', 'Unclassified']
    data['summary']['Present'] = 500
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(build_report_chart(SimpleNamespace(backend=backend), None, selected))


def test_plain_question_never_enables_chart_from_model_suggestion(backend):
    client, state = backend
    enable(client, state, [request(action='attendance', subject='self', employee_name=None,
        start_date='2026-09-01', end_date='2026-09-01', chart_type='pie', chart_metric='attendance_breakdown')])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my attendance for September 1'})
    assert response.status_code == 200 and response.json()['data']['charts'] == []


def test_tracking_unavailable_does_not_become_zero_productivity(backend):
    client, state = backend
    def change(data):
        row = data['rows'][0]
        row['tracking_available'] = False
        for field in ('productive_seconds', 'idle_seconds', 'tracked_seconds', 'screenshot_count'):
            row['summary'][field] = None
            row['summary'][field + '_days'] = 0
            for day in row['daily']:
                day[field] = None
        row['summary'].update(average_activity=None, activity_days=0)
        for day in row['daily']:
            day['activity'] = None
    wire(client, state, [request(action='work_summary', subject='self', employee_name=None,
        start_date='2026-09-01', end_date='2026-09-01', chart_type='pie', chart_metric='productivity')], change)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'My productive versus idle pie chart'})
    assert response.status_code == 200, response.text
    assert not response.json()['data']['charts']
    assert 'unavailable' in response.json()['data']['reply']


def test_chart_requires_explicit_period_and_rejects_overlong_period(backend):
    client, state = backend
    enable(client, state, [request(action='work_summary', subject='self', employee_name=None,
        start_date=None, end_date=None, chart_type='line', chart_metric='working_hours'),
        request(action='attendance', subject='self', employee_name=None,
        start_date='2026-08-01', end_date='2026-09-30', chart_type='pie', chart_metric='attendance_breakdown')])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'My working hours chart'})
    assert first.status_code == 200 and 'Which date' in first.json()['data']['reply']
    second = client.post('/v1/chat', headers=headers(), json={'message': 'My attendance pie chart August to September'})
    assert second.status_code == 200 and 'up to 31 days' in second.json()['data']['reply']
    assert not second.json()['data']['charts'] and 'attendance' not in state['calls']


def test_scope_followup_clears_previous_employee_filter(backend):
    client, state = backend
    enable(client, state, [request(action='attendance', chart_type='bar', chart_metric='attendance_breakdown',
        start_date='2026-09-01', end_date='2026-09-01'),
        request(action='attendance', subject='company', employee_name=None, start_date=None, end_date=None, use_previous=True)])
    calls = []
    original = client.app.state.backend.daily_attendance
    async def daily(identity, args):
        calls.append(args)
        # The fixture returns one authorized employee, preserving backend validation.
        state['allow_company_read'] = True
        return await original(identity, args)
    client.app.state.backend.daily_attendance = daily
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep attendance chart September 1'}).json()['data']
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Same for my company', 'conversation_id': first['conversation_id']})
    assert response.status_code == 200, response.text
    assert calls[0].employee_id == 101
    assert calls[-1].scope == 'company' and calls[-1].employee_id is None


def test_attendance_trend_reuses_complete_month_rows_and_fetches_missing_days():
    data = {'employee_ids': [101], 'rows': [{'employee_id': 101, 'date': '2026-09-01', 'status': 'Present'},
                                         {'employee_id': 101, 'date': '2026-09-02', 'status': ''}]}
    backend = SimpleNamespace(attendance=AsyncMock(return_value=data), daily_attendance=AsyncMock())
    selected = dict(start_date='2026-09-01', end_date='2026-09-02', scope='self', chart_type='line', chart_metric='attendance_trend')
    _, charts, _ = asyncio.run(build_report_chart(SimpleNamespace(backend=backend), None, selected))
    assert backend.attendance.await_count == 1 and backend.daily_attendance.await_count == 0
    assert charts[0]['labels'] == ['2026-09-01', '2026-09-02']
    assert charts[0]['series'][1]['name'] == 'Unclassified' and charts[0]['series'][1]['values'] == [0, 1]
    data['rows'].pop()
    backend.daily_attendance.return_value = {'employee_ids': [101], 'rows': [{'employee_id': 101, 'date': '2026-09-02', 'status': 'Absent'}]}
    _, charts, _ = asyncio.run(build_report_chart(SimpleNamespace(backend=backend), None, selected))
    assert backend.daily_attendance.await_count == 1
    assert charts[0]['series'][0]['name'] == 'Absent'
