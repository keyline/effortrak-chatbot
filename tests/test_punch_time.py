from io import BytesIO
from zipfile import ZipFile
from unittest.mock import patch

import pytest
from openpyxl import load_workbook
from pydantic import ValidationError

from app.schemas import ManagerReportArgs
from app.manager_query import render_report
from app.report_export import requested_chart_styles, style_export, pdf_bytes, data_tables, Pie, VerticalBarChart
from tests.test_backend import backend, headers
from tests.test_report_export import snapshot
from tests.test_semantic_query import enable, request


def filtered_rows(state, basis='shift_start', delay=20):
    cutoff = '11:00:00' if basis == 'fixed_time' else '10:00:00'
    seconds = delay * 60 + 1
    state.update(punch_filter_echo=True, manager_rows=[dict(employee_id=101, employee_name='PRIVATE EMPLOYEE',
        assessable=True, late_after=cutoff, count=1, unassessed_days=0,
        entries=[dict(date='2026-09-01', punch_in='11:00:01' if basis == 'fixed_time' else f'{10 + delay // 60:02d}:{delay % 60:02d}:01',
            late_minutes=seconds // 60, delay_seconds=seconds, cutoff=cutoff, policy_source='fixed_time' if basis == 'fixed_time' else 'saved_snapshot')])])


def send(client, message, conversation_id=None, platform='web'):
    return client.post('/v1/chat', headers=headers(), json={
        'message': message, 'conversation_id': conversation_id, 'client_platform': platform})


@pytest.mark.parametrize('message,basis,delay,after', [
    ('List employees who punched in after 11 AM last month', 'fixed_time', 0, '11:00:00'),
    ('Staff checking in past eleven in the morning last month', 'fixed_time', 0, '11:00:00'),
    ('Show users who punch in after their shift time', 'shift_start', 0, None),
    ('Who arrived over twenty minutes after shift start?', 'shift_start', 20, None),
    ('List empolyees punching in after 20 min of theri shift', 'shift_start', 20, None),
    ('Employees late by the configured cutoff', 'late_cutoff', 0, None),
])
@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_semantic_filter_contract_and_private_report_rendering(backend, message, basis, delay, after, platform):
    client, state = backend
    call = enable(client, state, [request(action='late_punchin', subject='company', employee_name=None,
        punch_basis=basis, punch_after=after, min_delay_minutes=delay)])
    filtered_rows(state, basis, delay)
    result = send(client, message, platform=platform)
    assert result.status_code == 200, result.text
    body = state['last_manager_body']
    assert (body['punch_basis'], body['punch_after'], body['min_delay_minutes'], body['threshold']) == (basis, after, delay, 0)
    assert 'Matching employees: 1' in result.json()['data']['reply']
    assert 'Reference:' in result.json()['data']['reply']
    assert 'PRIVATE EMPLOYEE' not in str(call.call_args.kwargs)


@pytest.mark.parametrize('format', ['pdf', 'xlsx'])
@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_direct_filtered_export_includes_requested_chart_types(backend, format, platform):
    client, state = backend
    enable(client, state, [request(action='late_punchin', subject='company', employee_name=None,
        punch_basis='shift_start', min_delay_minutes=20, chart_type='bar')])
    filtered_rows(state)
    result = send(client, f'Export employees punching in after 20 minutes of their shift in {format} with pie chart and bar chart', platform=platform)
    assert result.status_code == 200, result.text
    data = result.json()['data']
    assert len(data['artifacts']) == 1
    download = client.get(data['artifacts'][0]['download_url'], headers=headers())
    assert download.status_code == 200
    if format == 'xlsx':
        book = load_workbook(BytesIO(download.content))
        assert book['Chart 1']['B3'].value == book['Chart 2']['B3'].value == 1
        with ZipFile(BytesIO(download.content)) as archive:
            charts = [archive.read(name) for name in archive.namelist() if name.startswith('xl/charts/chart') and name.endswith('.xml')]
        assert len(charts) == 2
        assert any(b'pieChart' in xml for xml in charts)
        assert any(b'barChart' in xml for xml in charts)
    else:
        assert download.content.startswith(b'%PDF-')
    assert state['last_manager_body']['min_delay_minutes'] == 20


def test_generic_export_can_change_chart_without_a_new_model_call(backend):
    client, state = backend
    call = enable(client, state, [request(action='late_punchin', subject='company', employee_name=None,
        punch_basis='fixed_time', punch_after='11:00:00', min_delay_minutes=0)])
    filtered_rows(state, 'fixed_time', 0)
    first = send(client, 'List employees after 11 am in September').json()['data']
    result = send(client, 'Download this report in Excel along with pic chart', first['conversation_id'])
    assert result.status_code == 200, result.text
    file = result.json()['data']['artifacts'][0]
    book = load_workbook(BytesIO(client.get(file['download_url'], headers=headers()).content))
    assert book['Chart 1']._charts[0].__class__.__name__ == 'PieChart'
    assert call.await_count == 1
    assert state['last_manager_body']['punch_after'] == '11:00:00'


def test_clock_followup_replaces_prior_delay_filter(backend):
    client, state = backend
    enable(client, state, [request(action='late_punchin', punch_basis='shift_start', min_delay_minutes=20),
        request(action='late_punchin', subject='previous', employee_name=None, start_date=None, end_date=None,
            use_previous=True, punch_basis='fixed_time', punch_after='11:00:00')])
    filtered_rows(state)
    first = send(client, 'Joydeep more than 20 minutes after shift in September').json()['data']
    filtered_rows(state, 'fixed_time', 0)
    second = send(client, 'Instead after 11 AM', first['conversation_id'])
    assert second.status_code == 200, second.text
    assert state['last_manager_body']['min_delay_minutes'] == 0
    assert state['last_manager_body']['start_date'] == '2026-09-01'
    assert state['last_manager_body']['employee_id'] == 101


def test_old_backend_cannot_silently_ignore_new_filters(backend):
    client, state = backend
    enable(client, state, [request(action='late_punchin', punch_basis='shift_start', min_delay_minutes=20)])
    result = send(client, 'Joydeep over 20 minutes after shift in September')
    assert result.status_code == 502
    assert 'Update the web backend' in result.json()['message']


@pytest.mark.parametrize('changes', [dict(punch_basis='fixed_time'),
    dict(punch_basis='fixed_time', punch_after='25:00:00'),
    dict(punch_basis='fixed_time', punch_after='11:00:00', min_delay_minutes=20),
    dict(punch_after='11:00:00'), dict(min_delay_minutes=-1),
    dict(report='late', punch_basis='shift_start')])
def test_filter_arguments_are_strictly_validated(changes):
    with pytest.raises(ValidationError):
        ManagerReportArgs(scope='self', **{**dict(report='late_punchin', start_date='2026-09-01', end_date='2026-09-30'), **changes})


def test_chart_style_selection_does_not_mutate_original_and_handles_invalid_pie():
    saved = snapshot()
    result = style_export(saved, ['pie', 'bar'])
    assert [chart['type'] for chart in result['charts']] == ['pie', 'bar']
    assert saved['charts'][0]['type'] == 'bar'
    saved['charts'][0]['series'][0]['values'] = [0, 0]
    result = style_export(saved, ['pie'])
    assert not result['charts']
    assert 'could not be included' in result['text']
    assert requested_chart_styles('attach a pic chart and a bar graph') == ['pie', 'bar']
    assert requested_chart_styles('pie and bar charts') == ['pie', 'bar']


def test_requested_chart_survives_export_format_clarification(backend):
    client, state = backend
    enable(client, state, [request(action='late_punchin', punch_basis='shift_start', min_delay_minutes=20)])
    filtered_rows(state)
    first = send(client, 'Joydeep after shift by more than 20 minutes in September').json()['data']
    pending = send(client, 'Export this report with a pie chart', first['conversation_id'])
    assert 'Which export format' in pending.json()['data']['reply']
    result = send(client, 'Excel', first['conversation_id'])
    assert result.status_code == 200
    file = result.json()['data']['artifacts'][0]
    book = load_workbook(BytesIO(client.get(file['download_url'], headers=headers()).content))
    assert book['Chart 1']._charts[0].__class__.__name__ == 'PieChart'


def test_pdf_really_renders_both_chart_types():
    saved = style_export(snapshot(), ['pie', 'bar'])
    with patch('app.report_export.Pie', wraps=Pie) as pie, patch('app.report_export.VerticalBarChart', wraps=VerticalBarChart) as bar:
        payload = pdf_bytes(saved, data_tables(saved))
    assert payload.startswith(b'%PDF-')
    pie.assert_called_once()
    bar.assert_called_once()


@pytest.mark.parametrize('day', ['2026-10-15', '2026-09-20'])
def test_explicit_single_date_is_preserved(backend, day):
    client, state = backend
    enable(client, state, [request(action='late_punchin', subject='company', employee_name=None,
        start_date=day, end_date=day, period_kind='day', punch_basis='shift_start', min_delay_minutes=0)])
    filtered_rows(state, 'shift_start', 0)
    state['manager_rows'][0]['entries'][0]['date'] = day
    result = send(client, f'Employees punching in after shift on {day}')
    assert result.status_code == 200, result.text
    assert state['last_manager_body']['start_date'] == state['last_manager_body']['end_date'] == day


@pytest.mark.parametrize('change', ['delay', 'cutoff', 'employee'])
def test_invalid_filtered_data_fails_closed(backend, change):
    client, state = backend
    enable(client, state, [request(action='late_punchin', punch_basis='fixed_time', punch_after='11:00:00', min_delay_minutes=0)])
    filtered_rows(state, 'fixed_time', 0)
    row = state['manager_rows'][0]
    if change == 'delay': row['entries'][0]['delay_seconds'] = 0
    elif change == 'cutoff': row['entries'][0]['cutoff'] = '12:00:00'
    else: row['employee_id'] = 201
    result = send(client, 'Joydeep after 11 AM in September')
    assert result.status_code == 502


def test_unassessed_data_does_not_claim_a_missing_cutoff_is_the_only_cause():
    data = dict(report='late_punchin', start_date='2026-09-01', end_date='2026-09-01',
        punch_filter=dict(punch_basis='late_cutoff', punch_after=None, min_delay_minutes=0),
        rows=[dict(employee_name='Employee', count=0, assessable=False, late_after=None, entries=[], unassessed_days=1)],
        unassessed_days=1, details_truncated=False, notice='Valid comparison times were unavailable.')
    result = render_report(data)
    assert 'Unassessed punch-in days: 1' in result
    assert 'no late cutoff is configured' not in result
