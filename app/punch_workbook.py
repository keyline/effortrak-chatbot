"""Readable Excel presentation for authorized punch-in report snapshots."""

import math
from datetime import date, time, timedelta

from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo


POLICY_LABELS = {
    'fixed_time': 'Fixed clock cutoff',
    'saved_snapshot': 'Saved daily shift',
    'reconstructed_snapshot': 'Reconstructed shift (estimate)',
    'current_configuration': 'Current shift (estimate)',
}


def _clock(value):
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _filter_label(filters):
    basis = filters.get('punch_basis', 'late_cutoff')
    clock = _clock(filters.get('punch_after'))
    reference = (clock.strftime('%I:%M:%S %p' if clock.second else '%I:%M %p').lstrip('0') if basis == 'fixed_time' and clock
                 else 'shift start' if basis == 'shift_start' else 'the configured late cutoff')
    delay = filters.get('min_delay_minutes', 0)
    return f'More than {delay} minutes after {reference}' if delay else f'Strictly after {reference}'


def _layout(sheet, widths):
    sheet.sheet_view.showGridLines = False
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0


def _band(sheet, row, text, columns, safe_cell, title=False):
    sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=columns)
    cell = sheet.cell(row, 1, safe_cell(text))
    cell.font = Font(name='Calibri', size=14 if title else 11, bold=title, color='FFFFFF' if title else '334155')
    cell.alignment = Alignment(wrap_text=True, vertical='center')
    if title:
        cell.fill = PatternFill('solid', fgColor='176B61')
    sheet.row_dimensions[row].height = 26 if title else 24


def _table(sheet, header_row, headings, records, name, safe_cell):
    for column, heading in enumerate(headings, 1):
        cell = sheet.cell(header_row, column, heading)
        cell.font = Font(name='Calibri', bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='315E7E')
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    sheet.row_dimensions[header_row].height = 28
    for offset, record in enumerate(records, header_row + 1):
        height = 22
        for column, value in enumerate(record, 1):
            cell = sheet.cell(offset, column, safe_cell(value))
            cell.font = Font(name='Calibri', size=11)
            cell.alignment = Alignment(wrap_text=True, vertical='top')
            if isinstance(value, date):
                cell.number_format = 'dd mmm yyyy'
            elif isinstance(value, time):
                cell.number_format = 'h:mm:ss AM/PM' if value.second else 'h:mm AM/PM'
            elif isinstance(value, timedelta):
                cell.number_format = '[h]:mm:ss'
            elif isinstance(value, int):
                cell.number_format = '0'
            if isinstance(value, str):
                width = sheet.column_dimensions[get_column_letter(column)].width
                if not sheet.column_dimensions[get_column_letter(column)].hidden:
                    height = max(height, min(400, math.ceil(len(value) / max(8, width - 3)) * 14 + 6))
        sheet.row_dimensions[offset].height = height
    if records:
        table = Table(displayName=name, ref=f'A{header_row}:{get_column_letter(len(headings))}{header_row + len(records)}')
        table.tableStyleInfo = TableStyleInfo(name='TableStyleMedium2', showRowStripes=True)
        sheet.add_table(table)
    else:
        sheet.cell(header_row + 1, 1, 'No matching records in this report.')
    sheet.freeze_panes = f'B{header_row + 1}'
    sheet.print_title_rows = f'1:{header_row}'


