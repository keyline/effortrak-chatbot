import asyncio
import copy
import json
import os
from pathlib import Path
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook

from app.auth import Identity
from app.report_export import (READ_ENDPOINTS, authorize, data_tables, export_snapshot,
    make_snapshot, pdf_bytes, workbook_bytes)
from app.task_report import AssistantReply
from tests.test_backend import backend, headers
from tests.test_leave_balance_query import response
from tests.test_semantic_query import enable, request


def actor():
    return Identity(1, 101, 'Reader', 'ADMIN', ('self', 'company'), True, 'effortrak_backend', 'a' * 64, 'test-token', ('attendance', 'tasks', 'leave'))


def chart(kind='bar'):
    return dict(type=kind, title='Recorded hours <not markup>', unit='hours', labels=['One', 'Two'],
        series=[dict(name='Booked effort', values=[1.5, 2.25])], note='Missing values are unavailable, not zero.')


def snapshot(endpoint='manager-report', kind='bar'):
    source = dict(endpoint=endpoint, arguments=dict(scope='self'), data=dict(scope='self',
        rows=[dict(employee_id=101, employee_name='=HYPERLINK("https://invalid.example")',
                   total=3.75, missing=None, location=dict(coordinates=[22.5, 88.3]))],
        notice='Complete requested report.'))
    if endpoint in ('daily-tasks', 'project-tasks', 'visit-location'):
        source['arguments']['page'] = 1
        source['data'].update(page=1, has_more=False)
    return make_snapshot(AssistantReply('Displayed report\nOnly the returned page is included.', charts=[chart(kind)]), [source], actor())


@pytest.mark.parametrize('endpoint', sorted(READ_ENDPOINTS))
@pytest.mark.parametrize('format', ['pdf', 'xlsx'])
def test_all_report_sources_export_bounded_authorized_data(endpoint, format):
    saved = snapshot(endpoint)
    service = SimpleNamespace(revalidate=AsyncMock(), call=AsyncMock(return_value=saved['sources'][0]['data']))
    reply, tools, files = asyncio.run(export_snapshot(saved, format, actor(), service, {'kind': 'report'}))
    assert tools == ['export_report']
    assert reply.export_snapshot == saved
    assert files[0]['payload'].startswith(b'%PDF-' if format == 'pdf' else b'PK')
    assert 'complete report selection' in reply and 'All matching pages' in reply
    service.revalidate.assert_awaited_once()
    service.call.assert_awaited_once_with(endpoint, 'test-token', saved['sources'][0]['arguments'])


@pytest.mark.parametrize('kind', ['pie', 'doughnut', 'bar', 'line', 'table'])
def test_chart_files_have_the_same_numeric_values_and_editable_excel_chart(kind):
    saved = snapshot(kind=kind)
    xlsx = workbook_bytes(saved, data_tables(saved))
    book = load_workbook(BytesIO(xlsx))
    assert book['Chart 1']['B3'].value == 1.5
    assert book['Chart 1']['B4'].value == 2.25
    assert len(book['Chart 1']._charts) == (0 if kind == 'table' else 1)
    assert pdf_bytes(saved, data_tables(saved)).startswith(b'%PDF-')
    with ZipFile(BytesIO(xlsx)) as archive:
        charts = [name for name in archive.namelist() if name.startswith('xl/charts/chart') and name.endswith('.xml')]
        assert len(charts) == (0 if kind == 'table' else 1)


def test_missing_values_remain_missing_in_line_charts():
    saved = snapshot(kind='line')
    saved['charts'][0]['series'][0]['values'][1] = None
    book = load_workbook(BytesIO(workbook_bytes(saved, data_tables(saved))))
    assert book['Chart 1']['B4'].value is None
    assert book['Chart 1']._charts[0].display_blanks == 'gap'
    assert pdf_bytes(saved, data_tables(saved)).startswith(b'%PDF-')


def test_formula_injection_is_neutralized_and_coordinates_are_retained():
    saved = snapshot()
    tables = data_tables(saved)
    book = load_workbook(BytesIO(workbook_bytes(saved, tables)))
    values = [cell for sheet in book for row in sheet for cell in row]
    assert not any(cell.data_type == 'f' for cell in values)
    assert any(str(cell.value).startswith("'=HYPERLINK") for cell in values)
    assert any(cell.value == '[22.5, 88.3]' for cell in values)
    assert any(cell.value == 3.75 for cell in values)


