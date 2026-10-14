import re
from datetime import datetime, timedelta, timezone


INTRO = (
    r'^\s*(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?'
    r'(?:(?:i\s+(?:want|need)|i\s+would\s+like)\s+)?'
    r'(?:(?:check|show|find|get|give|tell|look\s+up)(?:\s+me)?\s+)?'
    r'(?:(?:what\s+(?:was|is)|how\s+was)\s+)?'
)
NAMED_ATTENDANCE = re.compile(
    INTRO + r'(?P<name>[^\r\n?]+?)\s+attendance(?:\s+history)?\b', re.IGNORECASE,
)
ATTENDANCE_OF = re.compile(r'\battendance(?:\s+(?:history|report))?\s+(?:of|for)\s+(?P<name>[^\r\n?]+?)'
                           r'\s+(?:on|for|from)\s+(?:\d|today\b|yesterday\b)', re.IGNORECASE)
NAMED_TASKS = re.compile(INTRO + r'(?P<name>[^\r\n?]+?)\s+tasks?(?:\s+(?:details|summary|history))?\b', re.IGNORECASE)
TASKS_OF = re.compile(r'\btasks?(?:\s+(?:details|summary|history))?\s+(?:of|for)\s+(?P<name>[^\r\n?]+?)'
                      r'\s+(?:on|for)\s+(?:\d|today\b|yesterday\b)', re.IGNORECASE)


def name_tokens(value: str) -> list[str]:
    return re.findall(r'[^\W\d_]+', value.casefold())


def named_attendance(message: str) -> tuple[str, str | None, str | None] | None:
    return named_request(message, NAMED_ATTENDANCE, ATTENDANCE_OF)


def named_tasks(message: str) -> tuple[str, str | None, str | None] | None:
    return named_request(message, NAMED_TASKS, TASKS_OF)


def named_request(message: str, before, after) -> tuple[str, str | None, str | None] | None:
    if re.search(r'\b(?:export|pdf|xlsx|excel)\b', message, re.IGNORECASE):
        return None
    match = after.search(message) or before.search(message)
    if not match:
        return None
    name = match['name'].strip().removesuffix("'s").removesuffix('\u2019s')
    tokens = name_tokens(name)
    if (not tokens or len(name) > 120 or any(len(token) < 2 for token in tokens)
            or any(token in {'my', 'own', 'personal', 'company', 'team', 'self', 'employee', 'employees', 'total', 'all', 'our', 'their', 'his', 'her', 'the', 'this', 'that', 'same', 'show', 'check', 'give', 'tell', 'find', 'get', 'can', 'could', 'would', 'please', 'you', 'me', 'for', 'task', 'tasks', 'details', 'summary'} for token in tokens)
            or any(char.isdigit() for char in name)):
        return None
    day = re.search(r'\b20\d{2}-\d{2}-\d{2}\b', message)
    local_day = re.search(r'\b(\d{2})/(\d{2})/(20\d{2})\b', message)
    month = re.search(r'\b20\d{2}-(?:0[1-9]|1[0-2])\b', message)
    date_value = day[0] if day else (f'{local_day[3]}-{local_day[2]}-{local_day[1]}' if local_day else None)
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    if re.search(r'\byesterday\b', message, re.IGNORECASE):
        date_value = (today - timedelta(days=1)).isoformat()
    elif re.search(r'\btoday\b', message, re.IGNORECASE):
        date_value = today.isoformat()
    return name, date_value, month[0] if month and not date_value else None


def model_query_context(context: dict) -> dict:
    if context.get('source') in ('local_employee_search', 'local_project_search', 'local_manager_report'):
        return {key: value for key, value in context.items() if key not in ('employee_ids', 'employee_id', 'employee_name', 'source', 'project_id', 'project_name')}
    return context
