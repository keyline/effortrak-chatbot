"""Local PDF/XLSX exports of displayed, bounded and reauthorized report snapshots."""

import copy
import hashlib
import json
import math
import re
from collections import Counter
from contextvars import ContextVar
from io import BytesIO
from uuid import uuid4
from xml.sax.saxutils import escape

from fastapi import HTTPException
from openpyxl import Workbook
from openpyxl.chart import BarChart, DoughnutChart, LineChart, PieChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.charts.doughnut import Doughnut
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from app.task_report import AssistantReply


READ_ENDPOINTS = frozenset({'attendance', 'daily-tasks', 'task-summary',
    'project-tasks', 'manager-report', 'work-report', 'leave-balance', 'leave-history',
    'visit-location', 'employee-count'})
capture_sources = ContextVar('report_export_sources', default=None)
defer_export = ContextVar('report_export_deferred', default=False)
FILE_REQUEST = re.compile(r'\b(?:pdf|xlsx|excel|export|download)\b', re.I)
GENERIC = frozenset('i want need like can could would you please kindly provide generate share give send get make convert into version me this that the same current report chart it in as to format a an download export pdf excel xlsx file with include including and data table tables charts graph graphs'.split())


def capture(endpoint, arguments, data):
    sources = capture_sources.get()
    if sources is not None and endpoint in READ_ENDPOINTS and data.get('status') != 'clarify':
        if len(sources) >= 9:
            return
        if len(json.dumps(data).encode()) > 2 * 1024 * 1024:
            sources.append({'oversized': True})
            return
        sources.append({'endpoint': endpoint, 'arguments': copy.deepcopy(arguments or {}), 'data': copy.deepcopy(data)})


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()
                if key not in ('as_of', 'as_of_date', 'generated_at')}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def fingerprint(data):
    return hashlib.sha256(json.dumps(canonical(data), sort_keys=True, allow_nan=False).encode()).hexdigest()


async def authorize(snapshot, identity, backend):
    if not identity.can_export:
        raise HTTPException(403, 'You do not have report export permission.')
    await backend.revalidate(identity)
    if (not isinstance(snapshot, dict) or snapshot.get('version') != 1
            or snapshot.get('company_id') != identity.company_id or snapshot.get('user_id') != identity.user_id
            or snapshot.get('policy_key') != identity.policy_key
            or not isinstance(snapshot.get('sources'), list) or not 1 <= len(snapshot['sources']) <= 8):
        raise HTTPException(409, 'This report cannot be exported. Display it again and retry.')
    for source in snapshot['sources']:
        if source.get('endpoint') not in READ_ENDPOINTS or not isinstance(source.get('arguments'), dict):
            raise HTTPException(409, 'Invalid saved report selection. Display the report again.')
        current = await backend.call(source['endpoint'], identity.credential, source['arguments'])
        if fingerprint(current) != fingerprint(source['data']):
            raise HTTPException(409, 'Report data or access changed. Display the report again before exporting or downloading it.')


