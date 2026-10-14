import calendar
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import ValidationError

from app.attendance_report import duration
from app.project_query import period, query_text
from app.schemas import ManagerReportArgs
from app.task_report import AssistantReply
from app.employee_lookup import INTRO, name_tokens


BOOKING_METRIC = r'(?:booking|booked|logged|timesheet|billable(?:\s+(?:and|&)\s+non[- ]?billable)?|non[- ]?billable)'
BILLING_QUESTION = re.compile(r'\b(?:billable|non[- ]?billable)\b', re.I)


TITLES = {'late': 'Late attendance', 'late_punchin': 'Late punch-ins', 'missing_punchout': 'Missing punch-outs', 'present': 'Present employees',
          'absent': 'Absent employees', 'work_arrangements': 'Permitted work arrangements',
          'locations': 'Recorded attendance locations', 'missing_location': 'Attendance with missing GPS',
          'effort_by_employee': 'Employee planned and booked time', 'effort_by_project': 'Project planned and booked time',
          'task_overruns': 'Tasks exceeding planned time', 'workload': 'Planned workload',
          'approved_leave': 'Approved leave', 'attendance_totals': 'Attendance summary', 'booked_time': 'Booked effort'}


def capability_reply(message):
    rules = [
        (r'\b(?:approve|reject|assign|reassign|remind|send|schedule)\b.*\b(?:leave|request|task|summary|report|timesheet)\b|\b(?:automatically|every\s+(?:week|day|month))\b',
         'I can show available reports, but cannot change assignments, approve requests, send reminders or schedule delivery yet. No changes have been made.'),
        (r'\b(?:pending|waiting|need|needs)\b.*\bapprovals?\b|\b(?:leave|timesheet)\b.*\b(?:approval|approve)\b',
         'Approval-queue reports are not available in the assistant yet. Please use the existing approval screen.'),
        (r'\b(?:overdue|deadline|unfinished|completed|completion|progress)\b.*\b(?:tasks?|projects?)\b|\b(?:tasks?|projects?)\b.*\b(?:overdue|unfinished|completed|completion|progress)\b',
         'Deadline and completion analysis is not available yet. I can show recorded task statuses and planned/booked time for a date or period.'),
        (r'\b(?:capacity|available\s+to\s+take|available\s+for\s+another)\b',
         'Available capacity is not calculated yet. I can show planned workload, but cannot infer availability without working schedules and approved leave.'),
        (r'\b(?:geofence|outside|boundary|how\s+far|distance|low[- ]accuracy|gps\s+accuracy|live\s+location|current\s+location)\b',
         'I can show authorized attendance capture locations, but cannot verify geofence compliance, GPS accuracy or an employee\'s current location yet.'),
        (r'\b(?:export|pdf|xlsx|excel)\b.*\b(?:tasks?|projects?|location|wfh|wfo)\b|\b(?:tasks?|location|wfh|wfo)\b.*\b(?:export|pdf|xlsx|excel)\b',
         'Task exports and attendance exports with WFH/WFO or precise locations are not available yet. Standard attendance PDF/XLSX exports are available.'),
        (r'\bclient\b.*\b(?:tasks?|report|details)\b',
         'Client-wide task reports are not available yet. Please specify a project name instead.'),
        (r'\birregular\b',
         'This analysis is not available in the assistant yet. I can show attendance and recorded leave statuses, but will not infer leave approval or irregularity.'),
    ]
    for pattern, reply in rules:
        if re.search(pattern, message, re.I):
            return reply
    return None


