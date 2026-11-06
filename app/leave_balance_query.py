"""Read-only leave balance rankings; employee records never enter model history."""

import re
from typing import Literal

from fastapi import HTTPException
from pydantic import Field, ValidationError

from app.schemas import StrictModel
from app.task_report import AssistantReply


class LeaveBalanceArgs(StrictModel):
    scope: Literal['self', 'team', 'company']
    financial_year: str | None = Field(default=None, pattern=r'^20\d{2}(?:-\d{2}(?:\d{2})?)?$')
    leave_type: str | None = Field(default=None, min_length=1, max_length=100)
    limit: int = Field(default=10, ge=1, le=50)
    balance_filter: Literal['positive', 'nonnegative', 'all', 'zero', 'negative'] = 'positive'
    ranking: Literal['highest', 'lowest'] = 'highest'


class BalanceRow(StrictModel):
    employee_id: int = Field(gt=0)
    employee_name: str = Field(min_length=1, max_length=255)
    allocated_days: float = Field(allow_inf_nan=False)
    used_days: float = Field(allow_inf_nan=False)
    remaining_days: float = Field(allow_inf_nan=False)


class BalanceGroup(StrictModel):
    leave_type: str
    employee_count: int = Field(ge=0)
    unlimited_count: int = Field(ge=0)
    rows: list[BalanceRow] = Field(max_length=50)


class BalanceData(StrictModel):
    status: Literal['answered']
    data_source: Literal['effortrak_backend']
    scope: Literal['self', 'team', 'company']
    financial_year: str
    start_date: str
    end_date: str
    limit: int = Field(ge=1, le=50)
    groups: list[BalanceGroup]
    notice: str
    balance_filter: Literal['positive', 'nonnegative', 'all', 'zero', 'negative'] = 'positive'
    ranking: Literal['highest', 'lowest'] = 'highest'


FILTER_LABELS = {
    'nonnegative': 'Zero and positive remaining balances only.',
    'positive': 'Positive remaining balances only.',
    'all': 'All recorded balances (including zero and negative).',
    'zero': 'Zero remaining balances only.',
    'negative': 'Negative remaining balances only.',
}


def requested_filter(text, default='positive'):
    if re.search(r'\b(?:positive\s+(?:remaining\s+|leave\s+)*balances?|only\s+positive)\b', text):
        return 'positive'
    if default == 'nonnegative' and re.search(r'\b(?:including|include)\s+(?:zero|0)\b', text) and not re.search(r'\bnegative\b', text):
        return 'nonnegative'
    if re.search(r'\b(?:all\s+(?:recorded\s+)?(?:leave\s+)?balances|including\s+(?:zero|0|negative)|include\s+(?:zero|0|negative))\b', text):
        return 'all'
    if re.search(r'\b(?:zero|0|no)\s+(?:(?:remaining|leave)\s+)*(?:balances?|leave\s+left)\b|\bexhausted\s+leave\b', text):
        return 'zero'
    if re.search(r'\b(?:negative|overdrawn)\s+(?:(?:remaining|leave)\s+)*balances?\b', text):
        return 'negative'
    return default