def inferred_charts(sources):
    charts = []
    for source in sources:
        data = source['data']
        if data.get('details_truncated') or data.get('has_more'):
            continue
        def add(title, unit, labels, values, kind='bar'):
            if labels and len(labels) <= 50 and any(value is not None for value in values):
                charts.append(dict(type=kind, title=title, unit=unit, labels=labels,
                    series=[dict(name=unit, values=values)], note='Based on the returned report data; missing values are unavailable, not zero.'))
        if source['endpoint'] == 'leave-balance':
            for group in data.get('groups', []):
                add(group['leave_type'] + ' - remaining leave', 'days',
                    [row['employee_name'] for row in group['rows']], [row['remaining_days'] for row in group['rows']])
        elif source['endpoint'] == 'manager-report' and data.get('report') == 'booked_time':
            rows = data.get('rows', [])
            if rows and not any(row.get('unavailable_count') for row in rows):
                keys = [('billable', 'Billable'), ('non_billable', 'Non-billable'), ('unclassified', 'Unclassified billing')]
                keys = [(key, label) for key, label in keys if key != 'unclassified' or any(row['billing'][key]['entry_count'] for row in rows)]
                add('Booked effort billing breakdown', 'hours', [label for _, label in keys],
                    [round(sum(row['billing'][key]['minutes'] for row in rows) / 60, 4) for key, _ in keys], 'pie')
        elif source['endpoint'] in ('attendance', 'daily-attendance'):
            summary = data.get('summary', {})
            if isinstance(summary, dict) and all(type(value) in (int, float) for value in summary.values()):
                add('Recorded attendance statuses', 'employees' if source['endpoint'] == 'daily-attendance' else 'employee-days', list(summary), list(summary.values()))
        elif source['endpoint'] in ('daily-tasks', 'project-tasks', 'task-summary'):
            counts = Counter()
            for row in data.get('rows', []):
                counts[row.get('status_name') or f"Status {row.get('work_status_id', 'unclassified')}"] += int(row.get('task_count', 1))
            add('Task statuses - returned rows', 'tasks', list(counts), list(counts.values()))
        elif source['endpoint'] == 'visit-location' and data.get('action') == 'visits':
            counts = Counter(row.get('employee_name') or 'Unnamed' for row in data.get('rows', []))
            add('Recorded visits - returned rows', 'check-ins', list(counts), list(counts.values()))
        elif source['endpoint'] == 'work-report':
            rows = data.get('rows', [])
            add('Recorded attendance time', 'hours', [row['employee_name'] for row in rows],
                [None if row['summary'].get('attendance_minutes') is None else round(row['summary']['attendance_minutes'] / 60, 4) for row in rows])
        elif source['endpoint'] == 'employee-count':
            add('Active employee headcount', 'employees', [data['scope']], [data['employee_count']])
        elif source['endpoint'] == 'manager-report':
            rows = data.get('rows', [])
            labels = [f"{row.get('employee_name', row.get('project_name', 'Record'))} (#{row.get('employee_id', row.get('project_id', ''))})" for row in rows]
            if data.get('report') in ('late', 'late_punchin', 'missing_punchout', 'present', 'absent'):
                add(data['report'].replace('_', ' ').title(), 'recorded days', labels,
                    [None if row.get('assessable') is False else row['count'] for row in rows])
            elif data.get('report') in ('effort_by_employee', 'effort_by_project', 'task_overruns', 'workload') and rows and len(rows) <= 50:
                charts.append(dict(type='bar', title='Planned and booked time (separate measures)', unit='hours', labels=labels,
                    series=[dict(name=label, values=[None if row.get(key) is None else round(row[key] / 60, 4) for row in rows])
                        for key, label in [('planned_minutes', 'Planned'), ('booked_minutes', 'Booked')]],
                    note='Planned task time and recorded task-linked booked time are distinct; neither is attendance time.'))
            elif data.get('report') == 'attendance_totals':
                counts = Counter()
                for row in rows:
                    counts.update(row['status_counts'])
                add('Recorded attendance status totals', 'employee-days', list(counts), list(counts.values()))
            elif data.get('report') in ('approved_leave', 'work_arrangements'):
                counts = Counter((row.get('arrangement') or row['employee_name']) for row in rows)
                add('Returned ' + data['report'].replace('_', ' '), 'requests' if data['report'] == 'approved_leave' else 'employees', list(counts), list(counts.values()))
    return charts[:10]


def make_snapshot(reply, sources, identity, previous=None):
    if not sources:
        if not previous or not getattr(reply, 'charts', None):
            return None
        sources = copy.deepcopy(previous['sources'])
    if len(sources) > 8 or any(source.get('oversized') for source in sources):
        return None
    charts = copy.deepcopy(getattr(reply, 'charts', []))
    if not charts:
        try:
            charts = inferred_charts(sources)
        except (KeyError, TypeError, ValueError):
            charts = []
    result = dict(version=1, company_id=identity.company_id, user_id=identity.user_id,
        policy_key=identity.policy_key, text=str(reply), charts=charts, sources=sources)
    try:
        size = len(json.dumps(result, allow_nan=False).encode())
    except (ValueError, TypeError):
        return None
    if size > 2 * 1024 * 1024:
        return None
    return result


def scalar_rows(value, prefix=''):
    result = {}
    for key, item in value.items():
        if key in ('employee_ids', 'data_source'):
            continue
        name = f'{prefix}.{key}' if prefix else key
        if isinstance(item, dict):
            result.update(scalar_rows(item, name))
        elif isinstance(item, list) and not any(isinstance(child, (dict, list)) for child in item):
            result[name] = json.dumps(item, ensure_ascii=False)
        elif not isinstance(item, list):
            result[name] = item
    return result


