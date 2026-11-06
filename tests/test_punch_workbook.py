from datetime import datetime, time, timedelta
from io import BytesIO

from openpyxl import load_workbook

from app.report_export import make_snapshot, data_tables, workbook_bytes, style_export
from app.task_report import AssistantReply
from tests.test_report_export import actor


def punch_snapshot(empty=False, truncated=False):
    rows = [] if empty else [dict(employee_id=101, employee_name='Somnath Shil', count=2,
        assessable=True, late_after='10:30:00', unassessed_days=1, entries=[
            dict(date='2026-10-07', punch_in='18:25:27', cutoff='10:30:00', late_minutes=475,
                delay_seconds=28527, policy_source='fixed_time'),
            dict(date='2026-10-08', punch_in='12:26:53', cutoff='10:30:00', late_minutes=116,
                delay_seconds=7013, policy_source='reconstructed_snapshot')])]
    args = dict(report='late_punchin', scope='company', employee_id=None, start_date='2026-10-01', end_date='2026-10-31',
        punch_basis='fixed_time', punch_after='10:30:00', min_delay_minutes=0, threshold=0)
    data = dict(data_source='effortrak_backend', scope='company', report='late_punchin',
        start_date=args['start_date'], end_date=args['end_date'], employee_ids=[101], rows=rows,
        punch_filter={key: args[key] for key in ('punch_basis', 'punch_after', 'min_delay_minutes')},
        unassessed_days=1 if rows else 0, matched_count=10 if truncated else len(rows),
        details_truncated=truncated, notice='Reconstructed shift settings are estimates, not verified historical assignments.')
    return make_snapshot(AssistantReply('Original chat answer\nAll source notes must remain available.'),
        [dict(endpoint='manager-report', arguments=args, data=data)], actor())


def book(saved):
    return load_workbook(BytesIO(workbook_bytes(saved, data_tables(saved))))


def test_punch_export_opens_on_readable_summary_and_single_record_table():
    saved = punch_snapshot()
    workbook = book(saved)
    visible = [sheet.title for sheet in workbook if sheet.sheet_state == 'visible']
    assert visible == ['Report', 'Punch-in Records', 'Notes', 'Chart 1']
    assert workbook.active.title == 'Report'
    assert workbook['Report']['A1'].value == 'Punch-In Time Report'
    assert workbook['Report']['B5'].value == 1
    assert workbook['Report']['B6'].value == 2
    assert workbook['Report']['D10'].value == 1
    assert workbook['Report']['A10'].value == 'Somnath Shil'
    assert workbook['Report']['B10'].value == 2
    assert workbook['Report']['C10'].value == time(10, 30)
    assert 'Original chat answer' not in str([cell.value for row in workbook['Report'] for cell in row])
    records = workbook['Punch-in Records']
    assert records.max_row == 7
    assert [cell.value for cell in records[5]] == ['Employee', 'Date', 'Punch-in time', 'Reference time', 'Delay (h:mm:ss)', 'Data quality', 'Employee ID']
    assert records['B6'].value == datetime(2026, 10, 7)
    assert records['C6'].value == time(18, 25, 27)
    assert records['D6'].value == time(10, 30)
    assert records['E6'].value == timedelta(seconds=28527)
    assert records['F7'].value == 'Reconstructed shift (estimate)'
    assert records['G6'].value == 101
    assert records.freeze_panes == 'B6'
    assert records.tables['PunchRecords'].ref == 'A5:G7'
    assert workbook['Report'].tables['PunchEmployees'].ref == 'A9:F10'
    assert workbook['Original Reply'].sheet_state == 'hidden'
    assert all(sheet.sheet_state == 'hidden' for sheet in workbook if sheet.title.startswith('Data '))
    assert workbook['Original Reply']['A2'].value == 'All source notes must remain available.'


def test_readable_workbook_keeps_both_charts_and_all_original_source_values():
    saved = style_export(punch_snapshot(), ['pie', 'bar'])
    workbook = book(saved)
    assert workbook['Chart 1']._charts[0].__class__.__name__ == 'PieChart'
    assert workbook['Chart 2']._charts[0].__class__.__name__ == 'BarChart'
    assert workbook['Chart 1']['B3'].value == workbook['Chart 2']['B3'].value == 2
    raw_values = [cell.value for sheet in workbook if sheet.title.startswith('Data ') for row in sheet for cell in row]
    assert 28527 in raw_values
    assert '2026-10-07' in raw_values


def test_empty_and_truncated_workbooks_explain_coverage_without_inventing_rows():
    empty = book(punch_snapshot(empty=True))
    assert empty['Report']['B5'].value == empty['Report']['B6'].value == 0
    assert not empty['Punch-in Records'].tables
    assert empty['Punch-in Records']['A6'].value == 'No matching records in this report.'
    truncated = book(punch_snapshot(truncated=True))
    assert 'Showing 1 of 10' in truncated['Report']['A7'].value
    assert 'truncated' in truncated['Report']['A7'].value


