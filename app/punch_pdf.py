"""Compact, business-facing PDF layout for authorized punch-in snapshots."""

from datetime import date
from html import escape
from io import BytesIO

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.charts.doughnut import Doughnut
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import KeepTogether, LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.punch_workbook import POLICY_LABELS, _clock, _filter_label


def _time(value):
    clock = _clock(value)
    return clock.strftime('%I:%M:%S %p' if clock.second else '%I:%M %p').lstrip('0') if clock else 'Unavailable'


def _delay(entry):
    seconds = entry.get('delay_seconds', entry.get('late_minutes', 0) * 60)
    hours, rest = divmod(int(seconds), 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours}:{minutes:02}:{seconds:02}'


def punch_pdf_bytes(snapshot, validate_chart):
    sources = snapshot.get('sources', [])
    if len(sources) != 1 or sources[0]['endpoint'] != 'manager-report' or sources[0]['data'].get('report') != 'late_punchin':
        return None
    source = sources[0]
    data, rows = source['data'], source['data']['rows']
    filters = data.get('punch_filter') or source.get('arguments', {})
    entries = [(employee, entry) for employee in rows for entry in employee['entries']]
    shared_reference = filters.get('punch_basis') == 'fixed_time' and all(
        entry.get('cutoff', employee.get('late_after')) == filters.get('punch_after') for employee, entry in entries)
    quality = any(entry.get('policy_source') not in ('fixed_time', 'saved_snapshot') for _, entry in entries)
    unassessed = data.get('unassessed_days', sum(row.get('unassessed_days', 0) for row in rows))
    period = ' to '.join(date.fromisoformat(data[key]).strftime('%d %b %Y') for key in ('start_date', 'end_date'))
    styles = getSampleStyleSheet()
    styles['BodyText'].fontSize, styles['BodyText'].leading = 9, 12
    styles['BodyText'].spaceBefore, styles['BodyText'].spaceAfter = 2, 0
    styles['BodyText'].wordWrap = 'CJK'
    styles['Heading2'].fontSize, styles['Heading2'].spaceBefore = 11, 7
    styles['Heading2'].spaceAfter = 4
    styles['Title'].fontSize, styles['Title'].leading = 16, 20
    p = lambda value: Paragraph(escape(str(value) if value is not None else 'Unavailable'), styles['BodyText'])

    def table(headers, values, widths):
        grid = LongTable([[p(value) for value in headers], *[[p(value) for value in record] for record in values]],
            colWidths=widths, repeatRows=1, splitByRow=1, splitInRow=1, hAlign='LEFT')
        grid.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E8F1EE')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F6F8FA')]),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LINEBELOW', (0, 0), (-1, -1), .25, colors.HexColor('#D7DFE3')),
            ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
        return grid

    flow = [Paragraph('Punch-In Time Report', styles['Title']), p(period), p(_filter_label(filters)), Spacer(1, 7),
        p(f"Matching employees: {sum(row['count'] > 0 for row in rows)} | Matching punch-in days: {sum(row['count'] for row in rows)} | Scope: {data['scope']}"),
        p(f"Showing {len(rows)} of {data['matched_count']} employee rows.")]
    if data['details_truncated']:
        flow.append(p('Partial report: results are truncated. Narrow the selection for a complete report.'))
    if unassessed:
        flow.append(p(f'{unassessed} punch-in days could not be assessed. These are not counted as on-time.'))
    if quality:
        flow.append(p('Some reference times are estimated or unspecified. See Data quality below.'))
    flow.append(Paragraph('Employee Summary', styles['Heading2']))
    headers = ['No.', 'Employee', 'Matching days']
    values = [[index, row['employee_name'], row['count']] for index, row in enumerate(rows, 1)]
    widths = [35, 615, 120]
    if unassessed:
        headers.append('Unassessed days')
        widths = [35, 505, 110, 120]
        for value, employee in zip(values, rows):
            value.append(employee.get('unassessed_days', 0))
    flow.append(table(headers, values, widths) if values else p('No matching employee records.'))
    flow.extend([Paragraph('Date-Wise Records', styles['Heading2']),
        p('Delay is hours:minutes:seconds beyond the reference time. Only the first recorded active punch-in per employee/day is assessed.')])
    headers = ['Employee', 'Date', 'Punch-in', 'Delay (h:mm:ss)']
    weights = [3.2, 1.4, 1.5, 1.5]
    if not shared_reference:
        headers.append('Reference')
        weights.append(1.5)
    if quality:
        headers.append('Data quality')
        weights.append(2.5)
    details = []
    for employee, entry in entries:
        record = [employee['employee_name'], date.fromisoformat(entry['date']).strftime('%d %b %Y'),
            _time(entry['punch_in']), _delay(entry)]
        if not shared_reference:
            record.append(_time(entry.get('cutoff', employee.get('late_after'))))
        if quality:
            record.append(POLICY_LABELS.get(entry.get('policy_source'), 'Not specified'))
        details.append(record)
    flow.append(table(headers, details, [770 * weight / sum(weights) for weight in weights]) if details else p('No matching punch-in records.'))

    # Charts follow the data; summary row numbers replace long, repeated legends.
    chart_labels = {f"{row['employee_name']} (#{row['employee_id']})": str(index) for index, row in enumerate(rows, 1)}
    if len(rows) > 50:
        chart_labels[f'Other employees ({len(rows) - 19})'] = 'Other'
    palette = ['#247765', '#4367C8', '#D39619', '#A45B93', '#697982', '#C35454']
    panels = []
    for chart in snapshot.get('charts', []):
        validate_chart(chart)
        if chart['type'] == 'table' or not any(value for series in chart['series'] for value in series['values'] if value is not None):
            continue
        mapped = all(label in chart_labels for label in chart['labels'])
        labels = [chart_labels[label] for label in chart['labels']] if mapped else [str(index + 1) for index in range(len(chart['labels']))]
        drawing = Drawing(370, 120)
        if chart['type'] in ('pie', 'doughnut'):
            graph = Doughnut() if chart['type'] == 'doughnut' else Pie()
            graph.x, graph.y, graph.width, graph.height = 125, 10, 95, 95
            graph.data, graph.labels = chart['series'][0]['values'], labels
            for index in range(len(graph.data)):
                graph.slices[index].fillColor = colors.HexColor(palette[index % len(palette)])
        else:
            graph = HorizontalLineChart() if chart['type'] == 'line' else VerticalBarChart()
            graph.x, graph.y, graph.width, graph.height = 40, 20, 310, 85
            graph.data = [series['values'] for series in chart['series']]
            graph.categoryAxis.categoryNames = labels
            graph.categoryAxis.labels.fontSize = 7
            graph.valueAxis.labels.fontSize = 7
            if chart['type'] == 'bar':
                for index in range(len(chart['series'])):
                    graph.bars[index].fillColor = colors.HexColor(palette[index % len(palette)])
        drawing.add(graph)
        panel = [p(chart['title']), drawing]
        panel.append(p(('Numbers refer to Employee Summary rows. Other groups all remaining employees.' if 'Other' in labels
            else 'Numbers refer to Employee Summary rows.') if mapped else 'Chart categories: ' + '; '.join(
            f"{index + 1}. {label}: " + ', '.join(str(series['values'][index]) for series in chart['series'])
            for index, label in enumerate(chart['labels']))))
        if len(chart['series']) > 1:
            panel.append(p('Series: ' + '; '.join(series['name'] for series in chart['series'])))
        if chart.get('note') and chart['note'] != 'Based on the returned report data; missing values are unavailable, not zero.':
            panel.append(p(chart['note']))
        panels.append(panel)
    if panels:
        for index in range(0, len(panels), 2):
            pair = Table([panels[index:index + 2]], colWidths=[385] * len(panels[index:index + 2]), hAlign='LEFT')
            pair.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
            flow.append(KeepTogether([Paragraph('Requested Charts', styles['Heading2']), pair]) if index == 0 else pair)
    flow.append(Paragraph('Report Notes', styles['Heading2']))
    if data.get('notice'):
        flow.append(p(data['notice']))
    if panels:
        flow.append(p('Charts show the distribution of matching punch-in days, not late versus on-time attendance.'))
    threshold = source.get('arguments', {}).get('threshold', 0)
    if threshold:
        flow.append(p(f'Employee selection: more than {threshold} matching days.'))
    for line in snapshot['text'].splitlines():
        if line.startswith(('A requested chart could not', 'Only the first 10 requested charts')):
            flow.append(p(line))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.drawString(30, 17, 'Effortrak | Punch-In Time Report')
        canvas.drawRightString(811, 17, f'Page {document.page}')
        canvas.restoreState()

    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=30, rightMargin=30,
        topMargin=25, bottomMargin=30, title='Punch-In Time Report', author='Effortrak').build(
            flow, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