@pytest.mark.parametrize('change', ['company', 'user', 'policy', 'endpoint', 'data'])
def test_changed_or_foreign_report_cannot_export(change):
    saved = snapshot()
    current = copy.deepcopy(saved['sources'][0]['data'])
    if change == 'company': saved['company_id'] = 2
    elif change == 'user': saved['user_id'] = 202
    elif change == 'policy': saved['policy_key'] = 'changed'
    elif change == 'endpoint': saved['sources'][0]['endpoint'] = 'delete-employee'
    else: current['rows'][0]['total'] = 99
    service = SimpleNamespace(revalidate=AsyncMock(), call=AsyncMock(return_value=current))
    with pytest.raises(HTTPException):
        asyncio.run(authorize(saved, actor(), service))


def test_metadata_clock_change_does_not_invalidate_identical_report_data():
    saved = snapshot()
    current = copy.deepcopy(saved['sources'][0]['data'])
    current['as_of'] = '2026-11-04 12:00:00'
    service = SimpleNamespace(revalidate=AsyncMock(), call=AsyncMock(return_value=current))
    asyncio.run(authorize(saved, actor(), service))


@pytest.mark.parametrize('format', ['pdf', 'xlsx'])
@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_real_chat_exports_and_downloads_leave_report_on_both_clients(backend, format, platform):
    client, state = backend
    state['balance_data'] = response()
    create = enable(client, state, [])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show employees with highest leave balance', 'client_platform': platform})
    assert first.status_code == 200
    cid = first.json()['data']['conversation_id']
    exported = client.post('/v1/chat', headers=headers(), json={'message': f'Can you give me this report in {format} format?', 'conversation_id': cid, 'client_platform': platform})
    assert exported.status_code == 200
    data = exported.json()['data']
    assert data['tools_used'] == ['export_report']
    url = data['artifacts'][0]['download_url']
    downloaded = client.get(url, headers=headers())
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b'%PDF-' if format == 'pdf' else b'PK')
    assert client.get(url, headers=headers(2)).status_code == 404
    history = client.get(f'/v1/conversations/{cid}', headers=headers()).json()['data']
    assert not any('export_snapshot' in row for row in history['messages'])
    create.assert_not_awaited()
    if format == 'xlsx':
        book = load_workbook(BytesIO(downloaded.content))
        assert book['Chart 1']['B3'].value == 15.5
    state['balance_denied'] = True
    assert client.get(url, headers=headers()).status_code == 403


def test_changed_data_requires_a_fresh_report_on_download(backend):
    client, state = backend
    state.update(modules=['leave'], scopes=['self', 'company'], balance_data=response())
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Highest leave balances'}).json()['data']
    exported = client.post('/v1/chat', headers=headers(), json={'message': 'Excel please', 'conversation_id': first['conversation_id']}).json()['data']
    state['balance_data']['groups'][0]['rows'][0].update(used_days=5, remaining_days=15)
    assert client.get(exported['artifacts'][0]['download_url'], headers=headers()).status_code == 409


def test_no_export_permission_does_not_create_a_file(backend):
    client, state = backend
    state.update(modules=['leave'], scopes=['self', 'company'], balance_data=response(), export=False)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Highest leave balances'}).json()['data']
    exported = client.post('/v1/chat', headers=headers(), json={'message': 'Export this as PDF', 'conversation_id': first['conversation_id']})
    assert exported.status_code == 403
    with client.app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM reports').fetchone()[0] == 0


def test_booked_effort_chart_exports_without_reinterpreting_as_attendance(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', subject='self', employee_name=None,
        chart_type='pie', chart_metric='booked_effort')])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show my booked effort pie chart for September 2026'}).json()['data']
    assert first['charts'][0]['series'][0]['values'] == [1, .5]
    exported = client.post('/v1/chat', headers=headers(), json={'message': 'Can you give me this report in Excel format?', 'conversation_id': first['conversation_id']})
    assert exported.status_code == 200
    data = exported.json()['data']
    download = client.get(data['artifacts'][0]['download_url'], headers=headers())
    book = load_workbook(BytesIO(download.content))
    assert [book['Chart 1']['B3'].value, book['Chart 1']['B4'].value] == [1, .5]
    assert create.await_count == 1
    assert 'export' not in state['calls']


def test_missing_snapshot_requests_report_instead_of_inventing_a_file(backend):
    client, _ = backend
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Export this report as Excel'})
    assert result.status_code == 200
    assert result.json()['data']['artifacts'] == []
    assert 'display the report first' in result.json()['data']['reply']