def data_tables(snapshot):
    tables = []
    def walk(value, path, parent=None):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    walk(item, f'{path} / {key}', parent)
        elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            rows = [{**(parent or {}), **scalar_rows(item)} for item in value]
            headers = list(dict.fromkeys(key for row in rows for key in row))
            if len(rows) > 5000 or len(headers) > 80:
                raise HTTPException(422, 'This report is too large to export. Narrow the report selection.')
            if headers:
                tables.append((path, headers, [[row.get(key) for key in headers] for row in rows]))
            for index, item in enumerate(value):
                label = {key: item[key] for key in ('employee_id', 'employee_name', 'leave_type') if key in item}
                walk(item, f'{path} {index + 1}', {**(parent or {}), **label})
    for index, source in enumerate(snapshot['sources'], 1):
        data = source['data']
        metadata = scalar_rows(data)
        if metadata:
            tables.append((f"{source['endpoint']} {index} / summary", ['Field', 'Value'], list(map(list, metadata.items()))))
        walk(data, f"{source['endpoint']} {index}")
    if len(tables) > 150 or sum(len(rows) * len(headers) for _, headers, rows in tables) > 100000:
        raise HTTPException(422, 'This report is too large to export. Narrow the report selection.')
    return tables


def safe_cell(value):
    if isinstance(value, str):
        value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', value)
        if len(value) > 30000:
            raise HTTPException(422, 'A report field is too large to export. Narrow the report selection.')
        if value.lstrip().startswith(('=', '+', '-', '@')):
            value = "'" + value
    return value


def valid_chart(chart):
    if chart.get('type') not in ('pie', 'doughnut', 'bar', 'line', 'table'):
        raise HTTPException(422, 'Unsupported report chart.')
    labels, series = chart.get('labels'), chart.get('series')
    if (not isinstance(labels, list) or not 1 <= len(labels) <= 50 or not all(isinstance(label, str) for label in labels)
            or not isinstance(series, list) or not 1 <= len(series) <= 20
            or any(not isinstance(row.get('name'), str) or len(row.get('values', [])) != len(labels)
                or any(value is not None and (type(value) not in (int, float) or not math.isfinite(value)) for value in row['values']) for row in series)):
        raise HTTPException(422, 'Invalid report chart data.')
    if chart['type'] in ('pie', 'doughnut') and (len(series) != 1 or any(value is None or value < 0 for value in series[0]['values'])):
        raise HTTPException(422, 'A pie chart needs complete nonnegative parts of one whole.')


