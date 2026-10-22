import json
from dataclasses import dataclass

from app.attendance_report import duration


class AssistantReply(str):
    def __new__(cls, content: str, *, model_content: str | None = None, query_context: dict | None = None, charts=None):
        reply = super().__new__(cls, content)
        reply.model_content = content if model_content is None else model_content
        reply.query_context = query_context
        reply.charts = charts or []
        return reply


@dataclass(frozen=True)
class TaskResult:
    # Only model_payload may be serialized into an OpenAI function-call output.
    model_payload: dict
    local_reply: AssistantReply
    chart_source: object = None


def trusted_query(history: list[dict]) -> dict:
    for row in reversed(history):
        if row['role'] != 'assistant' or not row.get('query_context'):
            continue
        try:
            context = json.loads(row['query_context'])
        except (TypeError, ValueError):
            return {}
        return context if isinstance(context, dict) else {}
    return {}


def task_result(data: dict, *, daily: bool) -> TaskResult:
    context = {'module': 'tasks', 'scope': data['scope'], 'employee_ids': data['employee_ids'],
               'date' if daily else 'month': data['date' if daily else 'month']}
    if daily:
        context['page'] = data['page']
        safe = {key: data[key] for key in ('scope', 'date', 'employee_ids', 'task_count', 'planned_minutes',
                                          'page', 'page_size', 'has_more')}
        safe['rows'] = [{key: row[key] for key in ('employee_id', 'work_status_id', 'planned_minutes')}
                        for row in data['rows']]
        safe['booked_minutes'] = data.get('booked_minutes')
        for safe_row, row in zip(safe['rows'], data['rows']):
            safe_row['booked_minutes'] = row.get('booked_minutes')
        lines = [f"Tasks for {data['date']} - {data['scope']} scope",
                 f"Employees in displayed tasks: {len({row['employee_id'] for row in data['rows']})}",
                 f"Tasks: {data['task_count']} | Total planned time: {duration(data['planned_minutes'])}",
                 f"Page {data['page']} (up to {data['page_size']} tasks)", '']
        if data.get('booked_minutes') is not None:
            lines.insert(3, f"Total booked time (available linked entries): {duration(data['booked_minutes'])}")
        if 'matching_employee_count' in data:
            lines.insert(2, f"Employees across all matching tasks: {data['matching_employee_count']}")
        if 'start_date' in data and (data['page_size'] != 50 or data['date'] == 'all history (newest first)'):
            lines.insert(2, f"Showing {len(data['rows'])} tasks, newest first. Totals below cover all matching tasks, not just this batch.")
        for row in data['rows']:
            booked = (f" | Booked: {duration(row['booked_minutes'])}" if row.get('booked_minutes') is not None
                      else ' | Booked: Not available')
            lines.extend([f"Task #{row['task_id']} - {row['employee_name']} (employee #{row['employee_id']})",
                          f"Date: {row['task_date']}" if 'start_date' in data else '',
                          f"Status: {row['status_name']} | Planned: {duration(row['planned_minutes'])}{booked}",
                          row['description'] or '(No description)',
                          '(Description shortened)' if row.get('description_truncated') else '', ''])
        if data['has_more']:
            lines.append(f"More tasks are available. Ask for page {data['page'] + 1} for the same date and scope.")
    else:
        rows = data['rows']
        safe = {key: data[key] for key in ('scope', 'month', 'employee_ids')}
        safe['rows'] = [{key: row[key] for key in ('employee_id', 'work_status_id', 'task_count', 'planned_minutes')}
                        for row in rows]
        lines = [f"Task summary for {data['month']} - {data['scope']} scope",
                 f"Employees with matching tasks: {len({row['employee_id'] for row in rows})}",
                 f"Tasks: {sum(int(row['task_count']) for row in rows)} | "
                 f"Total planned time: {duration(sum(int(row['planned_minutes']) for row in rows))}", '']
        for row in rows:
            lines.append(f"Employee #{row['employee_id']} | Status ID {row['work_status_id']} | "
                         f"Tasks: {row['task_count']} | Planned: {duration(int(row['planned_minutes']))}")
    if not data['rows']:
        if daily and data['task_count']:
            lines.append('No tasks are on this page. Ask for page 1 to return to the first page.')
        else:
            lines.append('No tasks were found for this period within your current employee and department permissions.')
    if data.get('project_id'):
        lines.insert(0, f"Project: {data['project_name']}")
    safe['data_source'] = 'effortrak_backend'
    safe['notice'] = 'Planned time is not actual worked time. Descriptions and names are displayed locally only.'
    if daily:
        safe['notice'] += ' Booked time covers available linked timesheet entries only, not attendance time.'
    lines.extend(['', safe['notice']])
    return TaskResult(safe, AssistantReply('\n'.join(lines).strip(), model_content=json.dumps(safe), query_context=context))
