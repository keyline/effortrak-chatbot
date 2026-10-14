import calendar
import re
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from app.schemas import ProjectTaskArgs
from app.task_report import AssistantReply


NUMBER_WORDS = {word: number for number, word in enumerate(
    ('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'), 0)}
NUMBER = r'(?:\d+|' + '|'.join(NUMBER_WORDS) + r')'
RECENCY = r'(?:most\s+recent|last|latest|recent|newest|next|more)'
TASK_SUBJECT = r'tasks?(?:\s+(?:history|details|records|list))?'
NAME_END = r'(?=\s+(?:tasks?|details|history|report|for|from|on|today|yesterday|last|latest|recent|newest|show|give|list|please|employee)\b|[?.]?$)'
QUOTES = '\"\x27\u201c\u201d\u2018\u2019'


def query_text(message):
    return message.strip().strip(QUOTES).strip()


def clean_name(value):
    value = value.strip(' \t\r\n?.,' + QUOTES)
    value = re.sub(r'^(?:the\s+)?project(?:\s+name)?\s*:?\s+(?:is\s+)?', '', value, flags=re.I)
    value = re.sub(r'^the\s+', '', value, flags=re.I)
    value = re.sub(r'\s+project$', '', value, flags=re.I).strip(' ' + QUOTES)
    if (value.casefold() in ('my', 'this', 'that', 'same', 'all', 'the', 'a', 'name', 'my team', 'our team', 'the team',
                            'show', 'give', 'tell', 'find', 'get', 'list', 'please')
            or re.match(r'^(?:20\d{2}-\d{2}(?:-\d{2})?|\d{2}/\d{2}/20\d{2})\b', value)
            or re.fullmatch(r'today|yesterday|tomorrow|(?:last|past)\s+(?:7|seven)\s+days', value, re.I)
            or re.fullmatch(r'tasks?(?:\s+(?:history|details|records|list))?|history|details|report|records', value, re.I)
            or re.match(r'^employee\s+(?:id\b|#|\d)', value, re.I) or not 2 <= len(value) <= 120):
        return None
    return value


def project_name(message):
    message = query_text(message)
    before = re.sub(r'^\s*(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:(?:show|give|tell|find|get|list)(?:\s+me)?\s+|i\s+(?:want|need)\s+)', '', message, flags=re.I)
    patterns = [
        r'\b' + TASK_SUBJECT + r'\s+(?:of|for|in|from|related\s+to|belonging\s+to)\s+(?:the\s+)?(?P<name>.+?)' + NAME_END,
        r'\bproject(?:\s+name)?\s*:?\s+(?:is\s+)?["\']?(?P<name>.+?)["\']?' + NAME_END,
        r'^(?P<name>.+?)\s+project\s+(?:tasks?|details|history|report)\b',
    ]
    for index, pattern in enumerate(patterns):
        if index == 0 and not re.search(r'\bproject\b', message, re.I) and not latest_request(message):
            continue
        match = re.search(pattern, before if index == 2 else message, re.I)
        if match:
            name = clean_name(match['name'])
            if name:
                return name
    return None


