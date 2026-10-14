import re


MONTHLY_OVERVIEW = re.compile(
    r'^\s*(?:please\s+)?(?:show|give|display)?\s*(?:me\s+)?'
    r'(?:(?:my|company|team)\s+)?attendance(?:\s+(?:report|summary))?'
    r'\s+(?:for\s+)?(?:20\d{2}-(?:0[1-9]|1[0-2])|this\s+month|last\s+month)[.!?]*\s*$',
    re.IGNORECASE,
)


def duration(minutes: int) -> str:
    return f'{minutes // 60}h {minutes % 60}m'


def format_daily_report(data: dict) -> str:
    lines = [f"Attendance for {data['date']} - {data['scope']} scope",
             f"Employees included: {len(data['employee_ids'])}"]
    for row in data['rows']:
        minutes = row['worked_minutes']
        lines.extend(['', f"{row['employee_name']} (employee #{row['employee_id']})",
                      f"Status: {row['status'] or 'Unclassified (not an absence)'}",
                      f"Recorded worked time: {duration(minutes)} ({minutes} minutes)",
                      f"Punch in: {row.get('punch_in') or 'Not recorded'}",
                      f"Punch out: {row.get('punch_out') or 'Not recorded'}"])
    if not data['rows']:
        lines.append('No attendance rows were returned in the authorized scope. This is not an absence count.')
    lines.extend(['', data.get('notice', '')])
    return '\n'.join(lines).rstrip()


def format_monthly_report(data: dict) -> str:
    period = data['period']
    lines = [f"{data['scope'].capitalize()} attendance - {data['month']}",
             f"Period: {period['start']} to {period['end']}",
             f"Snapshot date: {period['as_of']}",
             f"Employees included: {data['employee_count']}", '',
             'Calendar status totals (employee-days, not unique employees)']
    totals = data['summary']
    lines.extend(f'{status}: {count}' for status, count in sorted(totals.items()))
    if not totals:
        lines.append('No classified calendar statuses.')
    lines.extend([f"Unclassified employee-days: {data['unclassified_employee_days']}",
                  f"Total worked time: {duration(data['worked_minutes'])}", '', 'Employee breakdown'])
    # Render the authorized aggregates locally; do not introduce name lookup or new external data.
    for employee in data['employee_summaries']:
        statuses = '; '.join(f'{status}: {count}' for status, count in sorted(employee['status_counts'].items()))
        lines.append(f"Employee #{employee['employee_id']}: {statuses or 'No classified statuses'}; "
                     f"unclassified: {employee['unclassified_days']}; worked: {duration(employee['worked_minutes'])}")
    lines.extend(['', 'Each employee-date has one calendar status. Unclassified days are not absences. '
                  'Worked time is recorded attendance time, not payroll payable time.', data.get('notice', '')])
    return '\n'.join(lines).rstrip()