def test_employee_names_cannot_become_excel_formulas():
    saved = punch_snapshot()
    saved['sources'][0]['data']['rows'][0]['employee_name'] = '=HYPERLINK("https://invalid.example")'
    workbook = book(saved)
    assert workbook['Report']['A10'].value.startswith("'=")
    assert workbook['Punch-in Records']['A6'].value.startswith("'=")
    assert not any(cell.data_type == 'f' for sheet in workbook for row in sheet for cell in row)


def test_varied_references_and_shift_delay_are_not_labelled_as_one_fixed_time():
    saved = punch_snapshot()
    source = saved['sources'][0]
    source['data']['punch_filter'].update(punch_basis='shift_start', punch_after=None, min_delay_minutes=20)
    source['data']['rows'][0]['entries'][1]['cutoff'] = '10:00:00'
    workbook = book(saved)
    assert workbook['Report']['A3'].value == 'More than 20 minutes after shift start'
    assert workbook['Report']['C10'].value == 'Varies by date'
    assert workbook['Punch-in Records']['D7'].value == time(10)


def test_compact_columns_hide_redundant_metadata_but_never_estimates():
    saved = punch_snapshot()
    workbook = book(saved)
    assert all(workbook['Report'].column_dimensions[key].hidden for key in ('C', 'E', 'F'))
    assert not workbook['Report'].column_dimensions['D'].hidden
    records = workbook['Punch-in Records']
    assert records.column_dimensions['D'].hidden
    assert records.column_dimensions['G'].hidden
    assert not records.column_dimensions['F'].hidden
    assert records['E6'].number_format == '[h]:mm:ss'
    assert 'estimated' in workbook['Report']['A4'].value
    data = saved['sources'][0]['data']
    data['unassessed_days'] = data['rows'][0]['unassessed_days'] = 0
    for entry in data['rows'][0]['entries']:
        entry['policy_source'] = 'fixed_time'
    workbook = book(saved)
    assert workbook['Report'].column_dimensions['D'].hidden
    assert workbook['Punch-in Records'].column_dimensions['F'].hidden
    assert workbook['Report']['A3'].value == 'Strictly after 10:30 AM'


def test_compact_pdf_contains_exact_records_warnings_and_no_chat_duplication(monkeypatch):
    from reportlab import rl_config
    from app.report_export import pdf_bytes
    monkeypatch.setattr(rl_config, 'pageCompression', 0)
    saved = punch_snapshot(truncated=True)
    payload = pdf_bytes(saved, data_tables(saved))
    assert payload.startswith(b'%PDF')
    for text in (b'Employee Summary', b'Date-Wise Records', b'Somnath Shil', b'07 Oct 2026',
            b'6:25:27 PM', b'7:55:27', b'1:56:53', b'Partial report', b'could not be assessed',
            b'Reconstructed shift', b'not verified historical assignments'):
        assert text in payload
    assert b'Original chat answer' not in payload
    assert b'Displayed Report and Notices' not in payload
    assert b'delay_seconds' not in payload


def test_compact_pdf_empty_and_long_reports_keep_all_records(monkeypatch):
    import copy
    from reportlab import rl_config
    from app.report_export import pdf_bytes
    monkeypatch.setattr(rl_config, 'pageCompression', 0)
    empty = punch_snapshot(empty=True)
    assert b'No matching punch-in records.' in pdf_bytes(empty, data_tables(empty))
    saved = punch_snapshot()
    data = saved['sources'][0]['data']
    base = data['rows'][0]
    data['rows'] = [copy.deepcopy(base) for _ in range(60)]
    for index, row in enumerate(data['rows']):
        row['employee_id'] = 101 + index
        row['employee_name'] = f'Long Employee Name With Multiple Components {index:03}'
    data['matched_count'] = 60
    payload = pdf_bytes(saved, data_tables(saved))
    assert b'Long Employee Name With Multiple Components 059' in payload
    assert payload.count(b'6:25:27 PM') == 60
    assert payload.count(b'12:26:53 PM') == 60
    assert payload.count(b'/Type /Page\n') > 1


def test_compact_pdf_draws_requested_pie_and_bar_without_repeating_raw_tables(monkeypatch):
    from unittest.mock import patch
    from reportlab import rl_config
    from app import punch_pdf
    from app.report_export import pdf_bytes
    monkeypatch.setattr(rl_config, 'pageCompression', 0)
    saved = style_export(punch_snapshot(), ['pie', 'bar'])
    with patch.object(punch_pdf, 'Pie', wraps=punch_pdf.Pie) as pie, patch.object(
            punch_pdf, 'VerticalBarChart', wraps=punch_pdf.VerticalBarChart) as bar:
        payload = pdf_bytes(saved, data_tables(saved))
    assert pie.call_count == bar.call_count == 1
    assert b'Numbers refer to Employee Summary rows.' in payload
    assert b'Original chat answer' not in payload