def parse_question(message):
    text = re.sub(r'\s+', ' ', message.strip()).lower()
    if (re.search(r'\b(?:and|also|plus|as\s+well\s+as)\b', text)
            and re.search(r'\b(?:attendance|punch|timesheets?|tasks?|booked\s+effort|client\s+visits?|headcount|employee\s+count)\b', text)):
        return None
    if not re.search(r'\b(?:leave|leaves|pto)\b', text):
        return None
    if re.search(r'\b(?:add|edit|delete|adjust|approve|reject|allocate|increase|reduce|how\s+(?:to|do|can))\b', text):
        return None
    ranking = 'lowest' if re.search(r'\b(?:lowest|least|fewest|smallest|minimum|bottom|ascending)\b', text) else 'highest'
    if not re.search(r'\b(?:highest|most|greatest|maximum|largest|top|lowest|least|fewest|smallest|minimum|bottom|ascending|rank(?:ing|ed)?)\b', text) and requested_filter(text) == 'positive':
        return None
    if not re.search(r'\b(?:balances?|remaining|left|unused|available)\b', text):
        return None
    if re.search(r'\b(?:export|download|pdf|xlsx|excel|chart|graph|as\s+of|my\s+own)\b', text):
        return {'error': 'This report ranks company or team balances by configured financial year. Historical snapshots, exports and charts are not supported.'}
    if re.search(r'\b(?:top|bottom|lowest|highest)\s+(?!\d+\b)(?:one|two|three|four|five|six|seven|eight|nine|ten|twenty|fifty)\b', text):
        return None
    years = re.findall(r'\b20\d{2}(?:\s*[-/]\s*(?:20)?\d{2})?\b', text)
    if len(years) > 1 or re.search(r'\b(?:last|previous|next)\s+(?:financial\s+|fiscal\s+)?year\b|\b(?:month|week|yesterday|calendar)\b', text):
        return {'error': 'Please specify the financial year by its start year (for example 2026 or 2026-27). Current-year requests use your company\'s active financial year.'}
    number = re.search(r'\b(?:top|bottom|lowest|highest)\s+(\d+)\b|\b(\d+)\s+(?:employees?|staff|people)\b', text)
    leave_type = None
    quoted = re.search(r'["\u201c]([^"\u201d]+)["\u201d]\s+leave', message, re.I)
    common = re.search(r'\b(annual|casual|sick|earned|privilege|paid|unpaid|compensatory|maternity|paternity|bereavement)\s+leave\b', text)
    if quoted:
        leave_type = quoted[1].strip() + ' Leave'
    elif common:
        leave_type = common[1].title() + ' Leave'
    elif re.search(r"\b\w+['\u2019]s\s+.*\bleave\b", text):
        return None
    elif re.search(r'\b(?:for|type)\s+([a-z]{2,5})\b', text) and re.search(r'\b(?:cl|sl|el|pl|pto)\b', text):
        return {'error': 'Please use the full configured leave-type name rather than an abbreviation.'}
    elif re.search(r'\b(\w+)\s+leave\b', text):
        before = re.search(r'\b(\w+)\s+leave\b', text)[1]
        if before not in {'highest', 'most', 'greatest', 'maximum', 'largest', 'lowest', 'least', 'fewest', 'smallest', 'minimum', 'remaining', 'unused', 'available',
                          'the', 'their', 'with', 'by', 'for', 'of', 'on', 'a', 'any', 'has', 'have', 'zero', '0', 'no', 'negative', 'overdrawn', 'all'} and not (before.isdigit() and number):
            return None
    return dict(financial_year=re.sub(r'\s*[-/]\s*', '-', years[0]) if years else None,
                leave_type=leave_type, limit=int(number[1] or number[2]) if number else 10,
                scope='company' if re.search(r'\b(?:company|organization|organisation)\b', text) else 'team' if re.search(r'\bteam\b', text) else None,
                balance_filter=requested_filter(text, 'nonnegative' if ranking == 'lowest' else 'positive'), ranking=ranking)


def local_reply(text, args=None):
    return (AssistantReply(text, model_content='An authorized leave-balance ranking was handled locally.',
                           query_context={'kind': 'leave_balance', 'source': 'local_leave_balance', 'args': args or {}}), [], [])


