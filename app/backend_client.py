import base64
import json
import re
from datetime import timedelta
from uuid import uuid4

import httpx
from fastapi import HTTPException
from pydantic import Field, ValidationError

from app.auth import Identity
from app.config import Settings
from app.schemas import StrictModel
from app.task_report import TaskResult, task_result
from app.employee_lookup import name_tokens
from app.work_report import WorkData, format_work_report


class BackendContext(StrictModel):
    company_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    name: str
    designation: str
    scopes: list[str]
    can_export: bool
    policy_version: str = Field(pattern=r'^[a-f0-9]{64}$')
    data_source: str
    modules: list[str] = Field(default_factory=lambda: ['attendance'])


class BackendClient:
    def __init__(self, config: Settings):
        self.config = config
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def call(self, endpoint: str, token: str, body: dict | None = None) -> dict:
        url = self.config.backend_url.rstrip('/') + '/apiv2/ai/internal/' + endpoint
        headers = {'X-AI-Service-Key': self.config.ai_service_secret.get_secret_value()}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            response = await self.client.post(url, json=body or {}, headers=headers)
        except httpx.HTTPError:
            raise HTTPException(502, 'Effortrak backend is unavailable or timed out.') from None
        if response.status_code >= 400:
            status = response.status_code if response.status_code in (401,403,404,409,413,422,429,503) else 502
            # Forward only audited messages; arbitrary backend details remain private.
            if status == 429 and endpoint == 'token-reserve':
                raise HTTPException(429, 'Company AI token allowance is insufficient for this request. Contact your administrator.')
            if endpoint == 'work-report' and status in (413, 422):
                try:
                    message = response.json().get('message')
                except (ValueError, AttributeError):
                    message = None
                if message in ('Work reports support up to 31 days.', 'Future work records are unavailable.',
                               'Select an employee or a team of at most 10 employees for work reports.'):
                    raise HTTPException(status, message)
            if status == 403:
                try:
                    message = response.json().get('message')
                except (ValueError, AttributeError):
                    message = None
                safe_messages = {
                    'Daily report permission is required for work reports.':
                        'Your role needs Daily Report permission to view employee work summaries.',
                    'Effort report permission is required.':
                        'Your role needs Effort Report permission to view timesheet booking totals.',
                    'Attendance location permission is required.':
                        'Your role needs the separate attendance-location permission to view recorded locations.',
                    'Employee is outside your permitted scope.':
                        'The requested employee is not available within your authorized scope. '
                        'Choose an employee you are permitted to view; you can still ask about your own attendance.',
                    'Employee scope is not permitted.':
                        'You do not have permission for the requested employee scope. You can still ask about your own attendance.',
                    'Module read permission is not granted for this scope.':
                        'You do not have permission to read this module in the requested scope. Ask your administrator to check the module permissions.',
                    'You do not have report export permission.':
                        'You do not have permission to export reports. You can still ask to view authorized attendance.',
                    'You do not have permission to use the AI assistant.':
                        'Your role does not have permission to use the AI assistant.',
                    'AI assistant is disabled for your company.':
                        'AI assistant is disabled for your company.',
                    'No licensed AI-readable modules are available.':
                        'No licensed AI-readable modules are available for your account.',
                    'Module is not licensed.':
                        'The requested module is not available under your company license.',
                    'Task visibility is not enabled.':
                        'Task visibility is not enabled for your account.',
                    'Existing task visibility allows only your own tasks.':
                        'Your task permissions allow only your own tasks. Try asking for your tasks instead.',
                }
                if isinstance(message, str) and message in safe_messages:
                    raise HTTPException(status, safe_messages[message])
            if status == 422 and endpoint == 'export':
                try:
                    message = response.json().get('message')
                except (ValueError, AttributeError):
                    message = None
                if message in (
                    'No active members are assigned to your led teams in this company. Team reports exclude managers, admins and other leads.',
                    'No active direct reports are assigned to you in this company. Team attendance includes only employees whose primary manager is you.',
                    'No active employees were found in your authorized attendance scope.',
                ):
                    raise HTTPException(status, message)
            raise HTTPException(status, 'Effortrak backend rejected the request. Check login, permissions, and configuration.')
        if response.status_code != 200 or len(response.content) > 12 * 1024 * 1024:
            raise HTTPException(502, 'Invalid Effortrak backend response.')
        try:
            result = response.json()
            if result.get('success') is not True or not isinstance(result.get('data'), dict):
                raise ValueError('Invalid envelope')
            return result['data']
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(502, 'Invalid Effortrak backend response.') from None

    async def identity(self, token: str, company_id: int, user_id: int) -> Identity:
        data = await self.call('context', token)
        try:
            context = BackendContext.model_validate(data)
            if (context.company_id != company_id or context.user_id != user_id
                    or context.data_source != 'effortrak_backend' or not context.scopes
                    or not set(context.scopes).issubset({'self', 'team', 'company'})
                    or not context.modules or not set(context.modules).issubset({'attendance', 'leave', 'tasks'})):
                raise ValueError('Identity mismatch')
        except (ValidationError, ValueError):
            raise HTTPException(502, 'Backend identity or permission context is invalid.') from None
        return Identity(company_id, user_id, context.name, context.designation, tuple(context.scopes),
                        context.can_export, 'effortrak_backend', context.policy_version, token, tuple(context.modules))

    async def revalidate(self, identity: Identity):
        current = await self.identity(identity.credential, identity.company_id, identity.user_id)
        if current.policy_key != identity.policy_key:
            raise HTTPException(409, 'Permissions changed. Start a new conversation.')

    async def work_report(self, identity: Identity, args):
        await self.revalidate(identity)
        if args.scope not in identity.scopes or 'attendance' not in identity.modules:
            raise HTTPException(403, 'Work report is outside your authorized scope.')
        data = await self.call('work-report', identity.credential, args.model_dump(mode='json'))
        try:
            parsed = WorkData.model_validate_json(json.dumps(data), strict=True)
            ids = parsed.employee_ids
            if (parsed.data_source != 'effortrak_backend' or parsed.scope != args.scope
                    or len(set(ids)) != len(ids) or any(type(value) is not int or value <= 0 for value in ids)
                    or [row.employee_id for row in parsed.rows] != ids
                    or args.scope == 'self' and any(value != identity.user_id for value in ids)
                    or args.employee_id is not None and any(value != args.employee_id for value in ids)
                    or parsed.detailed_review != args.detailed_review
                    or parsed.period.start != args.start_date or parsed.period.end > args.end_date
                    or parsed.period.end > parsed.period.as_of or parsed.period.end < parsed.period.start
                    or (parsed.review_period.end - parsed.review_period.start).days != 29
                    or parsed.review_period.end != min(parsed.period.end, parsed.period.as_of - timedelta(days=1))):
                raise ValueError('Invalid work report identity or period')
            for row in parsed.rows:
                if row.summary.days != (parsed.period.end - parsed.period.start).days + 1:
                    raise ValueError('Invalid work summary day count')
                if (row.review is not None) != args.detailed_review or any(len(note) > 350 for note in row.attention):
                    raise ValueError('Unrequested review')
                totals = [row.summary] + ([row.review.summary] if row.review is not None else [])
                if not row.tracking_available and any(any(getattr(summary, field) is not None for field in ('productive_seconds', 'idle_seconds', 'tracked_seconds', 'average_activity', 'screenshot_count')) for summary in totals):
                    raise ValueError('Unauthorized tracking data')
                if row.review is not None:
                    days = [day.date for day in row.review.days]
                    if len(days) != 30 or row.review.summary.days != len(days) or len(set(days)) != len(days) or any(not parsed.review_period.start <= day <= parsed.review_period.end for day in days):
                        raise ValueError('Invalid review days')
                    if not row.tracking_available and any(any(getattr(day, field) is not None for field in ('productive_seconds', 'idle_seconds', 'tracked_seconds', 'activity', 'screenshot_count')) for day in row.review.days):
                        raise ValueError('Unauthorized review tracking')
        except (ValidationError, ValueError, TypeError):
            raise HTTPException(502, 'Invalid backend work report.') from None
        return format_work_report(parsed)

    async def find_employee(self, identity: Identity, scope: str, name: str, module: str = 'attendance') -> dict:
        await self.revalidate(identity)
        if scope not in identity.scopes or module not in identity.modules or module not in ('attendance', 'tasks'):
            raise HTTPException(403, 'Employee lookup requires an authorized module and employee scope.')
        tokens = name_tokens(name)
        term = name
        for attempt in range(2):
            data = await self.call('employee-search', identity.credential,
                                   {'scope': scope, 'name': term, 'module': module})
            matches = data.get('matches')
            if (data.get('data_source') != 'effortrak_backend' or data.get('scope') != scope
                    or not isinstance(matches, list) or len(matches) > 10 or type(data.get('has_more')) is not bool
                    or any(not isinstance(row, dict) or type(row.get('employee_id')) is not int or row['employee_id'] <= 0
                           or not isinstance(row.get('name'), str)
                           or (scope == 'self' and row['employee_id'] != identity.user_id) for row in matches)):
                raise HTTPException(502, 'Invalid backend employee-search response.')
            if matches or data['has_more'] or len(tokens) < 2 or attempt:
                break
            # Prefix queries tolerate shortened names, but never choose a nearest/fuzzy ID.
            term = max(tokens, key=len)
        matches = [row for row in matches
                   if all(any(word.startswith(token) for word in name_tokens(row['name'])) for token in tokens)]
        return {'matches': matches, 'has_more': data['has_more']}

    async def attendance(self, identity: Identity, args) -> dict:
        await self.revalidate(identity)
        data = await self.call('attendance', identity.credential, args.model_dump())
        self.validate_attendance(data, identity, args)
        return data

    @staticmethod
    def validate_attendance(data: dict, identity: Identity, args, date: str | None = None):
        ids, rows = data.get('employee_ids'), data.get('rows')
        month = date[:7] if date else args.month
        if (data.get('data_source') != 'effortrak_backend' or data.get('scope') != args.scope
                or data.get('month') != month or (date is not None and data.get('date') != date)
                or not isinstance(ids, list) or len(ids) > 50
                or any(type(value) is not int or value <= 0 for value in ids)
                or (args.scope == 'self' and any(value != identity.user_id for value in ids))
                or (args.employee_id is not None and ids != [args.employee_id])
                or not isinstance(rows, list)
                or any(not isinstance(row, dict) or type(row.get('employee_id')) is not int
                       or row['employee_id'] not in ids or not isinstance(row.get('employee_name'), str)
                       or not isinstance(row.get('status'), str)
                       or type(row.get('worked_minutes')) is not int or row['worked_minutes'] < 0
                       or not isinstance(row.get('date'), str) or row['date'][:7] != month
                       or (date is not None and row['date'] != date) for row in rows)):
            raise HTTPException(502, 'Invalid backend attendance response.')

    async def employee_count(self, identity: Identity, args) -> dict:
        await self.revalidate(identity)
        data = await self.call('employee-count', identity.credential, args.model_dump())
        if (data.get('data_source') != 'effortrak_backend' or data.get('scope') != args.scope
                or type(data.get('employee_count')) is not int or data['employee_count'] < 0):
            raise HTTPException(502, 'Invalid backend employee count response.')
        return data

    async def daily_attendance(self, identity: Identity, args) -> dict:
        await self.revalidate(identity)
        data = await self.call('attendance', identity.credential, {
            'scope': args.scope, 'employee_id': args.employee_id,
            'date': args.date.isoformat(), 'month': args.date.strftime('%Y-%m'),
        })
        self.validate_attendance(data, identity, args, args.date.isoformat())
        return data

    async def export(self, identity: Identity, args) -> tuple[dict, dict]:
        await self.revalidate(identity)
        if args.scope not in identity.scopes or 'attendance' not in identity.modules:
            raise HTTPException(403, 'Attendance export is not permitted for this scope.')
        if not identity.can_export:
            raise HTTPException(403, 'You do not have report export permission.')
        data = await self.call('export', identity.credential, args.model_dump(mode='json', exclude_none=True))
        try:
            payload = base64.b64decode(data.pop('payload_base64'), validate=True)
            employees = data['employee_ids']
            if (data.get('data_source') != 'effortrak_backend' or data.get('format') != args.format
                    or not isinstance(employees, list) or len(employees) > 50
                    or any(type(value) is not int or value <= 0 for value in employees)
                    or (args.scope == 'self' and any(value != identity.user_id for value in employees))
                    or (args.employee_id is not None and employees != [args.employee_id])
                    or len(payload) > 8 * 1024 * 1024 or not payload
                    or not payload.startswith(b'%PDF-' if args.format == 'pdf' else b'PK')):
                raise ValueError('Invalid report')
        except (KeyError, ValueError, TypeError):
            raise HTTPException(502, 'Invalid backend report response.') from None
        report_id = str(uuid4())
        filename = data.get('filename', f'attendance-{args.month}.{args.format}')
        if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.' + args.format, filename):
            raise HTTPException(502, 'Invalid backend report filename.')
        artifact = {'report_id': report_id, 'filename': filename,
                    'format': args.format, 'download_url': f'/v1/reports/{report_id}/download'}
        return ({**artifact, 'data_source': 'effortrak_backend', 'notice': data.get('notice', '')},
                {**artifact, 'payload': payload, 'employee_ids': employees})

    async def authorize_report(self, identity: Identity, employee_ids: list[int]):
        result = await self.call('authorize-report', identity.credential, {'employee_ids': employee_ids})
        if result.get('authorized') is not True:
            raise HTTPException(403, 'Report download is not permitted.')

    async def tasks(self, identity: Identity, args, *, daily: bool) -> TaskResult:
        await self.revalidate(identity)
        if 'tasks' not in identity.modules:
            raise HTTPException(403, 'Task queries require the task-module license and read permission.')
        project = hasattr(args, 'project_id')
        history = hasattr(args, 'start_date')
        start = args.start_date.isoformat() if history and args.start_date else None
        end = args.end_date.isoformat() if history and args.end_date else None
        data = await self.call('project-tasks' if project else 'daily-tasks' if daily else 'task-summary', identity.credential, args.model_dump(mode='json'))
        if project and (data.get('project_id') != args.project_id or not isinstance(data.get('project_name'), str)):
            raise HTTPException(502, 'Invalid backend task history response.')
        if history and (data.get('start_date') != start or data.get('end_date') != end
                        or not isinstance(data.get('rows'), list)
                        or any(not isinstance(row, dict) or not isinstance(row.get('task_date'), str)
                               or (start is not None and not start <= row['task_date'] <= end) for row in data.get('rows', []))):
            raise HTTPException(502, 'Invalid backend project task response.')
        ids, rows = data.get('employee_ids'), data.get('rows')
        if (data.get('data_source') != 'effortrak_backend' or data.get('scope') != args.scope
                or not isinstance(ids, list) or len(ids) > 50
                or any(type(value) is not int or value <= 0 for value in ids)
                or not isinstance(rows, list)
                or (args.scope == 'self' and any(value != identity.user_id for value in ids))
                or (args.employee_id is not None and ids != [args.employee_id])
                or any(not isinstance(row, dict) or type(row.get('employee_id')) is not int
                       or row['employee_id'] not in ids for row in rows)):
            raise HTTPException(502, 'Invalid backend task response.')
        if daily:
            expected_date = (f'{start} to {end}' if start else 'all history (newest first)') if history else args.date.isoformat()
            page_size = (args.latest_limit or 50) if history else 50
            if (data.get('date') != expected_date or data.get('page') != args.page
                    or data.get('page_size') != page_size or len(rows) > page_size
                    or type(data.get('task_count')) is not int or data['task_count'] < 0
                    or type(data.get('matching_employee_count')) is not int
                    or not len({row['employee_id'] for row in rows}) <= data['matching_employee_count'] <= min(len(ids), data['task_count'])
                    or (data['matching_employee_count'] == 0) != (data['task_count'] == 0)
                    or type(data.get('planned_minutes')) is not int or data['planned_minutes'] < 0
                    or type(data.get('has_more')) is not bool
                    or (data.get('booked_minutes') is not None and
                        (type(data['booked_minutes']) is not int or data['booked_minutes'] < 0))
                    or any(type(row.get('task_id')) is not int or row['task_id'] <= 0
                           or not isinstance(row.get('description'), str)
                           or not isinstance(row.get('status_name'), str)
                           or not isinstance(row.get('employee_name'), str)
                           or type(row.get('planned_minutes')) is not int or row['planned_minutes'] < 0
                           or (row.get('booked_minutes') is not None and
                               (type(row['booked_minutes']) is not int or row['booked_minutes'] < 0))
                           or type(row.get('work_status_id')) is not int for row in rows)):
                raise HTTPException(502, 'Invalid backend daily task response.')
        elif (data.get('month') != args.month
              or any(type(row.get('task_count')) not in (int, str) or not str(row['task_count']).isdigit()
                     or type(row.get('planned_minutes')) not in (int, str) or not str(row['planned_minutes']).isdigit()
                     or type(row.get('work_status_id')) not in (int, str) or not str(row['work_status_id']).isdigit()
                     for row in rows)):
            raise HTTPException(502, 'Invalid backend task summary response.')
        # Raw descriptions never leave this method except in the local-only reply object.
        return task_result(data, daily=daily)

    async def manager_report(self, identity: Identity, args):
        await self.revalidate(identity)
        module = 'tasks' if args.report in ('effort_by_employee', 'effort_by_project', 'task_overruns', 'workload', 'booked_time') else 'leave' if args.report == 'approved_leave' else 'attendance'
        if module not in identity.modules or args.scope not in identity.scopes:
            raise HTTPException(403, 'This report is outside your permitted module or employee scope.')
        data = await self.call('manager-report', identity.credential, args.model_dump(mode='json'))
        if args.project_id is not None and (data.get('project_id') != args.project_id or not isinstance(data.get('project_name'), str)):
            raise HTTPException(502, 'Invalid backend project effort subject.')
        ids, rows = data.get('employee_ids'), data.get('rows')
        start = args.start_date.isoformat() if args.start_date else None
        end = args.end_date.isoformat() if args.end_date else None
        if (data.get('data_source') != 'effortrak_backend' or data.get('scope') != args.scope
                or data.get('report') != args.report or data.get('start_date') != start or data.get('end_date') != end
                or not isinstance(ids, list) or len(ids) > 50 or any(type(value) is not int or value <= 0 for value in ids)
                or (args.scope == 'self' and ids != [identity.user_id])
                or (args.employee_id is not None and ids != [args.employee_id])
                or not isinstance(rows, list) or len(rows) > 50
                or type(data.get('matched_count')) is not int or data['matched_count'] < len(rows)
                or type(data.get('details_truncated')) is not bool or not isinstance(data.get('notice'), str)):
            raise HTTPException(502, 'Invalid backend manager report response.')
        if args.report in ('present', 'absent') and start == end:
            coverage = data.get('attendance_coverage')
            if (not isinstance(coverage, dict) or coverage.get('employee_count') != len(ids)
                    or any(type(coverage.get(key)) is not int or not 0 <= coverage[key] <= len(ids)
                           for key in ('employee_count', 'unclassified_employee_count', 'punch_in_employee_count'))):
                raise HTTPException(502, 'Invalid backend daily attendance coverage.')
            if data.get('absence_basis') == 'not_checked_in':
                if (args.report != 'absent' or any(type(coverage.get(key)) is not int or not 0 <= coverage[key] <= len(ids)
                        for key in ('excluded_employee_count', 'expected_employee_count'))
                        or coverage['excluded_employee_count'] + coverage['expected_employee_count'] != len(ids)):
                    raise HTTPException(502, 'Invalid backend operational absence counts.')
        for row in rows:
            if not isinstance(row, dict):
                raise HTTPException(502, 'Invalid backend manager report row.')
            project = args.report == 'effort_by_project'
            if (project and (type(row.get('project_id')) is not int or row['project_id'] <= 0 or not isinstance(row.get('project_name'), str))
                    or not project and (type(row.get('employee_id')) is not int or row['employee_id'] not in ids
                                        or not isinstance(row.get('employee_name'), str))):
                raise HTTPException(502, 'Invalid backend manager report subject.')
            if args.report == 'late_punchin':
                entries = row.get('entries')
                if (type(row.get('assessable')) is not bool or not isinstance(entries, list)
                        or type(row.get('count')) is not int or row['count'] != len(entries)
                        or row['assessable'] and not isinstance(row.get('late_after'), str)
                        or not row['assessable'] and (row.get('late_after') is not None or entries)):
                    raise HTTPException(502, 'Invalid backend late punch-in report.')
                days = []
                for entry in entries:
                    if (not isinstance(entry, dict) or not isinstance(entry.get('date'), str)
                            or not start <= entry['date'] <= end or not isinstance(entry.get('punch_in'), str)
                            or type(entry.get('late_minutes')) is not int or entry['late_minutes'] <= 0):
                        raise HTTPException(502, 'Invalid backend late punch-in entry.')
                    days.append(entry['date'])
                if len(set(days)) != len(days):
                    raise HTTPException(502, 'Duplicate backend late punch-in dates.')
            elif args.report in ('late', 'missing_punchout', 'present', 'absent'):
                dates = row.get('dates')
                if (not isinstance(dates, list) or type(row.get('count')) is not int or row['count'] != len(dates)
                        or (row['count'] <= args.threshold and not (row['count'] == 0 and args.threshold == 0 and (args.employee_id is not None or args.scope == 'self')))
                        or any(not isinstance(day, str) for day in dates) or len(set(dates)) != len(dates)
                        or any(not isinstance(day, str) or not start <= day <= end for day in dates)):
                    raise HTTPException(502, 'Invalid backend attendance exception counts.')
            elif args.report == 'booked_time':
                if (type(row.get('entry_count')) is not int or row['entry_count'] <= 0
                        or type(row.get('unavailable_count')) is not int or not 0 <= row['unavailable_count'] <= row['entry_count']
                        or (row.get('booked_minutes') is None) != (row['unavailable_count'] == row['entry_count'])
                        or row.get('booked_minutes') is not None and (type(row['booked_minutes']) is not int or row['booked_minutes'] < 0)):
                    raise HTTPException(502, 'Invalid backend timesheet total.')
                billing = row.get('billing')
                if not isinstance(billing, dict) or set(billing) != {'billable', 'non_billable', 'unclassified'}:
                    raise HTTPException(502, 'Invalid backend billing breakdown.')
                for bucket in billing.values():
                    if (not isinstance(bucket, dict) or type(bucket.get('entry_count')) is not int or bucket['entry_count'] < 0
                            or type(bucket.get('unavailable_count')) is not int or not 0 <= bucket['unavailable_count'] <= bucket['entry_count']
                            or (bucket.get('minutes') is None) != (bucket['entry_count'] > 0 and bucket['entry_count'] == bucket['unavailable_count'])
                            or bucket.get('minutes') is not None and (type(bucket['minutes']) is not int or bucket['minutes'] < 0)
                            or bucket['entry_count'] == 0 and bucket.get('minutes') != 0):
                        raise HTTPException(502, 'Invalid backend billing category.')
                if (sum(bucket['entry_count'] for bucket in billing.values()) != row['entry_count']
                        or sum(bucket['unavailable_count'] for bucket in billing.values()) != row['unavailable_count']
                        or sum(bucket['minutes'] or 0 for bucket in billing.values()) != (row['booked_minutes'] or 0)):
                    raise HTTPException(502, 'Backend billing breakdown does not match the booking total.')
            elif args.report == 'approved_leave':
                if (type(row.get('request_id')) is not int or row['request_id'] <= 0
                        or row.get('status') not in ('approved', 'withdraw_requested')
                        or not isinstance(row.get('start_date'), str) or not isinstance(row.get('end_date'), str)
                        or row['start_date'] > end or row['end_date'] < start or row['end_date'] < row['start_date']):
                    raise HTTPException(502, 'Invalid backend approved leave response.')
            elif args.report == 'attendance_totals':
                counts = row.get('status_counts')
                if (not isinstance(counts, dict) or any(not isinstance(label, str) or type(count) is not int or count < 0 for label, count in counts.items())
                        or type(row.get('worked_minutes')) is not int or row['worked_minutes'] < 0):
                    raise HTTPException(502, 'Invalid backend attendance totals.')
            elif args.report == 'work_arrangements':
                if row.get('arrangement') not in ('WFH and WFO allowed', 'WFH allowed', 'WFO allowed', 'Not configured'):
                    raise HTTPException(502, 'Invalid backend work arrangement.')
            elif args.report in ('locations', 'missing_location'):
                if not isinstance(row.get('date'), str) or not start <= row['date'] <= end:
                    raise HTTPException(502, 'Invalid backend location period.')
                for key in ('punch_in', 'punch_out'):
                    point = row.get(key)
                    if (point is not None and (not isinstance(point, dict)
                            or type(point.get('latitude')) not in (int, float) or type(point.get('longitude')) not in (int, float)
                            or not -90 <= point['latitude'] <= 90 or not -180 <= point['longitude'] <= 180)
                            or not isinstance(row.get(key + '_address'), str)):
                        raise HTTPException(502, 'Invalid backend location value.')
            else:
                if (type(row.get('planned_minutes')) is not int or row['planned_minutes'] < 0
                        or row.get('booked_minutes') is not None and (type(row['booked_minutes']) is not int or row['booked_minutes'] < 0)):
                    raise HTTPException(502, 'Invalid backend effort totals.')
                if args.report == 'task_overruns' and (type(row.get('task_id')) is not int
                        or row['task_id'] <= 0 or not isinstance(row.get('date'), str) or not start <= row['date'] <= end
                        or row.get('booked_minutes') is None or row['booked_minutes'] <= row['planned_minutes']):
                    raise HTTPException(502, 'Invalid backend task overrun.')
                if args.report != 'task_overruns' and (type(row.get('task_count')) is not int or row['task_count'] < 0):
                    raise HTTPException(502, 'Invalid backend task count.')
                if args.report == 'workload' and args.threshold and row['planned_minutes'] <= args.threshold:
                    raise HTTPException(502, 'Invalid backend workload threshold.')
        return data

    async def find_project(self, identity: Identity, scope: str, name: str, *, booked_effort: bool = False):
        await self.revalidate(identity)
        if 'tasks' not in identity.modules or scope not in identity.scopes:
            raise HTTPException(403, 'Project tasks are not permitted for this scope.')
        body = {'scope': scope, 'name': name}
        if booked_effort:
            body['report'] = 'booked_time'
        data = await self.call('project-search', identity.credential, body)
        matches = data.get('matches')
        if (data.get('data_source') != 'effortrak_backend' or data.get('scope') != scope
                or type(data.get('has_more')) is not bool or not isinstance(matches, list) or len(matches) > 10
                or any(not isinstance(row, dict) or type(row.get('project_id')) is not int or row['project_id'] <= 0
                       or not isinstance(row.get('name'), str) for row in matches)):
            raise HTTPException(502, 'Invalid backend project lookup response.')
        return data