def build_punch_workbook(workbook, snapshot, safe_cell):
    sources = snapshot.get('sources', [])
    if len(sources) != 1 or sources[0]['endpoint'] != 'manager-report' or sources[0]['data'].get('report') != 'late_punchin':
        return False
    source = sources[0]
    data = source['data']
    rows = data['rows']
    filters = data.get('punch_filter') or source.get('arguments', {})
    label = _filter_label(filters)
    start, end = date.fromisoformat(data['start_date']), date.fromisoformat(data['end_date'])
    period = f'{start:%d %b %Y} to {end:%d %b %Y}'
    count = sum(row['count'] for row in rows)
    unassessed = data.get('unassessed_days', sum(row.get('unassessed_days', 0) for row in rows))
    report = workbook.active
    report.title = 'Report'
    _layout(report, [42, 23, 23, 23, 41, 14])
    for column in ('C', 'E', 'F'):
        report.column_dimensions[column].hidden = True
    report.column_dimensions['D'].hidden = not unassessed
    span = 4 if unassessed else 2
    _band(report, 1, 'Punch-In Time Report', span, safe_cell, title=True)
    _band(report, 2, period, span, safe_cell)
    _band(report, 3, label, span, safe_cell)
    for row, column, title, value in [(5, 1, 'Matching employees', sum(item['count'] > 0 for item in rows)),
            (6, 1, 'Matching punch-in days', count)]:
        report.cell(row, column, title).alignment = Alignment(wrap_text=True, vertical='center')
        cell = report.cell(row, column + 1, value)
        cell.font = Font(name='Calibri', size=12, bold=True, color='176B61')
        report.row_dimensions[row].height = 24
    coverage = f"Showing {len(rows)} of {data['matched_count']} employee rows."
    if unassessed:
        coverage += f' {unassessed} punch-in days could not be assessed; these are not counted as on-time.'
    if data['details_truncated']:
        coverage += ' Results are truncated; narrow the selection for a complete report.'
    _band(report, 7, coverage, span, safe_cell)
    report.row_dimensions[7].height = 42
    employees, records = [], []
    for employee in rows:
        references = {entry.get('cutoff', employee.get('late_after')) for entry in employee['entries']}
        reference = (_clock(next(iter(references))) if len(references) == 1 else
                     'Varies by date' if references else _clock(employee.get('late_after')) or 'Unavailable')
        policies = list(dict.fromkeys(POLICY_LABELS.get(entry.get('policy_source'), 'Not specified') for entry in employee['entries']))
        employees.append([employee['employee_name'], employee['count'], reference,
            employee.get('unassessed_days', 0), '; '.join(policies) or 'No matching records', employee['employee_id']])
        for entry in employee['entries']:
            records.append([employee['employee_name'], date.fromisoformat(entry['date']),
                _clock(entry['punch_in']), _clock(entry.get('cutoff', employee.get('late_after'))),
                timedelta(seconds=entry.get('delay_seconds', entry['late_minutes'] * 60)),
                POLICY_LABELS.get(entry.get('policy_source'), 'Not specified'), employee['employee_id']])
    _table(report, 9, ['Employee', 'Matching days', 'Reference time', 'Unassessed days', 'Policy source', 'Employee ID'], employees, 'PunchEmployees', safe_cell)
    detail = workbook.create_sheet('Punch-in Records')
    _layout(detail, [34, 17, 20, 20, 20, 36, 14])
    shared_reference = filters.get('punch_basis') == 'fixed_time' and all(
        record[3] == _clock(filters.get('punch_after')) for record in records)
    quality_warning = any(record[5] not in ('Fixed clock cutoff', 'Saved daily shift') for record in records)
    detail.column_dimensions['D'].hidden = shared_reference
    detail.column_dimensions['F'].hidden = not quality_warning
    detail.column_dimensions['G'].hidden = True
    detail_span = 6 if quality_warning else 5
    _band(detail, 1, 'Date-Wise Punch-In Records', detail_span, safe_cell, title=True)
    _band(detail, 2, period, detail_span, safe_cell)
    _band(detail, 3, label, detail_span, safe_cell)
    if quality_warning:
        _band(report, 4, 'Some reference times are estimated or unspecified. See record details.', span, safe_cell)
        report.row_dimensions[4].height = 32
        _band(detail, 4, 'Estimated or unspecified reference times are identified in Data quality.', detail_span, safe_cell)
    _table(detail, 5, ['Employee', 'Date', 'Punch-in time', 'Reference time', 'Delay (h:mm:ss)', 'Data quality', 'Employee ID'], records, 'PunchRecords', safe_cell)
    notes = workbook.create_sheet('Notes')
    _layout(notes, [30, 110])
    _band(notes, 1, 'Report Notes', 2, safe_cell, title=True)
    items = [('Period', period), ('Filter', label), ('Access scope', data['scope']),
        ('Comparison', 'Only the first recorded active punch-in per employee/day is assessed. After means strictly later, including seconds.'),
        ('Delay (h:mm:ss)', 'Hours:minutes:seconds beyond the reference time. Example: 7:55:27 means 7 hours, 55 minutes, 27 seconds.'),
        ('Historical settings', 'Reconstructed snapshots and current-setting fallbacks are estimates, not verified historical shift assignments.'),
        ('Coverage', coverage), ('Backend notice', data['notice']),
        ('Technical data', 'Original source tables and the original reply are retained in hidden sheets.'),
        ('Charts', 'Charts use the same returned matching employee-days. They do not show the proportion of late versus on-time employees.')]
    threshold = source.get('arguments', {}).get('threshold', 0)
    if threshold:
        items.append(('Minimum occurrences', f'More than {threshold} matching days.'))
    for key, value in data.get('policy_sources', {}).items():
        if value:
            items.append(('Assessed days: ' + POLICY_LABELS.get(key, key), value))
    for line in snapshot['text'].splitlines():
        if line.startswith(('A requested chart could not', 'Only the first 10 requested charts')):
            items.append(('Export notice', line))
    for index, (key, value) in enumerate(items, 3):
        notes.cell(index, 1, safe_cell(key)).font = Font(bold=True, color='315E7E')
        cell = notes.cell(index, 2, safe_cell(value))
        cell.alignment = Alignment(wrap_text=True, vertical='top')
        notes.cell(index, 1).alignment = Alignment(wrap_text=True, vertical='top')
        notes.row_dimensions[index].height = max(30, math.ceil(len(str(value)) / 100) * 16 + 10,
            math.ceil(len(key) / 27) * 16 + 10)
    notes.freeze_panes = 'B3'
    original = workbook.create_sheet('Original Reply')
    for line in snapshot['text'].splitlines():
        original.append([safe_cell(line)])
    original.sheet_state = 'hidden'
    return True
