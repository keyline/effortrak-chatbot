from collections import Counter
from io import BytesIO
from uuid import uuid4

from fastapi import HTTPException
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet

from app.auth import Identity
from app.schemas import AttendanceArgs, ExportArgs, EmployeeCountArgs, DailyAttendanceArgs, DailyTaskArgs, TaskSummaryArgs, WorkReportArgs


EMPLOYEES = [
    {'id': 100, 'company_id': 1, 'name': 'Mira Demo', 'manager_id': 103},
    {'id': 101, 'company_id': 1, 'name': 'Asha Demo', 'manager_id': 100},
    {'id': 102, 'company_id': 1, 'name': 'Dev Demo', 'manager_id': 100},
    {'id': 103, 'company_id': 1, 'name': 'Arun Demo', 'manager_id': None},
    {'id': 201, 'company_id': 2, 'name': 'Ben Demo', 'manager_id': None},
]
SAMPLE_STATUSES = ('Present', 'Late', 'Absent')


def allowed_employee_ids(identity: Identity) -> set[int]:
    return {
        employee['id'] for employee in EMPLOYEES
        if employee['company_id'] == identity.company_id
        and (employee['id'] == identity.user_id or 'company' in identity.scopes
             or ('team' in identity.scopes and employee['manager_id'] == identity.user_id))
    }


def attendance(identity: Identity, args: AttendanceArgs) -> dict:
    if args.scope not in identity.scopes:
        raise HTTPException(403, 'You do not have permission for this attendance scope.')
    employees = [employee for employee in EMPLOYEES if employee['company_id'] == identity.company_id]
    if args.scope == 'self':
        employees = [employee for employee in employees if employee['id'] == identity.user_id]
    elif args.scope == 'team':
        employees = [employee for employee in employees if employee['manager_id'] == identity.user_id]
    if args.employee_id is not None:
        employees = [employee for employee in employees if employee['id'] == args.employee_id]
        if not employees:
            raise HTTPException(403, 'Requested employee is outside your permitted attendance scope.')
    rows = []
    # Only three fictional days exist. Never imply these are full-month totals.
    if args.month == '2026-09':
        for employee in employees:
            for day, status in enumerate(SAMPLE_STATUSES, 1):
                rows.append({'employee_id': employee['id'], 'employee_name': employee['name'],
                             'date': f'2026-09-{day:02}', 'status': status,
                             'worked_minutes': 0 if status == 'Absent' else 480})
    return {'data_source': 'fictional_sample', 'month': args.month, 'scope': args.scope,
            'sample_notice': 'Fictional records for September 1-3, 2026 only; not a full-month report.',
            'employee_ids': [employee['id'] for employee in employees],
            'summary': dict(Counter(row['status'] for row in rows)), 'rows': rows}


def export_attendance(identity: Identity, args: ExportArgs) -> tuple[dict, dict]:
    if args.report_type is not None:
        raise HTTPException(422, 'These report types require real backend data. Sample exports use a YYYY-MM month.')
    if not identity.can_export:
        raise HTTPException(403, 'You do not have permission to export reports.')
    data = attendance(identity, args)
    buffer = BytesIO()
    headers = ['Employee ID', 'Employee Name', 'Date', 'Status', 'Worked Minutes']
    values = [[row[key] for key in ('employee_id', 'employee_name', 'date', 'status', 'worked_minutes')]
              for row in data['rows']]
    if args.format == 'xlsx':
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Sample Attendance'
        sheet.append(['FICTIONAL SAMPLE - NOT REAL COMPANY DATA'])
        sheet.append([data['sample_notice']])
        sheet.append(headers)
        for row in values:
            sheet.append(row)
        sheet.freeze_panes = 'A4'
        for column, width in {'A': 18, 'B': 24, 'C': 18, 'D': 18, 'E': 24}.items():
            sheet.column_dimensions[column].width = width
        workbook.save(buffer)
    else:
        styles = getSampleStyleSheet()
        table = Table([headers, *values], repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('PADDING', (0, 0), (-1, -1), 6),
        ]))
        SimpleDocTemplate(buffer, pagesize=landscape(A4)).build([
            Paragraph('Fictional Sample Attendance', styles['Title']),
            Paragraph(data['sample_notice'], styles['Normal']), Spacer(1, 16), table,
        ])
    report_id = str(uuid4())
    artifact = {'report_id': report_id, 'filename': f'sample-attendance-{args.month}.{args.format}',
                'format': args.format, 'download_url': f'/v1/reports/{report_id}/download'}
    report = {**artifact, 'payload': buffer.getvalue(), 'employee_ids': data['employee_ids']}
    return {**artifact, 'data_source': 'fictional_sample', 'sample_notice': data['sample_notice']}, report


def definitions(identity: Identity) -> list[dict]:
    tools = []
    for name, model, description in [
        ('get_employee_work_report', WorkReportArgs, 'Display an authorized work-hours summary combining punches, attendance, desktop productive/idle time, screenshot capture counts and recorded average activity. Module and assignment checks are enforced by the backend. Normally detailed_review=false: show only brief supported recurring concerns from the preceding 30 completed days. True only for an explicitly requested detailed review. At most 31 requested days; resolve names/IDs using authorized context. Records and screenshot images stay local.'),
        ('get_employee_count', EmployeeCountArgs, 'Count active employees in an authorized scope. Use for total employees, headcount, team size. No date or employee ID needed. Not a present-day attendance count.'),
        ('get_daily_attendance', DailyAttendanceArgs, 'Read attendance for one exact date: today, yesterday, present, absent, late, leave, hours or punch times. Resolve relative dates using the supplied local date. employee_id=null reads the entire scope.'),
        ('get_attendance', AttendanceArgs, 'Read monthly fictional sample attendance for an authorized scope. Real backend results include period, employee_count, employee_summaries, worked time and status totals measured in employee-days, not unique employees. Use full aggregates rather than truncated daily rows.'),
        ('export_attendance', ExportArgs, 'Generate an authorized fictional sample attendance PDF or XLSX. Real backend supports report_type today, last_7_days, monthly_details (month), custom_range (start_date/end_date inclusive, maximum 93 days). Relative reports use the server date. Never supply a month with a custom range or relative report. Report contents stay local.'),
        ('get_daily_tasks', DailyTaskArgs, 'Read authorized Task Assign details for one date and employee/scope. Resolve this employee and that day from trusted query context; ask for clarification if ambiguous. page=1 first, then explicitly requested next pages. Descriptions remain local; the server presents the details.'),
        ('get_task_summary', TaskSummaryArgs, 'Read monthly authorized Task Assign totals grouped by employee and stored status ID. Planned time is not actual worked time. No task edits or exports.'),
    ]:
        if name == 'get_employee_work_report' and identity.data_source != 'effortrak_backend':
            continue
        if name in ('get_attendance', 'get_daily_attendance', 'export_attendance', 'get_employee_work_report') and 'attendance' not in identity.modules:
            continue
        if name == 'export_attendance' and not identity.can_export:
            continue
        if name in ('get_daily_tasks', 'get_task_summary') and (identity.data_source != 'effortrak_backend' or 'tasks' not in identity.modules):
            continue
        schema = model.model_json_schema()
        schema['properties']['scope']['enum'] = list(identity.scopes)
        schema['required'] = list(schema['properties'])
        for prop in schema['properties'].values():
            prop.pop('default', None)
        tools.append({'type': 'function', 'name': name, 'description': description,
                      'parameters': schema, 'strict': True})
        if identity.data_source == 'effortrak_backend':
            tools[-1]['description'] = description.replace('fictional sample', 'real Effortrak')
    return tools
