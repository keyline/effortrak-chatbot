import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.report_charts import build_report_chart
from tests.test_backend import backend, headers
from tests.test_punch_time import filtered_rows, send
from tests.test_punch_workbook import punch_snapshot
from tests.test_semantic_query import enable, request


def chart_question(**changes):
    return request(action='late_punchin', subject='company', employee_name=None,
        **{**dict(punch_basis='fixed_time', punch_after='11:00:00', min_delay_minutes=0), **changes})


@pytest.mark.parametrize('platform', ['web', 'mobile'])
@pytest.mark.parametrize('words,types', [('pie chart', ['pie']), ('pic chart', ['pie']),
    ('bar chart', ['bar']), ('pie chart and bar chart', ['pie', 'bar'])])
def test_requested_charts_use_authorized_filtered_report_in_both_clients(backend, platform, words, types):
    client, state = backend
    call = enable(client, state, [chart_question(chart_type=types[-1], chart_metric='punch_in_days')])
    filtered_rows(state, 'fixed_time', 0)
    result = send(client, 'Employees punching in after 11 AM last month along with ' + words, platform=platform)
    assert result.status_code == 200, result.text
    data = result.json()['data']
    assert [chart['type'] for chart in data['charts']] == types
    assert 'Punch-in time report' in data['reply']
    assert 'PRIVATE EMPLOYEE' in data['reply']
    assert not data['artifacts']
    for chart in data['charts']:
        assert chart['labels'] == ['PRIVATE EMPLOYEE (#101)']
        assert chart['series'][0]['values'] == [1]
        assert chart['unit'] == 'employee-days'
        assert 'not late versus on-time attendance' in chart['note']
    body = state['last_manager_body']
    assert (body['scope'], body['punch_basis'], body['punch_after'], body['min_delay_minutes']) == (
        'company', 'fixed_time', '11:00:00', 0)
    assert state['calls'].count('manager-report') == 1
    assert 'PRIVATE EMPLOYEE' not in str(call.call_args.kwargs)


@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_plain_request_never_adds_chart_even_if_model_invents_one(backend, platform):
    client, state = backend
    enable(client, state, [chart_question(chart_type='pie', chart_metric='punch_in_days')])
    filtered_rows(state, 'fixed_time', 0)
    result = send(client, 'Employees punching in after 11 AM last month', platform=platform)
    assert result.status_code == 200, result.text
    assert not result.json()['data']['charts']
    assert 'Matching employees: 1' in result.json()['data']['reply']


@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_style_followup_keeps_previous_report_instead_of_model_substitution(backend, platform):
    client, state = backend
    enable(client, state, [chart_question(), request(action='attendance', subject='unspecified',
        employee_name=None, start_date=None, end_date=None, chart_type='pie')])
    filtered_rows(state, 'fixed_time', 0)
    first = send(client, 'Employees after 11 AM in September', platform=platform).json()['data']
    second = send(client, 'Show this as a pie chart', first['conversation_id'], platform)
    assert second.status_code == 200, second.text
    assert second.json()['data']['charts'][0]['type'] == 'pie'
    body = state['last_manager_body']
    assert body['report'] == 'late_punchin'
    assert body['start_date'] == '2026-09-01'
    assert body['end_date'] == '2026-09-30'
    assert body['punch_after'] == '11:00:00'
    assert 'attendance' not in state['calls']


def test_period_followup_without_chart_request_is_report_only(backend):
    client, state = backend
    enable(client, state, [chart_question(chart_type='pie', chart_metric='punch_in_days')])
    filtered_rows(state, 'fixed_time', 0)
    first = send(client, 'Employees after 11 AM in September with pie chart').json()['data']
    state['manager_rows'][0]['entries'][0]['date'] = '2026-10-01'
    second = send(client, 'For October 2026', first['conversation_id'])
    assert second.status_code == 200, second.text
    assert not second.json()['data']['charts']
    assert state['last_manager_body']['start_date'] == '2026-10-01'
    assert state['last_manager_body']['punch_after'] == '11:00:00'


@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_self_only_user_cannot_get_company_chart(backend, platform):
    client, state = backend
    enable(client, state, [chart_question(chart_type='pie')])
    state['scopes'] = ['self']
    result = send(client, 'Company employees after 11 AM with pie chart', platform=platform)
    assert result.status_code == 200
    assert 'only your own records' in result.json()['data']['reply']
    assert not result.json()['data']['charts']
    assert set(state['calls']) <= {'context'}


@pytest.mark.parametrize('scope', ['self', 'team', 'company'])
@pytest.mark.parametrize('basis,delay', [('shift_start', 20), ('late_cutoff', 0)])
def test_authorized_scopes_and_relative_delay_survive_chart_request(backend, scope, basis, delay):
    client, state = backend
    enable(client, state, [request(action='late_punchin', subject=scope, employee_name=None,
        punch_basis=basis, min_delay_minutes=delay, chart_type='bar')])
    state['scopes'] = ['self', scope]
    filtered_rows(state, basis, delay)
    result = send(client, 'Show matching punch-ins after shift with bar chart')
    assert result.status_code == 200, result.text
    assert result.json()['data']['charts'][0]['type'] == 'bar'
    assert state['last_manager_body']['scope'] == scope
    assert state['last_manager_body']['punch_basis'] == basis
    assert state['last_manager_body']['min_delay_minutes'] == delay


@pytest.mark.parametrize('condition', ['empty', 'zero', 'truncated', 'too_many', 'unassessable', 'unassessed'])
def test_chart_coverage_and_missing_values_are_not_misrepresented(condition):
    data = punch_snapshot()['sources'][0]['data']
    if condition == 'empty':
        data.update(rows=[], matched_count=0)
    elif condition == 'zero':
        data['rows'][0].update(count=0, entries=[])
    elif condition == 'truncated':
        data.update(details_truncated=True, matched_count=2)
    elif condition == 'too_many':
        base = deepcopy(data['rows'][0])
        data['rows'] = [{**deepcopy(base), 'employee_id': index} for index in range(51)]
        data['matched_count'] = 51
    elif condition == 'unassessable':
        data['rows'][0]['assessable'] = False
    service = SimpleNamespace(backend=SimpleNamespace(manager_report=AsyncMock(return_value=data)))
    selected = dict(scope='company', chart_metric='punch_in_days', chart_type='pie',
        start_date='2026-10-01', end_date='2026-10-31', punch_basis='fixed_time', punch_after='10:30:00')
    text, charts, used = asyncio.run(build_report_chart(service, object(), selected, chart_types=['pie', 'bar']))
    assert used == ['get_manager_report']
    if condition == 'too_many':
        assert len(charts[0]['labels']) == 20
        assert charts[0]['labels'][-1] == 'Other employees (32)'
        assert sum(charts[0]['series'][0]['values']) == 102
        assert 'remaining 32 employees' in charts[0]['note']
    elif condition in ('empty', 'zero', 'truncated'):
        assert not charts
    elif condition == 'unassessable':
        assert [chart['type'] for chart in charts] == ['bar']
        assert charts[0]['series'][0]['values'] == [None]
        assert 'pie chart is unavailable' in text
    else:
        assert [chart['type'] for chart in charts] == ['pie', 'bar']
        assert 'could not be assessed' in charts[0]['note']
    assert 'Punch-in time report' in text