def report_kind(message):
    if re.search(r'\blate\b', message, re.I) and re.search(r'\bpunch(?:ing)?[- ]?ins?\b|\barrivals?\b', message, re.I):
        return 'late_punchin'
    for kind in ('late', 'present', 'absent'):
        if attendance_employee_name(message, kind):
            return kind
    patterns = [
        ('approved_leave', r'\b(?:approved\s+leave|on\s+leave)\b'),
        ('attendance_totals', r'\bcompare\b.*\battendance\b|\battendance\b.*\bcompare\b'),
        ('work_arrangements', r'\bwfh\b|\bwfo\b|\bwork(?:ing)?\s+from\s+(?:home|(?:the\s+)?office)\b|\bwork\s+arrangements?\b'),
        ('missing_location', r'\b(?:missing|no|without)\b.*\b(?:location|gps)\b'),
        ('locations', r'\b(?:locations?|coordinates?)\b|\bwhere\b.*\b(?:attendance|punch)\b'),
        ('missing_punchout', r'\b(?:missing|missed|pending)\b.*\bpunch[- ]?outs?\b'),
        ('late', r'\bwho\b.*\blate\b|\blate\s+(?:arrivals?|employees?|more\s+than|days?|count|attendance)\b'
                 r'|\bhow\s+(?:(?:many|much)\s+)?(?:days?|times|often)\b.*\blate\b'),
        ('present', r'\bwho\b.*\bpresent\b'), ('absent', r'\bwho\b.*\babsent\b'),
        ('task_overruns', r'\b(?:exceed|exceeds|exceeded|overrun)\b|\bbooked\b.*\b(?:more|greater)\b.*\bplanned\b'),
        ('workload', r'\bworkload\b|\bmore\s+than\s+(?:eight|\d+)\s+(?:planned\s+)?hours\b'),
        ('effort_by_project', r'\bprojects\b.*\b(?:time|effort)\b|\b(?:time|effort)\b.*\bprojects\b'),
        ('effort_by_employee', r'\bplanned\b.*\bbooked\b|\bbooked\b.*\bplanned\b'),
        ('booked_time', r'\b' + BOOKING_METRIC + r'\s+(?:effort\s+)?(?:hours?|time|effort)\b|\bhow\s+(?:much|many)\s+(?:time|hours?|effort)\s+did\b.*\b(?:book|log)\b|\bbillable\b'),
    ]
    return next((kind for kind, pattern in patterns if re.search(pattern, message, re.I)), None)