async def balance_report(identity, backend, selected):
    if 'leave' not in identity.modules:
        raise HTTPException(403, 'You do not have access to the requested module.')
    selected = dict(selected)
    selected.setdefault('balance_filter', 'nonnegative' if selected.get('ranking') == 'lowest' else 'positive')
    selected['scope'] = selected.get('scope') or ('company' if 'company' in identity.scopes else 'team' if 'team' in identity.scopes else 'company')
    try:
        args = LeaveBalanceArgs.model_validate(selected)
    except ValidationError:
        return local_reply('Please choose a financial year, a full leave-type name, and a top count between 1 and 50.', selected)
    if args.scope not in identity.scopes:
        raise HTTPException(403, 'You do not have permission for the requested employee scope.')
    data = await backend.call('leave-balance', identity.credential, args.model_dump())
    if data.get('status') == 'clarify':
        return local_reply('No unique configured financial year matches. Please check company financial-year setup or specify its start year.', args.model_dump())
    try:
        result = BalanceData.model_validate(data)
        if result.scope != args.scope or result.limit != args.limit or result.balance_filter != args.balance_filter or result.ranking != args.ranking:
            raise ValueError('Unexpected report scope')
        for group in result.groups:
            if len(group.rows) > args.limit or len(group.rows) > group.employee_count:
                raise ValueError('Invalid ranking size')
            if any(abs(row.allocated_days - row.used_days - row.remaining_days) > .011 or row.allocated_days == -1 for row in group.rows):
                raise ValueError('Invalid balance')
            if any((a.remaining_days > b.remaining_days if args.ranking == 'lowest' else a.remaining_days < b.remaining_days) for a, b in zip(group.rows, group.rows[1:])):
                raise ValueError('Invalid ranking order')
            if any((args.balance_filter == 'positive' and row.remaining_days <= 0)
                   or (args.balance_filter == 'nonnegative' and row.remaining_days < 0)
                   or (args.balance_filter == 'zero' and row.remaining_days != 0)
                   or (args.balance_filter == 'negative' and row.remaining_days >= 0) for row in group.rows):
                raise ValueError('Unexpected balance filter')
    except (ValidationError, ValueError):
        raise HTTPException(502, 'Invalid backend leave balance response.') from None
    clean = lambda text: re.sub(r'[\r\n\t|]+', ' ', text).strip()
    lines = [('Lowest' if args.ranking == 'lowest' else 'Highest') + ' remaining leave balances',
             f'Financial year: {clean(result.financial_year)} | {result.start_date} to {result.end_date}',
             f'Scope: {result.scope}. {"Bottom" if args.ranking == "lowest" else "Top"} {result.limit} employees per leave type.',
             'Balance filter: ' + FILTER_LABELS[result.balance_filter]]
    if not result.groups:
        lines.append('No matching balance records were found within your authorized scope. Filter: ' + FILTER_LABELS[result.balance_filter] + ' Check the configured leave-type name and selected financial year.')
    for group in result.groups:
        lines.extend(['', clean(group.leave_type)])
        for rank, row in enumerate(group.rows, 1):
            lines.append(f'{rank}. {clean(row.employee_name)}: {row.remaining_days:g} days remaining (allocated {row.allocated_days:g}; used {row.used_days:g}).')
        if group.employee_count > len(group.rows):
            lines.append(f'Showing {len(group.rows)} of {group.employee_count} finite balances.')
        if group.unlimited_count:
            lines.append(f'Unlimited entitlement: {group.unlimited_count} employee(s), excluded from numeric ranking.')
    lines.extend(['', result.notice])
    reply, _, _ = local_reply('\n'.join(lines), args.model_dump())
    reply.query_context['leave_types'] = [group.leave_type for group in result.groups]
    return reply, ['get_leave_balance_ranking'], []


async def leave_balance_reply(message, context, identity, backend):
    parsed = parse_question(message)
    if parsed is None and context.get('kind') == 'leave_balance' and context.get('args'):
        type_text = re.sub(r'^\s*(?:for|only)\s+', '', message, flags=re.I).strip(' .!?')
        leave_type = next((value for value in context.get('leave_types', []) if value.casefold() == type_text.casefold()), None)
        if leave_type is None and re.fullmatch(r'[^\W\d_]+(?:[ -][^\W\d_]+)*\s+leave', type_text, re.I):
            leave_type = type_text
        top = re.fullmatch(r'\s*(?:show\s+)?(?:the\s+)?top\s+(\d+)(?:\s+(?:instead|please))?\s*[.!?]?\s*', message, re.I)
        year = re.fullmatch(r'\s*(?:for\s+)?(20\d{2}(?:-\d{2}(?:\d{2})?)?)\s*[.!?]?\s*', message)
        filter_followup = re.fullmatch(r'\s*(?:(?:show|include|only|me|the|with|balances?|remaining|instead|please|also|and|all|recorded|zero|0|negative|positive)\s*)+[.!?]?\s*', message, re.I)
        direction = re.fullmatch(r'\s*(?:(?:show|me|the|same|but|sort|by|order|instead|please|first)\s+)*(lowest|highest|ascending|descending)(?:\s+(?:instead|first|order|please))*\s*[.!?]?\s*', message, re.I)
        if top or year or filter_followup or direction or leave_type:
            parsed = dict(context['args'])
            if top: parsed['limit'] = int(top[1])
            elif year: parsed['financial_year'] = year[1]
            elif direction:
                parsed['ranking'] = 'lowest' if direction[1].lower() in ('lowest', 'ascending') else 'highest'
                parsed['balance_filter'] = 'nonnegative' if parsed['ranking'] == 'lowest' else 'positive'
            elif leave_type: parsed['leave_type'] = leave_type
            else: parsed['balance_filter'] = requested_filter(message.lower().strip(' .!?') + ' balances')
    if parsed is None:
        return None
    if 'error' in parsed:
        previous = context.get('args') if context.get('kind') == 'leave_balance' else None
        result = local_reply(parsed['error'], previous)
        result[0].query_context['leave_types'] = context.get('leave_types', []) if previous else []
        return result
    return await balance_report(identity, backend, parsed)
