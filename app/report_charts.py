"""Build bounded, local chart datasets from authorized backend results, never model values."""

import calendar
import re
from collections import Counter
from datetime import date, timedelta

from fastapi import HTTPException

from app.schemas import AttendanceArgs, DailyAttendanceArgs, WorkReportArgs, ReportChart, ManagerReportArgs
from app.manager_query import render_report
from app.task_report import AssistantReply


CHART_REQUEST = re.compile(r'\b(?:charts?|graphs?|plots?|visual(?:ly)?|visuali[sz]\w*|pie|doughnut|donut)\b', re.I)
TYPE_ONLY = re.compile(r'^\s*(?:(?:please|now|can you|show|change|make|switch|convert|it|that|this|the|same|report|numbers|values|as|a|to|into|in|me|just|instead)\s+)*(pie|pi|circle|doughnut|donut|bar|line|table)(?:\s+(?:chart|graph|please|instead|only))*[.!?]?\s*$', re.I)


def restyle(message, charts):
    match = TYPE_ONLY.fullmatch(message)
    if not match or not charts:
        return None
    kind = {'pi': 'pie', 'circle': 'pie', 'donut': 'doughnut'}.get(match[1].lower(), match[1].lower())
    result = []
    for raw in charts:
        chart = ReportChart.model_validate(raw)
        if kind in ('pie', 'doughnut') and (len(chart.series) != 1 or chart.unit == 'percent' or any(v is None for v in chart.series[0].values)):
            return 'A pie chart needs one complete parts-of-a-whole series. Please use a bar or line chart for this report.', []
        result.append(chart.model_copy(update={'type': kind}).model_dump())
    return 'Same report and period.', result


def make_chart(kind, title, unit, labels, series, note, subtitle=''):
    # Nulls are gaps, not fabricated zeroes. Pie charts cannot represent those gaps.
    if kind in ('pie', 'doughnut') and (len(series) != 1 or unit == 'percent' or any(v is None for v in series[0]['values'])):
        kind = 'bar'
        note += ' A bar chart is used because these values are not one complete parts-of-a-whole series.'
    return ReportChart(type=kind, title=title, unit=unit, labels=labels, series=series, note=note, subtitle=subtitle).model_dump()


def punch_chart_data(rows):
    rows = sorted(rows, key=lambda row: (-row['count'], row['employee_name'].casefold(), row['employee_id']))
    shown = rows[:19] if len(rows) > 50 else rows
    labels = [f"{row['employee_name'][:225]} (#{row['employee_id']})" for row in shown]
    values = [None if row.get('assessable') is False else row['count'] for row in shown]
    note = ''
    if len(shown) < len(rows):
        remaining = rows[len(shown):]
        labels.append(f'Other employees ({len(remaining)})')
        values.append(None if any(row.get('assessable') is False for row in remaining) else sum(row['count'] for row in remaining))
        note = f'Top 19 employees by matching days; the remaining {len(remaining)} employees are grouped as Other employees. '
    return labels, values, note