def workbook_bytes(snapshot, tables):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Report'
    sheet.append(['Effortrak - exported report'])
    sheet.append(['Displayed result only. Page, scope and truncation notices remain applicable.'])
    sheet.append(['Up to 10 charts are included when meaningful; all returned report data remains in the data sheets. Locations are not plotted as numeric measurements.'])
    for line in snapshot['text'].splitlines():
        sheet.append([safe_cell(line)])
    sheet.column_dimensions['A'].width = 110
    for row in sheet:
        row[0].alignment = Alignment(wrap_text=True, vertical='top')
    for index, (title, headers, rows) in enumerate(tables, 1):
        sheet = workbook.create_sheet(f'Data {index}')
        sheet.append([safe_cell(title)])
        sheet.append([safe_cell(header) for header in headers])
        for row in rows:
            sheet.append([safe_cell(value) for value in row])
        sheet.freeze_panes = 'A3'
        sheet.auto_filter.ref = f'A2:{sheet.cell(sheet.max_row, len(headers)).coordinate}'
        for cell in sheet[2]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill('solid', fgColor='E8F1EE')
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = 26
            for cell in column:
                cell.alignment = Alignment(wrap_text=True, vertical='top')
    for index, chart in enumerate(snapshot['charts'], 1):
        valid_chart(chart)
        sheet = workbook.create_sheet(f'Chart {index}')
        sheet.append([safe_cell(chart['title'])])
        sheet.append(['Category', *[safe_cell(series['name']) for series in chart['series']]])
        for pos, label in enumerate(chart['labels']):
            sheet.append([safe_cell(label), *[series['values'][pos] for series in chart['series']]])
        sheet.column_dimensions['A'].width = 38
        sheet.freeze_panes = 'B3'
        sheet.append([safe_cell(chart.get('note', ''))])
        if chart['type'] == 'table' or not any(value for row in chart['series'] for value in row['values'] if value is not None):
            continue
        graph = {'pie': PieChart, 'doughnut': DoughnutChart, 'line': LineChart, 'bar': BarChart}[chart['type']]()
        graph.title = chart['title'][:200]
        graph.add_data(Reference(sheet, min_col=2, max_col=1 + len(chart['series']), min_row=2, max_row=2 + len(chart['labels'])), titles_from_data=True)
        graph.set_categories(Reference(sheet, min_col=1, min_row=3, max_row=2 + len(chart['labels'])))
        graph.display_blanks = 'gap'
        graph.width, graph.height = 24, 13
        if chart['type'] in ('bar', 'line'):
            graph.y_axis.title = chart.get('unit', '')
        sheet.add_chart(graph, 'H2')
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def pdf_bytes(snapshot, tables):
    buffer = BytesIO()
    styles = getSampleStyleSheet()
    styles['BodyText'].fontSize = 8
    styles['BodyText'].leading = 11
    styles['BodyText'].wordWrap = 'CJK'
    paragraph = lambda value: Paragraph(escape('Unavailable' if value is None else str(value)), styles['BodyText'])
    flow = [Paragraph('Effortrak Report', styles['Title']), paragraph('Export of the displayed result. Scope, page and truncation notes remain applicable. Up to 10 meaningful charts are included; all returned data is retained below.'), Spacer(1, 10)]
    palette = [colors.HexColor(value) for value in ('#247765', '#4367C8', '#D39619', '#A45B93', '#697982', '#C35454')]
    for chart in snapshot['charts']:
        valid_chart(chart)
        flow.extend([Paragraph(escape(chart['title']), styles['Heading2']), paragraph(chart.get('subtitle', ''))])
        if chart['type'] != 'table' and any(value for row in chart['series'] for value in row['values'] if value is not None):
            drawing = Drawing(700, 235)
            if chart['type'] in ('pie', 'doughnut'):
                graph = Doughnut() if chart['type'] == 'doughnut' else Pie()
                graph.x, graph.y, graph.width, graph.height = 240, 15, 200, 200
                graph.data = chart['series'][0]['values']
                graph.labels = [str(index + 1) for index in range(len(chart['labels']))]
                for index in range(len(graph.data)):
                    graph.slices[index].fillColor = palette[index % len(palette)]
            else:
                graph = HorizontalLineChart() if chart['type'] == 'line' else VerticalBarChart()
                graph.x, graph.y, graph.width, graph.height = 40, 70, 620, 145
                graph.data = [row['values'] for row in chart['series']]
                graph.categoryAxis.categoryNames = [label[:20] for label in chart['labels']]
                graph.categoryAxis.labels.angle = 30
                graph.categoryAxis.labels.fontSize = 6
                graph.valueAxis.labels.fontSize = 7
            drawing.add(graph)
            flow.append(drawing)
        heads = ['Category', *[row['name'] for row in chart['series']]]
        values = [[f'{index + 1}. {label}' if chart['type'] in ('pie', 'doughnut') else label,
                   *[row['values'][index] for row in chart['series']]] for index, label in enumerate(chart['labels'])]
        flow.append(_pdf_table(heads, values, paragraph))
        flow.extend([paragraph(chart.get('note', '')), Spacer(1, 10)])
    for title, headers, rows in tables:
        flow.append(Paragraph(escape(title), styles['Heading2']))
        # Narrow column groups keep wide report schemas readable without discarding fields.
        for start in range(0, len(headers), 6):
            positions = list(range(start, min(start + 6, len(headers))))
            if start:
                positions.insert(0, 0)
            flow.append(_pdf_table([headers[index] for index in positions], [[row[index] for index in positions] for row in rows], paragraph))
            flow.append(Spacer(1, 8))
    flow.append(Paragraph('Displayed Report and Notices', styles['Heading2']))
    for line in snapshot['text'].splitlines():
        flow.append(paragraph(line or ' '))
    SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=30, leftMargin=30,
        topMargin=30, bottomMargin=30, title='Effortrak Report', author='Effortrak').build(flow)
    return buffer.getvalue()


def _pdf_table(headers, rows, paragraph):
    table = LongTable([[paragraph(value) for value in headers], *[[paragraph(value) for value in row] for row in rows]],
        repeatRows=1, colWidths=[770 / len(headers)] * len(headers), splitByRow=1, splitInRow=1)
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E8F1EE')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LINEBELOW', (0, 0), (-1, -1), .3, colors.lightgrey),
        ('LEFTPADDING', (0, 0), (-1, -1), 5), ('TOPPADDING', (0, 0), (-1, -1), 5)]))
    return table