def attendance_employee_name(message, kind):
    status = {'late': 'late', 'late_punchin': 'late', 'present': 'present', 'absent': 'absent'}.get(kind)
    if not status:
        return None
    body = re.sub(INTRO, '', message, flags=re.I)
    body = re.sub(r'\s*\(employee\)', '', body, flags=re.I)
    stop = r'(?=\s+(?:last|this)\s+month\b|\s+(?:on|from|for|today|yesterday)\b|[?.]?$)'
    patterns = [
        r'\blate\s+punch[- ]?ins?\s+(?:of|for)\s+(?P<name>.+?)' + stop,
        r'\bhow\s+(?:(?:many|much)\s+)?(?:days?|times|often)\s+(?:(?:was|is|has|did)\s+)?(?P<name>.+?)\s+(?:(?:was|is|been)\s+)?' + status + r'\b',
        r'\bhow\s+many\s+' + status + r'\s+days\s+(?:did|does)\s+(?P<name>.+?)\s+(?:have|record)\b',
        r'\b' + status + r'\s+(?:days?|count|attendance)\s+(?:of|for)\s+(?:employee\s+)?(?P<name>.+?)' + stop,
        r'^(?:was|is)\s+(?P<name>.+?)\s+' + status + r'\b',
        r'^(?P<name>.+?)\s+(?:total\s+)?' + status + r'\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, body, re.I)
        if not match:
            continue
        value = match['name'].strip(' ?."\'').removesuffix("'s").removesuffix('\u2019s')
        value = re.sub(r'\s+(?:attendance|punch(?:ing)?[- ]?in)(?:\s+(?:was|is))?$', '', value, flags=re.I).strip()
        if (name_tokens(value) and len(value) <= 120 and not any(char.isdigit() for char in value)
                and not set(name_tokens(value)).intersection({'my', 'our', 'team', 'company', 'all', 'employee', 'employees',
                    'who', 'was', 'is', 'did', 'does', 'how', 'many', 'much', 'days', 'times', 'often', 'late',
                    'this', 'that', 'same', 'last', 'month', 'for', 'on', 'tell', 'show', 'give', 'me', 'i', 'we', 'you',
                    'he', 'she', 'they', 'him', 'her', 'total', 'project', 'projects', 'task', 'tasks', 'planned', 'booked',
                    'want', 'know', 'about', 'punch', 'attendance', 'in'})):
            return value
    return None


def booking_employee_name(message):
    body = re.sub(INTRO, '', message, flags=re.I)
    body = re.sub(r'\s*\(employee\)', '', body, flags=re.I)
    stop = r'(?=\s+(?:last|this|previous|current)\s+month\b|\s+(?:on|from|for|today|yesterday)\b|[?.]?$)'
    patterns = [
        r'\b' + BOOKING_METRIC + r'\s+(?:hours?|time)\s+(?:of|for)\s+(?:employee\s+)?(?P<name>.+?)' + stop,
        r'\bhow\s+(?:much|many)\s+(?:time|hours?|effort)\s+did\s+(?P<name>.+?)\s+(?:book|log)\b',
        r'^(?P<name>.+?)\s+(?:(?:last|this|previous|current)\s+month\s+)?(?:total\s+)?(?:effort\s+)?' + BOOKING_METRIC + r'\s+(?:hours?|time)\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, body, re.I)
        if not match:
            continue
        value = match['name'].strip(' ?."\'').removesuffix("'s").removesuffix('\u2019s')
        tokens = name_tokens(value)
        if (tokens and len(value) <= 120 and not any(char.isdigit() for char in value)
                and not set(tokens).intersection({'my', 'our', 'own', 'personal', 'team', 'company', 'employee', 'employees',
                                                 'id', 'all', 'total', 'how', 'many', 'much', 'hours', 'time', 'this', 'that',
                                                 'same', 'last', 'month', 'for', 'on', 'tell', 'show', 'give', 'me', 'i', 'we', 'you', 'he', 'she', 'they',
                                                 'and', 'billable', 'non', 'nonbillable'})):
            return value
    return None


def report_period(message):
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    if re.search(r'\btomorrow\b', message, re.I):
        day = (today + timedelta(days=1)).isoformat()
        return day, day
    if re.search(r'\b(?:this|last)\s+month\b', message, re.I):
        day = today.replace(day=1)
        if re.search(r'\blast\s+month\b', message, re.I):
            day = (day - timedelta(days=1)).replace(day=1)
        return day.isoformat(), day.replace(day=calendar.monthrange(day.year, day.month)[1]).isoformat()
    if re.search(r'\bthis\s+week\b', message, re.I):
        first = today - timedelta(days=today.weekday())
        return first.isoformat(), (first + timedelta(days=6)).isoformat()
    return period(message)


def render_report(data, *, include_names=True):
    title = TITLES[data['report']]
    dates = f"{data['start_date']} to {data['end_date']}" if data['start_date'] else 'current configuration'
    lines = [f'{title} | {dates}']
    daily_status = data['report'] in ('present', 'absent') and data['start_date'] == data['end_date']
    if daily_status:
        operational = data.get('absence_basis') == 'not_checked_in'
        label = 'not checked in' if operational else 'present' if data['report'] == 'present' else 'marked absent'
        if operational:
            title = 'Working-day absence'
        lines = [f"{title} | {data['start_date']}",
                 f"Employees {label}: {sum(row['count'] > 0 for row in data['rows'])}."]
        coverage = data.get('attendance_coverage', {})
        if operational:
            lines.append(f"Expected to work: {coverage['expected_employee_count']}. Excluded for leave, holiday or week-off: {coverage['excluded_employee_count']}.")
        elif coverage.get('employee_count') and coverage.get('unclassified_employee_count') == coverage['employee_count'] and coverage.get('punch_in_employee_count') == 0:
            lines.append(f"No punch-ins are recorded for this date. All {coverage['employee_count']} employees remain unclassified; absence has not been confirmed.")
        elif coverage.get('unclassified_employee_count'):
            lines.append(f"{coverage['unclassified_employee_count']} employees have unclassified attendance and are not counted as absent.")
    if data['report'] == 'booked_time' and data.get('project_id'):
        lines.insert(0, 'Project: ' + data['project_name'])
        rows = data['rows']
        available = not rows or any(row['booked_minutes'] is not None for row in rows)
        total = duration(sum(row['booked_minutes'] or 0 for row in rows)) if available else 'not available'
        lines.append(f"Total booked: {total} across {sum(row['entry_count'] for row in rows)} timesheet entries.")
        parts = []
        for key, label in [('billable', 'Billable'), ('non_billable', 'Non-billable'), ('unclassified', 'Unclassified billing')]:
            buckets = [row['billing'][key] for row in rows]
            entries = sum(bucket['entry_count'] for bucket in buckets)
            if key == 'unclassified' and entries == 0:
                continue
            known = entries == 0 or any(bucket['entry_count'] and bucket['minutes'] is not None for bucket in buckets)
            value = duration(sum(bucket['minutes'] or 0 for bucket in buckets)) if known else 'not available'
            parts.append(f'{label}: {value}')
        lines.append(' | '.join(parts))
    if not data['rows'] and not daily_status:
        lines.append('No matching records were found within your access permissions.')
    for row in data['rows']:
        if daily_status and not include_names:
            continue
        name = row.get('employee_name', row.get('project_name', ''))
        if data['report'] == 'late_punchin':
            if not row['assessable']:
                lines.append(f'{name}: late punch-ins cannot be assessed because no late cutoff is configured.')
            else:
                lines.append(f"{name}: {row['count']} late punch-in day(s). Cutoff: {row['late_after']}.")
                for entry in row['entries']:
                    lines.append(f"{entry['date']}: punch-in {entry['punch_in']}, {entry['late_minutes']} minutes after the cutoff.")
        elif daily_status:
            lines.append(name if row['count'] else f'{name}: checked in or excluded from working-day absence.' if operational else f'{name}: not recorded as {label}.')
        elif data['report'] in ('late', 'missing_punchout', 'present', 'absent'):
            dates_list = f" - {', '.join(row['dates'])}" if row['dates'] else ''
            label = 'late day' if data['report'] == 'late' else 'day'
            lines.append(f"{name}: {row['count']} {label}{'s' if row['count'] != 1 else ''}{dates_list}.")
        elif data['report'] == 'booked_time':
            booked = duration(row['booked_minutes']) if row['booked_minutes'] is not None else 'not available'
            lines.append(f"{name}: {booked} booked across {row['entry_count']} timesheet entries.")
            parts = []
            for key, label in [('billable', 'Billable'), ('non_billable', 'Non-billable'), ('unclassified', 'Unclassified billing')]:
                bucket = row['billing'][key]
                if key == 'unclassified' and bucket['entry_count'] == 0:
                    continue
                value = duration(bucket['minutes']) if bucket['minutes'] is not None else 'not available'
                parts.append(f'{label}: {value}')
            lines.append(' | '.join(parts))
            if row['unavailable_count']:
                lines.append(f"{row['unavailable_count']} entry/entries have no recorded duration and are excluded from the total.")
        elif data['report'] == 'approved_leave':
            withdrawal = ' (withdrawal pending)' if row['status'] == 'withdraw_requested' else ''
            lines.append(f"{name}: {row['start_date']} to {row['end_date']}{withdrawal}.")
        elif data['report'] == 'attendance_totals':
            counts = '; '.join(f'{label}: {count}' for label, count in row['status_counts'].items())
            lines.append(f"{name}: {counts}; worked {duration(row['worked_minutes'])}.")
        elif data['report'] == 'work_arrangements':
            lines.append(f"{name}: {row['arrangement']}.")
        elif data['report'] in ('locations', 'missing_location'):
            lines.append(f"{name} | {row['date']}")
            for key, label in [('punch_in', 'Punch-in'), ('punch_out', 'Punch-out')]:
                point = row[key]
                place = f"{point['latitude']}, {point['longitude']}" if point else 'GPS not recorded'
                address = row[key + '_address']
                lines.append(f'{label}: {address + "; " if address else ""}{place}.')
        else:
            booked = duration(row['booked_minutes']) if row['booked_minutes'] is not None else 'not available'
            task = f" | Task #{row['task_id']}" if 'task_id' in row else ''
            count = f" | {row['task_count']} tasks" if 'task_count' in row else ''
            lines.append(f"{name}{task}{count}: planned {duration(row['planned_minutes'])}; booked {booked}.")
    if data['details_truncated']:
        lines.append('Showing the first 50 results. Narrow the period or employee selection for more detail.')
    lines.append(data['notice'])
    return '\n'.join(lines)


async def manager_reply(message, context, identity, backend):
    message = query_text(message)
    unsupported = capability_reply(message)
    if unsupported:
        return (AssistantReply(unsupported, model_content='An unavailable capability was explained locally.', query_context={}), [], [])
    kind = report_kind(message)
    named_reports = ('booked_time', 'late', 'late_punchin', 'present', 'absent', 'missing_punchout')
    pending_name = context.get('kind') == 'manager_report' and context.get('report') in named_reports and context.get('needs_employee_name')
    name_reply = message.strip(' ?."\'') if pending_name and re.fullmatch(r"[\w\s'\u2019-]{2,120}[?.]?", message.strip()) and not re.search(
        r'\b(?:hi|hello|thanks|show|tell|attendance|tasks?|today|yesterday|month|cancel)\b', message, re.I) and not any(char.isdigit() for char in message) else None
    id_reply = pending_name and re.fullmatch(r'\s*employee\s*(?:id\s*[:#]?\s*)?#?\s*\d+\s*[?.]?\s*', message, re.I)
    billing_followup = (context.get('kind') == 'manager_report' and context.get('report') == 'booked_time'
                        and BILLING_QUESTION.search(message) and not booking_employee_name(message)
                        and not re.search(r'\b(?:my|team|company|employee\s*(?:id|#|\d))\b', message, re.I))
    followup = context.get('kind') == 'manager_report' and (billing_followup or re.fullmatch(r'\s*(?:for\s+)?(?:[\d/\-]+(?:\s+to\s+[\d/\-]+)?|today|yesterday|tomorrow|this month|last month|last 7 days|this week)\s*[?.]?\s*', message, re.I)
                                                        or re.search(r'\b(?:this|that|same)\s+employee\b', message, re.I) or name_reply or id_reply)
    punch_followup = (kind == 'late_punchin' and context.get('kind') == 'manager_report'
                      and not attendance_employee_name(message, kind)
                      and not re.search(r'\b(?:my|team|company|employee\s*(?:id|#|\d))\b', message, re.I))
    followup = bool(followup or punch_followup)
    if not kind and not followup:
        return None
    kind = kind or context['report']
    module = 'tasks' if kind in ('effort_by_employee', 'effort_by_project', 'task_overruns', 'workload', 'booked_time') else 'leave' if kind == 'approved_leave' else 'attendance'
    if module not in identity.modules:
        raise HTTPException(403, 'This report requires the relevant module license and read permission.')
    if kind == 'work_arrangements' and re.search(r'\b(?:today|yesterday|tomorrow|month|approved|approval|days|\d{2}/\d{2}/20\d{2}|20\d{2}-\d{2})\b|\b(?:working|worked)\s+from\b', message, re.I):
        return (AssistantReply('Daily WFH/WFO selections and approvals are not available in these records. Ask "Show my team\'s permitted work arrangements" for current configuration.',
                               model_content='Daily work-mode data is unavailable.', query_context={}), [], [])
    if module == 'tasks' and re.search(r'\bproject\s+\S', message, re.I):
        return ('For project-specific details, ask "Show project NAME tasks for YYYY-MM". Project-filtered insight reports are not available yet.', [], [])
    scope = (context['scope'] if followup else 'team' if re.search(r'\b(?:my|our|the)\s+team\b', message, re.I)
             else 'company' if re.search(r'\bcompany\b', message, re.I)
             else 'self' if re.search(r'\b(?:my|own|personal)\b|\bwhere\s+did\s+i\b|\b(?:did|was|have)\s+i\b', message, re.I)
             else 'company' if re.search(r'\bcompany\b', message, re.I) or 'company' in identity.scopes
             else 'team' if 'team' in identity.scopes else 'self')
    if scope not in identity.scopes:
        raise HTTPException(403, 'This report is outside your permitted employee scope.')
    selected = dict(context) if followup else {'kind': 'manager_report', 'report': kind, 'scope': scope, 'source': 'local_manager_report'}
    selected.update(report=kind)
    if kind in named_reports:
        try:
            dates = report_period(message)
            if dates:
                ManagerReportArgs(scope=scope, report=kind, start_date=dates[0], end_date=dates[1])
                selected.update(start_date=dates[0], end_date=dates[1])
        except (ValueError, ValidationError):
            return (AssistantReply('Please specify a valid booking period of up to 93 days.', model_content='Booking period needs clarification.', query_context=selected), [], [])
    employee = re.search(r'\bemployee\s*(?:id\s*[:#]?\s*)?#?\s*(\d+)\b', message, re.I)
    if employee:
        selected['employee_id'] = int(employee[1])
    name = re.search(r'\bfor\s+employee\s+(?!id\b|\d)(.+?)(?=\s+(?:on|from|for)\s|[?.]?$)', message, re.I)
    location_name = re.search(r'\bwhere\s+did\s+(.+?)\s+(?:mark|punch)', message, re.I)
    if location_name and location_name[1].strip().lower() not in ('i', 'this employee', 'that employee', 'my team'):
        name = location_name
    used = []
    booking_name = name_reply or (booking_employee_name(message) if kind == 'booked_time' else attendance_employee_name(message, kind))
    if booking_name:
        selected['employee_name'] = booking_name
        selected.pop('employee_id', None)
    elif name:
        selected['employee_name'] = name[1].strip()
    if (kind == 'booked_time' and not selected.get('employee_name') and not selected.get('employee_id')
            and not re.search(r'\b(?:my|own|personal|team|company|all\s+employees)\b|\bdid\s+i\s+(?:book|log)\b', message, re.I) and not followup):
        selected['needs_employee_name'] = True
        return (AssistantReply('Whose booked hours should I check? Please provide an employee name or ID, or specify your team.',
                               model_content='Booking employee needs clarification.', query_context=selected), [], [])
    if (kind in ('late', 'late_punchin', 'present', 'absent') and re.search(r'\bhow\s+(?:(?:many|much)\s+)?(?:(?:late|present|absent)\s+)?(?:days?|times|often)\b', message, re.I)
            and scope != 'self' and not selected.get('employee_name') and not selected.get('employee_id')
            and not re.search(r'\b(?:team|company|all\s+employees)\b', message, re.I) and not followup):
        selected['needs_employee_name'] = True
        return (AssistantReply('Which employee should I check? Please provide their name or employee ID.',
                               model_content='Attendance employee needs clarification.', query_context=selected), [], [])
    if selected.get('employee_name') and not selected.get('employee_id'):
        if module == 'leave':
            return ('Please specify the employee ID or your permitted team for approved-leave queries. Leave name lookup is not available yet.', [], [])
        result = await backend.find_employee(identity, scope, selected['employee_name'], module)
        used.append('employee_search')
        if result['has_more'] or len(result['matches']) != 1:
            if kind in named_reports:
                selected.pop('employee_id', None)
                selected.pop('employee_name', None)
                selected['needs_employee_name'] = True
            return (AssistantReply('Please specify the full employee name or ID; I could not find one unique authorized match.',
                                   model_content='Employee clarification required.', query_context=selected if kind in named_reports else {}), used, [])
        selected['employee_id'] = result['matches'][0]['employee_id']
        selected['needs_employee_name'] = False
    if kind == 'late_punchin' and selected.get('needs_employee_name') and not selected.get('employee_id'):
        return (AssistantReply('Which employee should I check for late punch-ins? Please provide their name or employee ID.',
                               model_content='Punch-in employee needs clarification.', query_context=selected), used, [])
    if re.search(r'\b(?:this|that|same)\s+employee\b', message, re.I) and not selected.get('employee_id'):
        ids = context.get('employee_ids', [])
        if len(ids) != 1:
            return ('Which employee? Please provide their name or employee ID.', [], [])
        selected['employee_id'] = ids[0]
    threshold = re.search(r'\bmore\s+than\s+(\d+)\s+times\b', message, re.I)
    hours = re.search(r'\bmore\s+than\s+(eight|\d+)\s+(?:planned\s+)?hours\b', message, re.I)
    value = int(threshold[1]) if threshold else (8 if hours[1].lower() == 'eight' else int(hours[1])) * 60 if hours else selected.get('threshold', 0) if followup else 0
    selected['threshold'] = value
    try:
        dates = None if kind == 'work_arrangements' else report_period(message)
        if (name_reply or id_reply or billing_followup or punch_followup) and not dates:
            dates = (selected.get('start_date'), selected.get('end_date'))
            if not all(dates):
                dates = None
        compare = kind == 'attendance_totals' and re.search(r'\b(?:with|against|versus)\s+last\s+month\b', message, re.I)
        if compare:
            dates = report_period('this month')
        elif kind == 'attendance_totals' and re.search(r'\bcompare\b', message, re.I):
            return ('For attendance comparisons, ask "Compare my team attendance with last month". Other comparison periods are not supported yet.', [], [])
        if kind != 'work_arrangements' and not dates:
            return (AssistantReply('Which date or period should I check? You can use yesterday, YYYY-MM, or a date range.',
                                   model_content='Report period required.', query_context=selected), used, [])
        dates = dates or (None, None)
        args = ManagerReportArgs(scope=scope, report=kind, employee_id=selected.get('employee_id'),
                                 start_date=dates[0], end_date=dates[1], threshold=value)
    except (ValueError, ValidationError):
        return (AssistantReply('Please specify a valid period of up to 93 days and a valid threshold.',
                               model_content='Invalid report period.', query_context=selected), used, [])
    data = await backend.manager_report(identity, args)
    text = render_report(data)
    if compare:
        previous = report_period('last month')
        old = await backend.manager_report(identity, args.model_copy(update={'start_date': date.fromisoformat(previous[0]),
                                                                            'end_date': date.fromisoformat(previous[1])}))
        text += '\n\n' + render_report(old) + '\nCurrent month is incomplete; full-month totals are not directly comparable.'
    selected.update(start_date=dates[0], end_date=dates[1], employee_ids=data['employee_ids'])
    selected['needs_employee_name'] = False
    return (AssistantReply(text, model_content='An authorized manager report was displayed locally.', query_context=selected),
            used + ['get_manager_report'], [])
