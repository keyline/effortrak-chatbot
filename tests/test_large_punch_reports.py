import copy
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.report_export import data_tables, pdf_bytes, style_export, workbook_bytes
from app.manager_query import render_report
from tests.test_backend import backend, headers
from tests.test_punch_workbook import punch_snapshot
from tests.test_punch_time import send
from tests.test_semantic_query import enable, request


def large_snapshot(count=123):
    saved = punch_snapshot()
    data = saved['sources'][0]['data']
    base = data['rows'][0]
    data['rows'] = [{**copy.deepcopy(base), 'employee_id': 1000 + i,
        'employee_name': f'Employee {i:03}', 'unassessed_days': 0} for i in range(count)]
    data.update(employee_ids=[row['employee_id'] for row in data['rows']], matched_count=count,
        unassessed_days=0, details_truncated=False)
    for row in data['rows']:
        for entry in row['entries']:
            entry['policy_source'] = 'fixed_time'
    saved['charts'] = []
    saved['text'] = render_report(data)
    return saved


@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_large_company_chat_pages_retain_filters_and_complete_export(backend, platform):
    client, state = backend
    call = enable(client, state, [request(action='late_punchin', subject='company', employee_name=None,
        start_date='2026-10-01', end_date='2026-10-31', punch_basis='fixed_time', punch_after='10:30:00')])
    saved = large_snapshot()
    source = saved['sources'][0]
    service = client.app.state.backend
    original = service.call

    async def backend_call(endpoint, token, body=None):
        if endpoint == 'manager-report':
            from app.report_export import capture
            capture(endpoint, body, source['data'])
            state['last_manager_body'] = body
            return copy.deepcopy(source['data'])
        return await original(endpoint, token, body)

    service.call = backend_call
    first = send(client, 'Show company punch-ins after 10:30 AM in October', platform=platform)
    assert first.status_code == 200, first.text
    cid = first.json()['data']['conversation_id']
    for page, text, expected in [(1, first.json()['data']['reply'], 50)]:
        assert text.count('matching day(s).') == expected
        assert 'Report page: 1 of 3' in text
        assert 'Overall matching employees: 123; overall matching punch-in days: 246' in text
    second = send(client, 'next page', cid, platform)
    assert second.status_code == 200, second.text
    assert 'Employee 050: 2 matching' in second.json()['data']['reply']
    third = send(client, 'next page', cid, platform)
    assert third.status_code == 200, third.text
    assert 'Report page: 3 of 3' in third.json()['data']['reply']
    assert third.json()['data']['reply'].count('matching day(s).') == 23
    assert not third.json()['data']['charts']
    assert state['last_manager_body']['punch_after'] == '10:30:00'
    assert call.await_count == 1
    exported = send(client, 'Download this report in Excel with pie and bar charts', cid, platform)
    assert exported.status_code == 200, exported.text
    artifact = exported.json()['data']['artifacts'][0]
    download = client.get(artifact['download_url'], headers=headers())
    assert download.status_code == 200, download.text
    book = load_workbook(BytesIO(download.content))
    assert book['Report']['B5'].value == 123
    assert book['Report']['B6'].value == 246
    assert book['Punch-in Records'].max_row == 251
    assert 'Employee 122' in [cell.value for row in book['Report'] for cell in row]
    assert sum(book['Chart 1'].cell(row, 2).value for row in range(3, 23)) == 246
    source['data']['rows'][-1]['employee_name'] = 'Changed after export'
    assert client.get(artifact['download_url'], headers=headers()).status_code == 409


def test_large_exports_include_every_employee_and_entry_not_just_chat_page(monkeypatch):
    from reportlab import rl_config
    monkeypatch.setattr(rl_config, 'pageCompression', 0)
    saved = style_export(large_snapshot(175), ['pie', 'bar'])
    # Infer the summary charts through the normal snapshot path.
    from app.report_export import inferred_charts
    saved['charts'] = inferred_charts(saved['sources'])
    saved = style_export(saved, ['pie', 'bar'])
    tables = data_tables(saved)
    assert len(tables) < 10
    payload = pdf_bytes(saved, tables)
    assert b'Employee 174' in payload
    assert payload.count(b'6:25:27 PM') == 175
    assert payload.count(b'12:26:53 PM') == 175
    assert 'Employee 174' not in saved['text']
    book = load_workbook(BytesIO(workbook_bytes(saved, tables)))
    assert book['Report']['B5'].value == 175
    assert book['Punch-in Records'].max_row == 355
    assert len(saved['charts'][0]['labels']) == 20
    assert sum(saved['charts'][0]['series'][0]['values']) == 350


def test_out_of_range_page_is_not_silently_changed_to_page_one():
    text = render_report(large_snapshot()['sources'][0]['data'], page=4)
    assert 'has 3 pages' in text
    assert 'Employee 000' not in text


def test_large_summary_counts_all_matching_employees_not_only_first_page():
    text = render_report(large_snapshot()['sources'][0]['data'], include_names=False)
    assert 'Matching employees: 123 | Matching punch-in days: 246' in text
    assert 'Employee 000' not in text