def test_generated_files_for_visual_review(tmp_path):
    saved = snapshot(kind='pie')
    folder = Path(os.environ.get('REPORT_EXPORT_PREVIEW_DIR', str(tmp_path)))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'report-export.pdf').write_bytes(pdf_bytes(saved, data_tables(saved)))
    (folder / 'report-export.xlsx').write_bytes(workbook_bytes(saved, data_tables(saved)))


@pytest.mark.parametrize('action', ['late_punchin', 'late', 'missing_punchout', 'present', 'absent',
    'work_arrangements', 'locations', 'missing_location', 'effort_by_employee', 'effort_by_project',
    'task_overruns', 'workload', 'approved_leave', 'attendance_totals', 'booked_time'])
def test_each_manager_report_can_export_its_displayed_result(backend, action):
    client, state = backend
    create = enable(client, state, [request(action=action, subject='self', employee_name=None,
        start_date=None if action == 'work_arrangements' else '2026-09-01',
        end_date=None if action == 'work_arrangements' else '2026-09-30', period_kind='none' if action == 'work_arrangements' else 'range')])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Authorized calendar metrics report'})
    assert first.status_code == 200
    cid = first.json()['data']['conversation_id']
    exported = client.post('/v1/chat', headers=headers(), json={'message': 'Export this report as Excel', 'conversation_id': cid})
    assert exported.status_code == 200, exported.text
    assert exported.json()['data']['artifacts']
    assert create.await_count == 1


@pytest.mark.parametrize('message', ['Show my tasks for 01/10/2026', 'Show my tasks for 2026-09'])
def test_task_reports_export_with_descriptions_remaining_local(backend, message):
    client, state = backend
    state['modules'] = ['tasks']
    first = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert first.status_code == 200
    cid = first.json()['data']['conversation_id']
    exported = client.post('/v1/chat', headers=headers(), json={'message': 'Download this report as PDF', 'conversation_id': cid})
    assert exported.status_code == 200, exported.text
    assert exported.json()['data']['artifacts']


def test_project_task_report_exports_with_existing_project_visibility(backend):
    from tests.test_project_tasks import setup_project
    client, state = backend
    create = setup_project(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show project IIHM tasks for 2026-09'}).json()['data']
    exported = client.post('/v1/chat', headers=headers(), json={'message': 'Excel please', 'conversation_id': first['conversation_id']})
    assert exported.status_code == 200
    assert exported.json()['data']['artifacts']
    create.assert_not_awaited()


def test_visit_report_exports_without_exporting_a_different_report(backend):
    client, state = backend
    state.update(modules=['visits'], scopes=['self', 'company'])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Joydeep Ghosh last visited client?'})
    assert first.status_code == 200
    exported = client.post('/v1/chat', headers=headers(), json={'message': 'PDF please', 'conversation_id': first.json()['data']['conversation_id']})
    assert exported.status_code == 200
    assert exported.json()['data']['tools_used'] == ['export_report']
    assert 'attendance' not in state['calls']


@pytest.mark.parametrize('action,message', [('work_summary', 'Show Joydeep work summary for 2026-09-01'),
    ('work_review', 'Show Joydeep detailed 30 day review for 2026-09-01')])
def test_work_reports_keep_their_data_and_export_without_model_followup(backend, action, message):
    from tests.test_work_report import question
    client, state = backend
    create = enable(client, state, [question(action=action)])
    first = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert first.status_code == 200, first.text
    exported = client.post('/v1/chat', headers=headers(), json={'message': 'Make that a PDF',
        'conversation_id': first.json()['data']['conversation_id']})
    assert exported.status_code == 200, exported.text
    assert exported.json()['data']['artifacts']
    assert create.await_count == 1


def test_direct_booked_effort_export_does_not_use_attendance_export(backend):
    client, state = backend
    enable(client, state, [request(action='booked_time', subject='self', employee_name=None, format='xlsx')])
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Export my booked effort for September 2026 as Excel'})
    assert result.status_code == 200, result.text
    assert result.json()['data']['artifacts']
    assert 'export' not in state['calls']


def test_export_format_clarification_preserves_report(backend):
    client, state = backend
    enable(client, state, [request(action='booked_time', subject='self', employee_name=None)])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show my booked effort for September 2026'}).json()['data']
    clarify = client.post('/v1/chat', headers=headers(), json={'message': 'Export this report', 'conversation_id': first['conversation_id']})
    assert 'PDF or Excel' in clarify.json()['data']['reply']
    result = client.post('/v1/chat', headers=headers(), json={'message': 'Excel please', 'conversation_id': first['conversation_id']})
    assert result.status_code == 200 and result.json()['data']['artifacts']
