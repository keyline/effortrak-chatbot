"""Local, read-only client visit and recorded-location questions."""

import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import Field, ValidationError, model_validator, field_validator

from app.manager_query import report_period
from app.schemas import StrictModel
from app.task_report import AssistantReply


class VisitArgs(StrictModel):
    action: str = Field(pattern=r'^(visits|location|lms_location)$')
    scope: str = Field(pattern=r'^(self|team|company)$')
    employee_id: int | None = Field(default=None, ge=1)
    employee_name: str | None = Field(default=None, min_length=2, max_length=120)
    client_name: str | None = Field(default=None, min_length=2, max_length=120)
    start_date: str | None = None
    end_date: str | None = None
    latest: bool = False
    page: int = Field(default=1, ge=1, le=100)

    @model_validator(mode='after')
    def dates(self):
        if self.start_date or self.end_date:
            start = datetime.strptime(self.start_date or '', '%Y-%m-%d').date()
            end = datetime.strptime(self.end_date or '', '%Y-%m-%d').date()
            if not 0 <= (end - start).days <= 92:
                raise ValueError('Use up to 93 days.')
        return self


class LocationData(StrictModel):
    address: str = Field(max_length=500)
    coordinates: list[float] | None
    maps_url: str | None = Field(default=None, max_length=250)

    @field_validator('coordinates')
    @classmethod
    def valid_coordinates(cls, point):
        import math
        if point is not None and (len(point) != 2 or not all(math.isfinite(x) for x in point)
                                  or abs(point[0]) > 90 or abs(point[1]) > 180 or point == [0, 0]):
            raise ValueError('Invalid coordinates')
        return point