async def build_report_chart(service, identity, selected, *, chart_types=None):
    start, end = date.fromisoformat(selected['start_date']), date.fromisoformat(selected['end_date'])
    metric = selected.get('chart_metric')
    maximum = 92 if metric == 'punch_in_days' else 30
    if not 0 <= (end - start).days <= maximum:
        return f'Please select a chart period of up to {maximum + 1} days.', [], []
    kind = selected.get('chart_type') or 'auto'
    scope = selected['scope']
    common = {'scope': scope, 'employee_id': selected.get('employee_id')}
    period = f'{start} to {end}' if start != end else str(start)
    if metric == 'punch_in_days':
        data = await service.backend.manager_report(identity, ManagerReportArgs(
            **common, report='late_punchin', start_date=start, end_date=end,
            threshold=selected.get('threshold') or 0,
            punch_basis=selected.get('punch_basis') or 'late_cutoff',
            punch_after=selected.get('punch_after'), min_delay_minutes=selected.get('min_delay_minutes') or 0))
        text, used, rows = render_report(data, page=selected.get('page') or 1), ['get_manager_report'], data['rows']
        selected['page'] = selected.get('page') or 1
        if not rows or not any(row['count'] for row in rows):
            return text + '\nNo matching punch-in days are available to chart.', [], used
        if data['details_truncated'] or data['matched_count'] != len(rows):
            return text + '\nPlease narrow the selection; punch-in charts require a complete report.', [], used
        labels, values, grouping_note = punch_chart_data(rows)
        unassessed = data.get('unassessed_days', sum(row.get('unassessed_days', 0) for row in rows))
        note = (grouping_note + 'Matching first daily punch-ins under the report filters. Each employee/date counts once. '
                'Slices represent shares of matching punch-in days, not late versus on-time attendance. '
                'The report time filter, reference-time quality and coverage notes still apply.')
        if unassessed:
            note += f' {unassessed} punch-in days could not be assessed and are excluded, not counted as on-time.'
        charts = []
        for style in chart_types or [kind]:
            style = 'bar' if style == 'auto' else style
            if style == 'line':
                style = 'bar'
                text += '\nA bar chart is used because these values compare employees, not dates.'
            if style in ('pie', 'doughnut') and any(value is None for value in values):
                text += '\nThe requested pie chart is unavailable because some employee counts could not be assessed. The report is retained.'
                continue
            charts.append(make_chart(style, f'Matching punch-in days by employee - {period}',
                'employee-days', labels, [{'name': 'Matching punch-in days', 'values': values}], note,
                f"{scope.capitalize()} | {sum(value for value in values if value is not None)} matching punch-in days"))
        return text, charts, used
    if metric == 'booked_effort':
        data = await service.backend.manager_report(identity, ManagerReportArgs(
            **common, report='booked_time', start_date=start, end_date=end,
            project_id=selected.get('project_id')))
        text = render_report(data)
        used = ['get_manager_report']
        rows = data['rows']
        if not rows:
            return text, [], used
        if data['details_truncated'] or data['matched_count'] != len(rows):
            return text + '\nPlease narrow the employee scope; the effort chart requires a complete report.', [], used
        if any(row['unavailable_count'] for row in rows):
            return text + '\nA complete effort chart is unavailable because some timesheet durations are missing.', [], used
        buckets = [('billable', 'Billable'), ('non_billable', 'Non-billable'), ('unclassified', 'Unclassified billing')]
        buckets = [(key, label) for key, label in buckets
                   if key != 'unclassified' or any(row['billing'][key]['entry_count'] for row in rows)]
        labels = [label for _, label in buckets]
        values = [round(sum(row['billing'][key]['minutes'] for row in rows) / 60, 4) for key, _ in buckets]
        subject = rows[0]['employee_name'] if len(rows) == 1 else scope.capitalize()
        if data.get('project_name'):
            subject += ' / ' + data['project_name']
        total = sum(row['booked_minutes'] for row in rows)
        subtitle = f'Total booked: {total // 60}h {total % 60}m | {sum(row["entry_count"] for row in rows)} timesheet entries'
        note = 'Recorded timesheet bookings by booking date, not attendance or planned task time. Billing categories are parts of total booked effort; the total is not an additional slice.'
        if kind == 'line':
            kind = 'bar'
            note += ' A bar chart is used because this report contains billing categories, not a daily time series.'
        chart = make_chart('doughnut' if kind == 'auto' else kind,
                           f'{subject[:180]} booked effort - {period}', 'hours', labels,
                           [{'name': 'Booked effort', 'values': values}], note, subtitle)
        return text, [chart], used
    if metric in ('attendance_breakdown', 'attendance_trend'):
        totals = Counter()
        points = []
        covered_ids = set()
        employee_name = None
        # Monthly aggregates cover all employees even when detail rows are truncated.
        full_month = start.day == 1 and start.year == end.year and start.month == end.month and end.day == calendar.monthrange(start.year, start.month)[1]
        if metric == 'attendance_breakdown' and full_month:
            data = await service.backend.attendance(identity, AttendanceArgs(**common, month=start.strftime('%Y-%m')))
            counts = data.get('summary')
            unclassified = data.get('unclassified_employee_days')
            if (not isinstance(counts, dict) or any(not isinstance(key, str) or len(key) > 250 or type(value) is not int or value < 0 for key, value in counts.items())
                    or type(unclassified) is not int or unclassified < 0
                    or type(data.get('employee_count')) is not int or data['employee_count'] != len(data['employee_ids'])
                    or len(set(data['employee_ids'])) != len(data['employee_ids'])
                    or sum(counts.values()) + unclassified != len(data['employee_ids']) * ((end - start).days + 1)):
                raise HTTPException(502, 'Invalid attendance chart totals.')
            totals.update(data['summary'])
            covered_ids.update(data['employee_ids'])
            if len(data['employee_ids']) == 1 and data.get('rows'):
                employee_name = data['rows'][0].get('employee_name')
            totals['Unclassified'] += data['unclassified_employee_days']
            used = ['get_attendance']
        else:
            monthly = {}
            used = []
            day = start
            while day <= end:
                key = day.strftime('%Y-%m')
                data = None
                if start != end:
                    if key not in monthly:
                        monthly[key] = await service.backend.attendance(identity, AttendanceArgs(**common, month=key))
                        if 'get_attendance' not in used:
                            used.append('get_attendance')
                    source = monthly[key]
                    rows = [row for row in source['rows'] if row['date'] == str(day)]
                    if len(rows) == len(source['employee_ids']) and {row['employee_id'] for row in rows} == set(source['employee_ids']):
                        data = {'employee_ids': source['employee_ids'], 'rows': rows}
                # Never infer a day's totals from truncated detail rows.
                if data is None:
                    data = await service.backend.daily_attendance(identity, DailyAttendanceArgs(**common, date=day))
                    if 'get_daily_attendance' not in used:
                        used.append('get_daily_attendance')
                ids = data['employee_ids']
                rows = data['rows']
                if len(set(ids)) != len(ids) or len(rows) != len(ids) or {row['employee_id'] for row in rows} != set(ids):
                    raise HTTPException(502, 'Incomplete attendance data; no chart was generated.')
                counts = Counter(row['status'] or 'Unclassified' for row in rows)
                covered_ids.update(ids)
                if len(ids) == 1 and rows:
                    employee_name = rows[0].get('employee_name')
                totals.update(counts)
                points.append((str(day), counts))
                day += timedelta(days=1)
        if any(type(value) is not int or value < 0 for value in totals.values()):
            raise HTTPException(502, 'Invalid attendance chart counts.')
        if not sum(totals.values()):
            return 'No attendance records were returned for this authorized scope and period.', [], used
        statuses = sorted(totals)
        if metric == 'attendance_breakdown':
            labels, series = statuses, [{'name': 'Attendance', 'values': [totals[label] for label in statuses]}]
            kind = 'doughnut' if kind == 'auto' else kind
        else:
            labels = [day for day, _ in points]
            series = [{'name': status, 'values': [counts.get(status, 0) for _, counts in points]} for status in statuses]
            kind = 'line' if kind == 'auto' else kind
        subject = (employee_name[:150] if isinstance(employee_name, str) and employee_name and (scope == 'self' or common['employee_id'])
                   else f"Employee #{common['employee_id']}" if common['employee_id'] else scope.capitalize())
        employees, days = len(covered_ids), (end - start).days + 1
        subtitle = f'{employees} employee{"s" if employees != 1 else ""} | {days} calendar day{"s" if days != 1 else ""} | {sum(totals.values())} employee-days'
        chart = make_chart(kind, f'{subject} attendance - {period}', 'employee-days', labels, series,
                           'Each employee on each date counts once. For example, 10 employees absent for 2 days = 20 absent employee-days. Unclassified days are not absences.', subtitle)
        return chart['title'] + '\n' + subtitle, [chart], used
    if metric not in ('working_hours', 'productivity', 'activity'):
        return 'Charts currently support attendance, working hours, productive versus idle time, average activity, and booked-effort billing breakdowns. Which would you like?', [], []
    result = await service.backend.work_report(identity, WorkReportArgs(**common, start_date=start, end_date=end, include_daily=True))
    data = result.chart_source
    if data is None:
        raise HTTPException(502, 'Daily chart data is unavailable. Update the web and AI services together.')
    rows = data.rows
    if not rows:
        return 'No active employees are available in this authorized scope.', [], ['get_employee_work_report']
    dates = sorted({str(day.date) for row in rows for day in row.daily})
    if not dates:
        raise HTTPException(502, 'Daily chart data is unavailable. Update the web and AI services together.')
    note = 'Missing values are unavailable, not zero. Attendance time is not payroll time; activity is not a work-quality rating.'
    if metric == 'working_hours':
        if len(rows) > 1 and kind not in ('line',):
            labels = [row.employee_name for row in rows]
            series = [{'name': 'Attendance hours', 'values': [None if row.summary.attendance_minutes is None else round(row.summary.attendance_minutes / 60, 4) for row in rows]}]
        else:
            labels = dates
            series = [{'name': row.employee_name, 'values': [None if day.attendance_minutes is None else round(day.attendance_minutes / 60, 4) for day in sorted(row.daily, key=lambda day: day.date)]} for row in rows]
        kind = 'bar' if kind == 'auto' else kind
        unit, title = 'hours', 'Attendance working hours'
    elif metric == 'activity':
        labels = dates
        series = [{'name': row.employee_name, 'values': [day.activity for day in sorted(row.daily, key=lambda day: day.date)]} for row in rows]
        kind = 'line' if kind == 'auto' else kind
        unit, title = 'percent', 'Average daily activity'
    else:
        paired = [(str(day.date), day.productive_seconds, day.idle_seconds) for row in rows if row.tracking_available for day in row.daily
                  if day.productive_seconds is not None and day.idle_seconds is not None]
        note += ' Productive and idle totals include only employee-days with both metrics recorded.'
        if not paired:
            return 'Productive/idle chart data is unavailable for this period under the current screenshot module access or recorded coverage.', [], ['get_employee_work_report']
        if kind in ('line', 'bar'):
            labels = dates
            series = [{'name': name, 'values': [round(sum(item[index] for item in paired if item[0] == day) / 3600, 4) if any(item[0] == day for item in paired) else None for day in dates]}
                      for name, index in [('Productive', 1), ('Idle', 2)]]
        else:
            labels = ['Productive', 'Idle']
            series = [{'name': 'Recorded hours', 'values': [round(sum(item[index] for item in paired) / 3600, 4) for index in (1, 2)]}]
        kind = 'doughnut' if kind == 'auto' else kind
        unit, title = 'hours', 'Productive versus idle time'
    if len(series) > 20:
        # Aggregate the full selection instead of silently dropping employee series.
        points = {day: [getattr(item, 'activity' if metric == 'activity' else 'attendance_minutes')
                        for row in rows for item in row.daily if str(item.date) == day
                        and getattr(item, 'activity' if metric == 'activity' else 'attendance_minutes') is not None]
                  for day in dates}
        labels = dates
        series = [{'name': 'Mean recorded employee activity' if metric == 'activity' else 'Total recorded attendance hours',
                   'values': [round(sum(points[day]) / (len(points[day]) if metric == 'activity' else 60), 4)
                              if points[day] else None for day in dates]}]
        note += (' Daily activity is the arithmetic mean across employees with a recorded score; missing scores are excluded.'
                 if metric == 'activity' else ' Daily hours sum all authorized employees with recorded attendance.')
    if len(labels) > 50:
        # Only additive working-hour comparisons reach this branch.
        values = series[0]['values']
        ranked = sorted(zip(labels, values), key=lambda item: (item[1] is None, -(item[1] or 0), item[0]))
        labels = [label for label, _ in ranked[:19]] + ['Other employees']
        remaining = [value for _, value in ranked[19:] if value is not None]
        series[0]['values'] = [value for _, value in ranked[:19]] + [round(sum(remaining), 4) if remaining else None]
        note += ' Top 19 employees are shown individually; Other employees sums all remaining recorded hours.'
    if all(value is None for item in series for value in item['values']):
        return 'No recorded values are available for this chart under the current module access and period.', [], ['get_employee_work_report']
    chart = make_chart(kind, f'{title} - {data.period.start} to {data.period.end}', unit, labels, series, note)
    return str(result.local_reply), [chart], ['get_employee_work_report']


def chart_reply(text, selected, charts):
    return AssistantReply(text, model_content='An authorized chart/report was rendered locally; chart values and employee names were not sent to the model.',
                          query_context={'kind': 'semantic_question', 'selection': selected}, charts=charts)