def latest_request(message):
    patterns = [
        r'\b(?P<mode>' + RECENCY + r')\s+(?:the\s+)?(?:(?P<count>' + NUMBER + r')\s+)?' + TASK_SUBJECT + r'\b',
        r'\b(?P<count>' + NUMBER + r')\s+(?P<mode>' + RECENCY + r')\s+' + TASK_SUBJECT + r'\b',
        r'\b' + TASK_SUBJECT + r'\s+(?P<mode>' + RECENCY + r')\s+(?P<count>' + NUMBER + r')\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.I)
        if match:
            word = (match['count'] or 'five').lower()
            return (int(word) if word.isdigit() else NUMBER_WORDS[word], match['mode'].lower() in ('next', 'more'))
    return None


def clarification_name(message):
    message = query_text(message)
    value = re.sub(r'^(?:the\s+)?project(?:\s+name)?\s+(?:is\s+)?|^(?:it\s+is|it\x27s)\s+', '', message.strip(), flags=re.I)
    if (re.search(r'\b(?:attendance|employee|tasks?|show|give|tell|why|how|when|today|yesterday|page|scope|cancel)\b', value, re.I)
            or value.casefold().strip(' .?!') in ('hi', 'hello', 'thanks', 'yes', 'no', 'okay', 'ok')
            or not re.search(r'[a-zA-Z]', value)):
        return None
    return clean_name(value)


def period(message):
    dates = re.findall(r'\b(?:20\d{2}-\d{2}-\d{2}|\d{2}/\d{2}/20\d{2})\b', message)
    dates = [value if '/' not in value else '-'.join(reversed(value.split('/'))) for value in dates]
    if re.search(r'\bfrom\b.*\b(?:to|until|through)\b', message, re.I) and len(dates) != 2:
        raise ValueError('Specify both range endpoints.')
    if len(dates) > 2:
        raise ValueError('Specify two endpoints.')
    if dates:
        return dates[0], dates[-1]
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    if re.search(r'\b(?:last|past)\s+(?:7|seven)\s+days\b', message, re.I):
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if re.search(r'\byesterday\b', message, re.I):
        day = (today - timedelta(days=1)).isoformat()
        return day, day
    if re.search(r'\btoday\b', message, re.I):
        return today.isoformat(), today.isoformat()
    month = re.search(r'\b(20\d{2})-(0[1-9]|1[0-2])\b', message)
    if month:
        return month[0] + '-01', month[0] + f'-{calendar.monthrange(int(month[1]), int(month[2]))[1]:02d}'
    return None


async def project_reply(message, context, identity, backend):
    message = query_text(message)
    name = project_name(message)
    latest = latest_request(message)
    pending_name = context.get('kind') == 'project_tasks' and context.get('needs_project_name') is True
    name_reply = clarification_name(message) if pending_name else None
    batch_reply = context.get('kind') == 'project_tasks' and re.fullmatch(
        r'\s*(?:please\s+)?(?:show(?:\s+me)?\s+more|next\s+batch|continue)\s*[?.]?\s*', message, re.I)
    if not re.search(r'\bproject\b', message, re.I) and re.search(r'\bemployee\s*(?:id|#|\d)', message, re.I):
        return None
    period_only = re.fullmatch(r'\s*(?:for\s+)?(?:[\d/\-]+(?:\s+to\s+[\d/\-]+)?|today|yesterday|(?:last|past)\s+(?:7|seven)\s+days|next\s+page|page\s+\d+)\s*[?.]?\s*', message, re.I)
    followup = context.get('kind') == 'project_tasks' and (
        period_only or name_reply or batch_reply
        or re.search(r'\b(?:this|that|same)\s+project\b', message, re.I)
        or (latest and not name))
    if not name and not followup and not latest and not re.search(r'\bproject\b', message, re.I):
        return None
    if re.search(r'\b(?:pdf|xlsx|export|excel)\b', message, re.I):
        return ('Project task file exports are not implemented. You can ask to view project task details.', [], [])
    scope = ('team' if re.search(r'\b(?:my|our|the)\s+team\b|\bteam\s+scope\b', message, re.I)
             else 'company' if re.search(r'\b(?:my|our|the)\s+company\b|\bcompany\s+scope\b', message, re.I)
             else 'self' if re.search(r'\bmy\b', message, re.I)
             else 'company' if re.search(r'\bcompany\b', message, re.I) or 'company' in identity.scopes
             else 'team' if 'team' in identity.scopes else 'self') if not followup else context['scope']
    used = []
    selected = dict(context) if followup else {'kind': 'project_tasks', 'scope': scope, 'source': 'local_project_search'}
    if name_reply:
        name = name_reply
    if name:
        selected.pop('project_id', None)
        selected['project_name'] = name
    elif followup and not selected.get('project_id') and not pending_name:
        name = selected.get('project_name')
    employee = re.search(r'\bemployee\s*(?:id\s*[:#]?\s*)?#?\s*(\d+)\b', message, re.I)
    if employee:
        selected = {**selected, 'employee_id': int(employee[1])}
    try:
        dates = period(message)
        limit = latest[0] if latest else selected.get('latest_limit') if followup else None
        if limit is not None and not 1 <= limit <= 50:
            raise ValueError('Select 1 to 50 tasks.')
        if dates:
            ProjectTaskArgs(scope=scope, project_id=1, start_date=dates[0], end_date=dates[1])
        elif name_reply or batch_reply or (latest and latest[1]) or re.search(r'\b(?:next\s+page|page\s+\d+)\b', message, re.I):
            dates = (selected.get('start_date'), selected.get('end_date'))
        selected.update(latest_limit=limit)
        if dates:
            selected.update(start_date=dates[0], end_date=dates[1])
        if not name and not selected.get('project_id'):
            selected['needs_project_name'] = True
            return (AssistantReply('Which project should I check? Please provide its name.',
                                   model_content='A project name is required; the task request was retained locally.', query_context=selected), [], [])
        if name:
            result = await backend.find_project(identity, scope, name)
            used.append('project_search')
            matches = result['matches']
            if result['has_more'] or len(matches) != 1:
                selected.pop('project_id', None)
                selected.pop('project_name', None)
                selected['needs_project_name'] = True
                return (AssistantReply('No unique authorized project match. Please provide the full project name. '
                                       'This does not establish whether a project exists outside your scope.',
                                       model_content='Project lookup requires clarification.', query_context=selected), used, [])
            selected.update(project_id=matches[0]['project_id'], project_name=matches[0]['name'], needs_project_name=False)
        next_batch = bool(batch_reply) or bool(latest and latest[1]) or bool(re.search(r'\bnext\s+page\b', message, re.I))
        if next_batch:
            if not context.get('project_id') or (latest and context.get('latest_limit') != limit):
                return (AssistantReply('Ask for the last tasks first, then request the next batch with the same size.',
                                       model_content='Task pagination needs clarification.', query_context=selected), used, [])
        if (not dates or not all(dates)) and limit is None:
            return (AssistantReply(f"Selected project {selected['project_name']}. Which date or date range? "
                                   'Use DD/MM/YYYY, YYYY-MM, or DD/MM/YYYY to DD/MM/YYYY (maximum 93 days).',
                                   model_content='A project was selected locally; a report period is required.', query_context=selected), used, [])
        page = re.search(r'\bpage\s+(\d+)\b', message, re.I)
        number = int(page[1]) if page else selected.get('page', 1) + 1 if next_batch else 1
        dates = dates or (None, None)
        args = ProjectTaskArgs(scope=scope, project_id=selected['project_id'], employee_id=selected.get('employee_id'),
                               start_date=dates[0], end_date=dates[1], page=number, latest_limit=limit)
        result = await backend.tasks(identity, args, daily=True)
    except (ValueError, ValidationError):
        return (AssistantReply('Please provide a valid date range of 1 to 93 days, a valid page number, or 1 to 50 latest tasks.',
                               model_content='Project period needs clarification.', query_context=selected), used, [])
    local = result.local_reply
    updated = {**selected, 'start_date': args.start_date.isoformat() if args.start_date else None,
               'end_date': args.end_date.isoformat() if args.end_date else None, 'page': args.page, 'latest_limit': limit}
    return (AssistantReply(str(local), model_content='Authorized project task details were displayed locally. Names and descriptions remain local.',
                           query_context=updated), used + ['get_project_tasks'], [])
