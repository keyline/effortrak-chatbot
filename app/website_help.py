"""Route website/mobile instructions locally, without model calls or report access."""

import json
import re
from typing import Literal

from fastapi import HTTPException
from pydantic import Field, ValidationError

from app.schemas import StrictModel
from app.task_report import AssistantReply


INSTRUCTION = re.compile(
    r'^\s*(?:please\s+)?(?:how\s+(?:do|can|should|to|would|could|does\s+one)\b|'
    r'(?:can|could|may)\s+(?:i|we)\s+(?:please\s+)?(?:add|create|register|onboard|'
    r'edit|update|change|deactivate|disable|enable|assign|reassign|reschedule|book|'
    r'correct|delete|remove|apply|approve|reject|configure|set\s+up|import|upload|'
    r'invite|reset|send|finish|close|return|submit|increase|reduce)\b|'
    r'(?:can|could|would)\s+you\s+(?:please\s+)?(?:help\s+me|(?:show|tell|teach)\s+me\s+how(?=\s+(?:to|do|can|should|would|could|i|we|one)\b))\b|'
    r'(?:can|could|would)\s+you\s+(?:please\s+)?(?:explain|describe)\s+'
    r'(?=(?:the\s+)?(?:steps|process|procedure|workflow|setup|bulk|how\s+to|'
    r'upload(?:ing)?|import(?:ing)?|reset(?:ting)?|send(?:ing)?|assign(?:ing)?|'
    r'creat(?:e|ing)|add(?:ing)?|configur(?:e|ing))\b)|'
    r'what\s+(?:are\s+the\s+steps|is\s+the\s+(?:process|procedure))\s+(?:for|to)\b|'
    r'(?:(?:my|the)\s+)?(?:otp|verification\s+code)\s+(?:is\s+)?(?:not\s+(?:received|arriving|coming|working)|invalid|expired)\b|'
    r'(?:my\s+)?(?:mobile\s+app|app)\s+(?:is\s+)?(?:not\s+(?:syncing|updating|loading)|sync\s+(?:issue|problem))\b|'
    r'where\s+(?:do\s+i|can\s+i|is\s+the|to)\b|'
    r'(?:help|guide|teach)\s+me\b|(?:show|give)\s+me\s+(?:the\s+)?(?:steps|instructions|tutorial)\b|'
    r'(?:steps|instructions|tutorial|guide)\s+(?:for|to|on)\b|'
    r'i\s+(?:want|need)\s+to\s+(?:learn|know\s+how)\b|'
    r'why\s+(?:can\s*not|can.t|don.t)\s+i\s+(?:see|access|open)\b)', re.I)
FOLLOWUP = re.compile(r'^\s*(?:(?:please|show|give|me|the|that|this|same|again|a|an|open|page|link|video|tutorial|steps|instructions|repeat)\s*)+[?.!]*\s*$', re.I)
PLATFORM_FOLLOWUP = re.compile(r'^\s*(?:(?:please|can|could|would|you|show|give|me|steps|instructions|instead|switch|change|to|version|on|in|for|the|what|about|same|mobile|phone|app|android|iphone|ios|website|web|browser)\s*)+[?.!]*\s*$', re.I)


class HelpResult(StrictModel):
    can_classify: bool = False
    platform: Literal['web', 'mobile', 'android', 'ios'] = 'web'
    topic_id: str | None = Field(max_length=80, pattern=r'^[a-z][a-z0-9_]*$')
    status: str = Field(pattern=r'^(answered|clarify|module_disabled|permission_denied)$')
    reply: str = Field(min_length=1, max_length=12000)


async def website_help(message, context, identity, backend, client_platform='web', service=None):
    previous = context.get('topic_id') if context.get('kind') == 'website_help' else None
    help_context = context.get('kind') == 'website_help'
    if not INSTRUCTION.search(message) and not (help_context and (FOLLOWUP.fullmatch(message) or PLATFORM_FOLLOWUP.fullmatch(message))):
        return None
    question = message
    if help_context and not previous and PLATFORM_FOLLOWUP.fullmatch(message) and context.get('help_question'):
        question = re.sub(r'\b(?:website|web|browser|desktop portal|mobile|phone|app|android|iphone|ios)\b', '', context['help_question'], flags=re.I)[:3800] + ' ' + message
    body = {'message': question, 'previous_topic': previous}
    if context.get('help_platform'):
        body['platform'] = context['help_platform']
    if client_platform != 'web':
        body['client_platform'] = client_platform
    try:
        data = HelpResult.model_validate(await backend.call('help', identity.credential,
            body))
        if service is not None and data.status == 'clarify' and data.can_classify and data.topic_id is None:
            from app.help_features import classify_guide
            chosen = await classify_guide(service, question, identity, data.platform)
            if chosen is not None:
                selected = HelpResult.model_validate(await backend.call('help', identity.credential,
                    {'operation': 'select', 'guide_id': chosen, 'platform': data.platform}))
                if selected.topic_id != chosen or selected.platform != data.platform:
                    raise HTTPException(502, 'Invalid help selection response.')
                data = selected
    except ValidationError:
        raise HTTPException(502, 'Invalid help response.') from None
    except HTTPException as exc:
        if exc.status_code not in (404, 503):
            raise
        return (AssistantReply('Help is currently unavailable. Please contact your administrator; no records have been changed.',
            model_content='Website help was unavailable; no model was called.', query_context={}), [], [])
    return (AssistantReply(data.reply,
        model_content='A help question used reviewed guides and current backend access checks. No backend records or model-authored instructions were returned.',
        query_context={'kind': 'website_help', 'topic_id': data.topic_id, 'help_platform': data.platform,
                       'help_question': question if INSTRUCTION.search(question) else context.get('help_question', '')}), ['website_help'], [])


async def refresh_help_history(rows, identity, backend):
    """Re-authorize saved help links after module/seat changes, without global caches."""
    checked = {}
    messages = []
    for row in rows:
        context = json.loads(row.get('query_context') or '{}')
        if row['role'] == 'assistant' and context.get('kind') == 'website_help' and context.get('topic_id'):
            topic = (context['topic_id'], context.get('help_platform', 'web'))
            if topic not in checked:
                result = await website_help('repeat the steps', context, identity, backend)
                checked[topic] = str(result[0])
            row = {**row, 'content': checked[topic]}
        messages.append(row)
    return messages
