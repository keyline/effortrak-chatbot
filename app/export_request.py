import re

from app.employee_lookup import named_attendance
from app.schemas import ExportArgs


def export_request(message: str, identity, context: dict | None = None):
    context = context or {}
    date_only = re.fullmatch(r'\s*(?:20\d{2}-\d{2}-\d{2}|\d{2}/\d{2}/20\d{2})(?:\s+to\s+(?:20\d{2}-\d{2}-\d{2}|\d{2}/\d{2}/20\d{2}))?\s*[?.]?\s*', message, re.I)
    if date_only and context.get('kind') == 'attendance_export':
        ids = context.get('employee_ids', [])
        if len(ids) != 1:
            raise ValueError('Select one employee for the new period.')
        message = f"Export {context['scope']} attendance for employee ID {ids[0]} for {message.strip(' ?.')} as {context.get('format', 'pdf')}"
    report = re.search(r'\battendance\b.*\breport\b|\breport\b.*\battendance\b', message, re.I)
    if not (report or re.search(r'\b(?:export|download|pdf|xlsx|excel)\b', message, re.I)) or re.search(r'\btasks?\b', message, re.I):
        return None
    scope = 'company' if re.search(r'\bcompany\b', message, re.I) else 'team' if re.search(r'\bteam\b', message, re.I) else 'self'
    employee = re.search(r'\bemployee\s+(?:id\s*)?#?\s*(\d+)\b', message, re.I)
    clean = re.sub(r'\b(?:export|download|generate|pdf|xlsx|excel)\b', '', message, flags=re.I).strip()
    clean = re.sub(r'\bfrom\b', 'on', clean, flags=re.I)
    named = named_attendance(clean)
    if named and employee:
        raise ValueError('Specify either an employee name or ID, not both.')
    if named:
        scope = 'company' if 'company' in identity.scopes else 'team' if 'team' in identity.scopes else 'self'
    dates = re.findall(r'\b(?:20\d{2}-\d{2}-\d{2}|\d{2}/\d{2}/20\d{2})\b', message)
    dates = [value if '/' not in value else '-'.join(reversed(value.split('/'))) for value in dates]
    month = re.search(r'\b20\d{2}-(?:0[1-9]|1[0-2])\b', message)
    relative = re.search(r'\btoday\b|\blast\s+(?:7|seven)\s+days\b', message, re.I)
    if relative and (dates or month):
        raise ValueError('Specify a relative report or explicit dates, not both.')
    if re.search(r'\bfrom\b.*\b(?:to|through|until)\b', message, re.I) and len(dates) != 2:
        raise ValueError('Specify both range endpoints.')
    args = dict(scope=scope, employee_id=int(employee[1]) if employee else None,
                format='xlsx' if re.search(r'\b(?:xlsx|excel)\b', message, re.I) else 'pdf')
    if len(dates) == 2:
        args.update(report_type='custom_range', start_date=dates[0], end_date=dates[1])
    elif len(dates) == 1:
        args.update(report_type='custom_range', start_date=dates[0], end_date=dates[0])
    elif len(dates) > 2:
        raise ValueError('Specify one start date and one end date.')
    elif re.search(r'\blast\s+(?:7|seven)\s+days\b', message, re.I):
        args['report_type'] = 'last_7_days'
    elif re.search(r'\btoday\b', message, re.I):
        args['report_type'] = 'today'
    elif month:
        args.update(report_type='monthly_details', month=month[0])
    else:
        raise ValueError('Specify Today, Last 7 Days, a YYYY-MM month, or a DD/MM/YYYY date range.')
    generic = re.match(r'^attendance\s+(?:for\s+)?(?:20\d{2}-|today\b|last\s+)', clean, re.I)
    if not named and not employee and not generic and not re.search(r'\b(?:my|self|personal|team|company|our|all)\b', message, re.I):
        raise ValueError('Specify your own attendance, an employee name/ID, team, or company.')
    return ExportArgs.model_validate(args), named[0] if named else None