class VisitRow(StrictModel):
    employee_name: str | None = Field(default=None, max_length=500)
    client_name: str | None = Field(default=None, max_length=500)
    recorded_at: str | None = Field(pattern=r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
    status: str | None = Field(default=None, max_length=100)
    accuracy_m: float | None = Field(default=None, ge=0)
    is_mock: bool = False
    location: LocationData


class VisitData(StrictModel):
    status: str = Field(pattern=r'^answered$')
    action: str = Field(pattern=r'^(visits|location|lms_location)$')
    start_date: str | None
    end_date: str | None
    as_of: str = Field(pattern=r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
    timezone: str = Field(pattern=r'^Asia/Kolkata$')
    rows: list[VisitRow] = Field(max_length=20)
    has_more: bool
    page: int = Field(ge=1, le=100)


def clean(value):
    value = re.sub(r"(?:'s|\u2019s)\b", '', value, flags=re.I)
    value = re.sub(r'^(?:the\s+|employee\s+)', '', value.strip(), flags=re.I)
    return value.strip(' ?.,"\'') or None


def parse_question(message):
    """Extract only explicit names; never guess a client/employee ID."""
    text = message.strip().replace('\u2019', "'")
    if re.search(r'\b(?:attendance|punch[- ]?(?:in|out)?)\b', text, re.I):
        return None
    if not re.search(r'\b(?:visit(?:ed|s|ing|ors)?|went|location|where\s+(?:is|was))\b', text, re.I):
        return None
    if re.search(r'\b(?:how\s+(?:to|do|can)|add|create|delete|edit|schedule|assign|cancel|export)\b', text, re.I):
        return None
    latest = bool(re.search(r'\b(?:last\s+visited|(?:latest|last)\s+(?:client\s+)?visit|most\s+recent|recently)\b', text, re.I))
    days = re.search(r'\b(?:last|past|previous)\s+(\d+|x|seven|thirty|ten)\s+days?\b', text, re.I)
    dates = None
    error = None
    if days:
        words = {'seven': 7, 'thirty': 30, 'ten': 10}
        count = words.get(days[1].lower())
        if count is None and days[1].isdigit(): count = int(days[1])
        if count is None or not 1 <= count <= 93:
            error = 'How many days should I check? Please choose between 1 and 93 days.'
        else:
            today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
            dates = ((today - timedelta(days=count - 1)).isoformat(), today.isoformat())
        text = text[:days.start()] + text[days.end():]
    else:
        try:
            dates = report_period(text)
        except (ValueError, ValidationError):
            error = 'Please specify valid dates, using DD/MM/YYYY or YYYY-MM-DD, for up to 93 days.'
    if dates and re.search(r'\bthis\s+(?:month|week)\b', text, re.I):
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
        dates = (dates[0], min(dates[1], today))
    # Remove dates only after parsing them. Do not turn an invalid period into an unbounded lookup.
    text = re.sub(r"\b(?:today|yesterday|tomorrow)(?:'s)?\b|\b(?:this|last|previous|current)\s+(?:week|month)\b", '', text, flags=re.I)
    text = re.sub(r'\b(?:from\s+)?(?:\d{2}/\d{2}/20\d{2}|20\d{2}-\d{2}(?:-\d{2})?)(?:\s+(?:to|through|until|and)\s+(?:\d{2}/\d{2}/20\d{2}|20\d{2}-\d{2}(?:-\d{2})?))?', '', text, flags=re.I)
    text = re.sub(r'^\s*(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:(?:show|tell|give|list|find|get)(?:\s+me)?\s+)?', '', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip(' ?.,')
    text = re.sub(r'^all\s+(?=visits?\b)', '', text, flags=re.I)
    text = re.sub(r"'s\b", '', text, flags=re.I)
    text = re.sub(r'\s+(?:along\s+with|with)\s+(?:the\s+)?date.*$', '', text, flags=re.I)
    text = re.sub(r'\s+(?:in|for|during|on|over)(?:\s+the)?$', '', text, flags=re.I)
    employee = client = None
    action = 'visits'
    if re.search(r'\blms\b', text, re.I):
        if re.search(r'\bvisit', text, re.I):
            return {'error': 'LMS follow-ups are scheduled activities, not GPS-confirmed visits. Ask for a recorded client visit or an LMS lead location.'}
        action = 'lms_location'
        match = re.search(r'(?:location\s+(?:of|for)\s+(?:lms\s+)?(?:lead|client)?\s*|(?:lms\s+)?(?:lead|client)\s+)(.+?)(?:\s+location)?$', text, re.I)
        client = clean(match[1]) if match else None
    elif re.search(r'\blocation\b|^where\s+(?:is|was)\b', text, re.I) and not re.search(r'\bvisit|\bclient\b', text, re.I):
        action = 'location'
        latest = True
        text = re.sub(r'\b(?:current|live|latest|last\s+known|recorded|adaptive|gps)\b', '', text, flags=re.I)
        text = re.sub(r'\s+', ' ', text).strip()
        match = re.search(r'^where\s+(?:is|was)\s+(.+?)(?:\s+now)?$|^location\s+(?:of|for)\s+(.+?)$|^(.+?)\s+location$', text, re.I)
        employee = clean(next((v for v in match.groups() if v), '')) if match else None
    else:
        patterns = [
            (r'^(.+?)\s+client\s+(?:visits?\s+by|visited\s+by)\s+(.+)$', 'both_client_first'),
            (r'^(.+?)\s+client\s+(?:visited\s+employee\s+list|visitors|visits)$', 'client'),
            (r'^employees\s+who\s+visited\s+(.+)$', 'client'),
            (r'^(?:visitors|employees\s+who\s+visited)\s+(?:to|at|for)\s+(.+)$', 'client'),
            (r'^(?:who|which\s+employees?)\s+(?:has\s+|have\s+)?(?:visited|went\s+to|checked\s+in\s+at)\s+(.+)$', 'client'),
            (r'^which\s+client\s+(?:did|has)\s+(.+?)\s+(?:(?:last|most\s+recently)\s+)?visit(?:ed)?(?:\s+most\s+recently)?$', 'employee'),
            (r'^(.+?)\s+(?:last\s+visited|most\s+recently\s+visited)\s*(?:client)?$', 'employee'),
            (r'^(.+?)\s+client\s+(.+?)\s+visit(?:ed|s)?(?:\s+(?:list|history))?$', 'both_client_first'),
            (r'^(.+?)\s+(?:visits?\s+(?:to|at)|(?:last\s+)?visited|went\s+to)\s+(?:client\s+)?(.+?)(?:\s+(?:list|history))?$', 'both'),
            (r'^(?:visits?|visit\s+(?:list|history))\s+(?:of|for|by)\s+(.+?)(?:\s+(?:to|at)\s+(?:client\s+)?(.+))?$', 'both'),
            (r'^(.+?)\s+(?:(?:latest|last)\s+)?(?:client\s+)?visits?(?:\s+(?:list|history|details))?$', 'employee'),
            (r'^(.+?)\s+client\s+(?:visited\s+employee\s+list|visitors|visits)$', 'client'),
            (r'^(?:latest|last)\s+client\s+visited\s+by\s+(.+)$', 'employee'),
        ]
        for pattern, kind in patterns:
            match = re.search(pattern, text, re.I)
            if not match: continue
            if kind == 'client': client = clean(match[1])
            elif kind == 'employee': employee = clean(match[1])
            elif kind == 'both_client_first': client, employee = clean(match[1]), clean(match[2])
            else:
                employee = clean(match[1])
                client = clean(match[2]) if match.lastindex == 2 and match[2] else None
            break
    if client:
        client = re.sub(r'^client\s*:?\s+', '', client, flags=re.I)
        client = re.sub(r'\s+client$', '', client, flags=re.I).strip() or None
    return dict(action=action, employee_name=employee, client_name=client, latest=latest,
                dates=dates, error=error)


def render(data):
    action = data['action']
    title = {'visits': 'Recorded client visits', 'location': 'Last recorded employee location',
             'lms_location': 'LMS lead location (saved record, not an employee visit)'}[action]
    lines = [title]
    if data.get('start_date'): lines.append(f"Period: {data['start_date']} to {data['end_date']} (inclusive)")
    lines.append(f"As of: {data['as_of']} ({data['timezone']})")
    if not data['rows']: lines.append('No matching records were found within your authorized scope.')
    for row in data['rows']:
        lines.append(' | '.join(str(row[k]) for k in ('employee_name', 'client_name') if row.get(k)))
        lines.append(('Record updated: ' if action == 'lms_location' else 'Captured: ') + str(row.get('recorded_at') or 'Not recorded'))
        if action == 'visits': lines.append('Status: Recorded check-in. Check-out time is not stored.')
        location = row['location']
        lines.append('Location: ' + (location['address'] or 'Address unavailable'))
        if location.get('coordinates'): lines.append('Coordinates: ' + ', '.join(map(str, location['coordinates'])))
        if location.get('coordinates'):
            lat, lng = location['coordinates']
            lines.append(f'Open in Maps: https://www.google.com/maps/search/?api=1&query={lat}%2C{lng}')
        if row.get('accuracy_m') is not None: lines.append(f"Recorded GPS accuracy: {row['accuracy_m']} m")
        if row.get('is_mock'): lines.append('Warning: this point was marked as mock GPS.')
        lines.append('')
    if action == 'location': lines.append('This is the latest stored GPS point, not a live device request. The capture time may be old; current presence is not verified.')
    elif action == 'visits': lines.append('Source: Field Sales recorded check-ins, not planned visits. GPS capture does not prove physical presence.')
    lines.append('Includes only active employees and records within your authorized scope.')
    if data['has_more']: lines.append('Showing 20 records, newest first. Ask for the next page or narrow the period.')
    return '\n'.join(lines).strip()


async def visit_reply(message, context, identity, backend):
    previous = context.get('kind') == 'visit_location'
    parsed = parse_question(message)
    if parsed is None and re.search(r'\b(?:attendance|tasks?|efforts?|payroll|leave|help|hello|hi)\b', message, re.I):
        return None
    followup = previous and (re.fullmatch(r'\s*(?:next(?:\s+page)?|more|(?:for\s+)?(?:the\s+)?(?:last|past)\s+\d+\s+days|(?:for\s+)?20\d{2}-\d{2}(?:-\d{2})?)\s*[?.]?\s*', message, re.I)
                            or context.get('pending') in ('period', 'employee', 'client'))
    if parsed is None and not followup: return None
    selected = dict(context.get('args', {})) if followup else {}

    def reply(text, pending=None, used=None):
        return (AssistantReply(text, model_content='An authorized visit/location request was handled locally.',
                               query_context={'kind': 'visit_location', 'source': 'local_visit_location', 'args': selected, 'pending': pending}), used or [], [])

    if parsed is not None:
        selected = {k: v for k, v in parsed.items() if k in ('action', 'employee_name', 'client_name', 'latest')}
        if parsed.get('error'): return reply(parsed['error'], 'period')
        if parsed['dates']: selected.update(start_date=parsed['dates'][0], end_date=parsed['dates'][1])
    elif context.get('pending') in ('employee', 'client'):
        field = context['pending']
        match = re.fullmatch(r'\s*(?:employee\s+(?:id\s*)?)?#?(\d+)\s*', message, re.I) if field == 'employee' else None
        if match:
            selected.update(employee_id=int(match[1]), employee_name=None)
        else: selected[field + '_name'] = clean(message)
    elif re.fullmatch(r'\s*(?:next(?:\s+page)?|more)\s*[?.]?\s*', message, re.I):
        if not context.get('has_more'): return reply('There are no more records in this report.')
        selected['page'] = selected.get('page', 1) + 1
    else:
        period_text = 'last ' + message.strip() + ' days' if context.get('pending') == 'period' and re.fullmatch(r'\s*\d+\s*', message) else message
        extra = parse_question('visits for my ' + period_text)
        if extra and extra.get('error'): return reply(extra['error'], 'period')
        dates = extra.get('dates') if extra else None
        if not dates: return reply('Which date or period should I check? Use today, last 7 days, or a date range.', 'period')
        selected.update(start_date=dates[0], end_date=dates[1], page=1)
    employee = selected.get('employee_name')
    employee_id = re.fullmatch(r'(?:employee\s+)?(?:id\s*[:#]?\s*|#)(\d+)', employee or '', re.I)
    if employee_id:
        selected.update(employee_id=int(employee_id[1]), employee_name=None)
        employee = None
    personal = (employee is not None and employee.lower() in ('my', 'me', 'i', 'my own')) or (
        bool(followup) and selected.get('scope') == 'self' and not employee and not selected.get('employee_id'))
    if personal: selected['employee_name'] = None
    selected['scope'] = 'self' if personal else 'company' if 'company' in identity.scopes else 'team' if 'team' in identity.scopes else 'self'
    if selected.get('action') == 'location' and not personal and not employee and not selected.get('employee_id'):
        return reply('Which employee should I check? Please provide their full name.', 'employee')
    if selected.get('action') == 'visits' and not personal and not employee and not selected.get('client_name') and not selected.get('employee_id'):
        return reply('Which employee or client should I check? Please include the name in your question.')
    if not selected.get('latest') and not selected.get('start_date') and selected.get('action') != 'lms_location':
        return reply('Which date or period should I check? Use today, last 7 days, or a date range.', 'period')
    try:
        args = VisitArgs.model_validate(selected)
    except (ValueError, ValidationError):
        return reply('Please specify a valid employee/client name and an inclusive period of up to 93 days.', 'period')
    data = await backend.call('visit-location', identity.credential, args.model_dump())
    if data.get('status') == 'clarify' and data.get('subject') in ('employee', 'client'):
        subject = data['subject']
        return reply(f'Please specify the full {subject} name; I could not find one unique authorized match.', subject, ['get_visit_location'])
    try:
        data = VisitData.model_validate(data).model_dump()
        if data['action'] != args.action:
            raise ValueError('Unexpected action')
    except (ValueError, ValidationError):
        raise HTTPException(502, 'Invalid visit/location response.') from None
    selected['page'] = args.page
    result = reply(render(data), used=['get_visit_location'])
    result[0].query_context['has_more'] = data['has_more']
    return result