async def export_snapshot(snapshot, format, identity, backend, context):
    if format not in ('pdf', 'xlsx'):
        raise HTTPException(422, 'Choose PDF or Excel (XLSX).')
    await authorize(snapshot, identity, backend)
    tables = data_tables(snapshot)
    payload = pdf_bytes(snapshot, tables) if format == 'pdf' else workbook_bytes(snapshot, tables)
    if len(payload) > 8 * 1024 * 1024:
        raise HTTPException(422, 'The report file is too large. Narrow the report selection.')
    report_id = str(uuid4())
    report = dict(report_id=report_id, filename=f'effortrak-report-{report_id[:8]}.{format}', format=format,
        download_url=f'/v1/reports/{report_id}/download', payload=payload, employee_ids=[], export_snapshot=snapshot)
    reply = AssistantReply('Your displayed report is attached as ' + format.upper() + '. Included charts and tables use the same report data. Existing page/truncation notes still apply.',
        model_content='An authorized report was exported locally. No report records were sent to the model.', query_context=context)
    reply.export_snapshot = snapshot
    return reply, ['export_report'], [report]


async def reply_with_exports(service, message, history, identity, client_platform):
    from app.website_help import INSTRUCTION
    previous = None
    context = {}
    if history and history[-1]['role'] == 'assistant':
        previous = json.loads(history[-1].get('export_snapshot') or 'null')
        context = json.loads(history[-1].get('query_context') or '{}')
    exporting = bool(service.backend and FILE_REQUEST.search(message) and not INSTRUCTION.search(message))
    words = set(re.findall(r'[a-z]+', message.lower()))
    generic = words.issubset(GENERIC) and not re.search(r'\d', message)
    formats = set('xlsx' if value in ('xlsx', 'excel') else 'pdf' for value in re.findall(r'\b(pdf|excel|xlsx)\b', message.lower()))
    if exporting and not generic and words.issubset(GENERIC | {'task', 'tasks', 'project', 'projects'}):
        return AssistantReply('Please specify the task/project report, employee or scope, and period, or display it first and ask for PDF or Excel.', query_context={}), [], []
    if exporting and not generic and not words.intersection({'attendance', 'task', 'tasks', 'project', 'projects',
            'effort', 'efforts', 'booked', 'timesheet', 'leave', 'visits', 'visit', 'location', 'locations',
            'work', 'working', 'hours', 'productive', 'productivity', 'idle', 'activity', 'late', 'lateness',
            'absent', 'present', 'workload', 'overruns', 'headcount', 'count', 'punch', 'punchout'}):
        return AssistantReply('Please specify which report to export, its employee/scope and period, or display it first and ask for PDF or Excel.', query_context={}), [], []
    if exporting and generic:
        if not previous:
            return AssistantReply('Please display the report first (or narrow its scope if it is too large), then ask to export it as PDF or Excel.', query_context={}), [], []
        if len(formats) != 1:
            reply = AssistantReply('Which export format: PDF or Excel (XLSX)?', query_context=context)
            reply.export_snapshot = previous
            return reply, [], []
        return await export_snapshot(previous, formats.pop(), identity, service.backend, context)
    direct = exporting and not ('attendance' in words and not words.intersection({'chart', 'charts', 'graph'}))
    question = message
    if direct:
        question = re.sub(r'\b(?:in|as|to)\s+(?:an?\s+)?(?:pdf|excel|xlsx)(?:\s+(?:format|file))?\b', '', question, flags=re.I)
        question = re.sub(r'\b(?:pdf|excel|xlsx)\b', '', question, flags=re.I)
        question = re.sub(r'\b(?:export|download)\b', 'show', question, flags=re.I)
        question = re.sub(r'\b(?:with|including)\s+(?:a\s+)?charts?\b', '', question, flags=re.I)
    sources = []
    token = capture_sources.set(sources)
    deferred = defer_export.set(direct)
    try:
        result = await service._reply(question, history, identity, client_platform=client_platform)
    finally:
        defer_export.reset(deferred)
        capture_sources.reset(token)
    reply, tools, reports = result
    if not reports and service.backend and (sources or getattr(reply, 'charts', [])):
        if not isinstance(reply, AssistantReply):
            reply = AssistantReply(reply, model_content='An authorized report was handled locally.')
        snapshot = make_snapshot(reply, sources, identity, previous)
        if snapshot:
            reply.export_snapshot = snapshot
            result = reply, tools, reports
            if direct and len(formats) == 1:
                return await export_snapshot(snapshot, formats.pop(), identity, service.backend, getattr(reply, 'query_context', None) or {})
            if direct:
                pending = AssistantReply(str(reply) + '\nWhich export format: PDF or Excel (XLSX)?',
                    model_content=reply.model_content, query_context=reply.query_context, charts=reply.charts)
                pending.export_snapshot = snapshot
                return pending, tools, reports
    return result
