import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.help_features import classify_guide, followup_suggestions
from app.website_help import website_help
from tests.test_token_usage import setup, rows
from tests.test_backend import backend, headers


def response(choice=None, **changes):
    return NS(status='completed', output=[NS(type='function_call', name='choose_help_guide',
        arguments=json.dumps(choice or {'guide_id': 'employee_add', 'confidence': 'high'}))], **changes)


def service():
    async def call(endpoint, credential, body):
        assert endpoint == 'help' and credential == 'trusted'
        operation = body.get('operation', 'answer')
        if operation == 'catalogue':
            return {'guides': [{'id': 'employee_add', 'title': 'Add an employee'}]}
        if operation == 'select':
            return {'platform': 'web', 'topic_id': 'employee_add', 'status': 'answered',
                    'reply': 'Website help: Add an employee\n1. Verified server instructions.'}
        return {'platform': 'web', 'topic_id': None, 'status': 'clarify', 'can_classify': True,
                'reply': 'Please clarify the action.'}
    return NS(settings=NS(ai_help_llm_fallback_enabled=True, chat_mode='openai', openai_model='test-model'),
              backend=NS(call=AsyncMock(side_effect=call)), client=object(), token_meter=object(),
              create_response=AsyncMock(return_value=response()))


def test_classifier_returns_only_backend_reauthorized_instructions():
    agent = service()
    result = asyncio.run(website_help('How do I bring a new colleague into the system?', {},
        NS(credential='trusted'), agent.backend, service=agent))
    assert str(result[0]) == 'Website help: Add an employee\n1. Verified server instructions.'
    args, kwargs = agent.create_response.call_args
    assert args[1] == 'help_classification'
    assert kwargs['max_output_tokens'] == 192 and kwargs['store'] is False
    assert 'Verified server instructions' not in kwargs['instructions']
    assert kwargs['tools'][0]['strict'] is True
    assert agent.backend.call.call_args.args[2] == {'operation': 'select', 'guide_id': 'employee_add', 'platform': 'web'}


@pytest.mark.parametrize('change', ['disabled', 'unmetered', 'unconfigured', 'demo'])
def test_classifier_never_spends_without_opt_in_and_metering(change):
    agent = service()
    if change == 'disabled': agent.settings.ai_help_llm_fallback_enabled = False
    if change == 'unmetered': agent.token_meter = None
    if change == 'unconfigured': agent.client = None
    if change == 'demo': agent.settings.chat_mode = 'demo'
    assert asyncio.run(classify_guide(agent, 'How do I onboard?', NS(credential='trusted'), 'web')) is None
    agent.create_response.assert_not_awaited()
    agent.backend.call.assert_not_awaited()


@pytest.mark.parametrize('choice', [
    {'guide_id': 'unknown', 'confidence': 'high'}, {'guide_id': 'employee_add', 'confidence': 'uncertain'},
    {'guide_id': None, 'confidence': 'uncertain'}, {'guide_id': 'employee_add', 'confidence': 'high', 'steps': 'invented'},
    {'guide_id': ['employee_add'], 'confidence': 'high'},
])
def test_uncertain_or_invalid_model_choice_never_reaches_select(choice):
    agent = service(); agent.create_response.return_value = response(choice)
    assert asyncio.run(classify_guide(agent, 'How do I do this?', NS(credential='trusted'), 'web')) is None
    assert agent.backend.call.await_count == 1


def test_selection_is_rechecked_after_permission_revocation():
    agent = service()
    original = agent.backend.call.side_effect
    async def call(endpoint, credential, body):
        data = await original(endpoint, credential, body)
        if body.get('operation') == 'select':
            data.update(status='permission_denied', reply='Your access has changed.')
        return data
    agent.backend.call.side_effect = call
    result = asyncio.run(website_help('How do I bring a colleague into the system?', {},
        NS(credential='trusted'), agent.backend, service=agent))
    assert str(result[0]) == 'Your access has changed.'


def test_known_and_ambiguous_help_never_calls_classifier():
    agent = service()
    for status, topic in [('answered', 'employee_add'), ('permission_denied', 'employee_add'), ('clarify', None)]:
        agent.backend.call = AsyncMock(return_value={'platform': 'web', 'topic_id': topic, 'status': status, 'reply': 'Existing response.'})
        asyncio.run(website_help('How do I add an employee?', {}, NS(credential='trusted'), agent.backend, service=agent))
    agent.create_response.assert_not_awaited()


def test_classifier_uses_existing_durable_meter_and_budget(tmp_path):
    result = response(id='resp_help', usage=NS(input_tokens=30, output_tokens=20, total_tokens=50,
        input_tokens_details=NS(cached_tokens=0)))
    meter, sdk, identity, calls = setup(tmp_path, result)
    agent = service(); agent.client = sdk; agent.token_meter = meter
    agent.backend.call.side_effect = None
    agent.backend.call.return_value = {'guides': [{'id': 'employee_add', 'title': 'Add an employee'}]}
    async def create(actor, operation, **kwargs):
        return await meter.create(sdk, actor, operation, **kwargs)
    agent.create_response = create
    assert asyncio.run(classify_guide(agent, 'How do I onboard?', identity, 'web')) == 'employee_add'
    assert calls[0][2]['operation'] == 'help_classification'
    assert calls[0][2]['tokens'] == 40 + 192
    assert calls[1][2]['state'] == 'settled' and calls[1][2]['total_tokens'] == 50
    assert rows(meter)[0][2] == 1


def test_classifier_cannot_spend_after_quota_rejection(tmp_path):
    meter, sdk, identity, _ = setup(tmp_path)
    meter.backend.call.side_effect = HTTPException(429, 'Company quota exhausted')
    agent = service(); agent.client = sdk; agent.token_meter = meter
    agent.backend.call.side_effect = None
    agent.backend.call.return_value = {'guides': [{'id': 'employee_add', 'title': 'Add an employee'}]}
    async def create(actor, operation, **kwargs):
        return await meter.create(sdk, actor, operation, **kwargs)
    agent.create_response = create
    with pytest.raises(HTTPException) as exc:
        asyncio.run(classify_guide(agent, 'How do I onboard?', identity, 'web'))
    assert exc.value.status_code == 429
    sdk.responses.create.assert_not_awaited()


@pytest.mark.parametrize('bad', ['empty', 'invalid_json', 'incomplete', 'wrong_tool', 'failure'])
def test_classifier_failures_clarify_without_retry(bad):
    agent = service()
    if bad == 'empty':
        agent.backend.call.side_effect = None; agent.backend.call.return_value = {'guides': []}
    if bad == 'invalid_json': agent.create_response.return_value.output[0].arguments = 'bad json'
    if bad == 'incomplete': agent.create_response.return_value.status = 'incomplete'
    if bad == 'wrong_tool': agent.create_response.return_value.output[0].name = 'execute_action'
    if bad == 'failure': agent.create_response.side_effect = TimeoutError()
    assert asyncio.run(classify_guide(agent, 'How do I do this?', NS(credential='trusted'), 'web')) is None
    assert agent.create_response.await_count <= 1


def test_api_followups_and_history_recheck_permissions_without_model(backend):
    client, _ = backend
    agent = client.app.state.chat
    agent.settings.ai_help_followups_enabled = True
    original = agent.backend.call
    revoked = False
    async def call(endpoint, token, body=None):
        if endpoint != 'help': return await original(endpoint, token, body)
        if body.get('operation') == 'suggestions':
            return {'suggestions': [] if revoked else [{'label': 'View attendance', 'question': 'How do I view my attendance?'}]}
        return {'platform': 'web', 'topic_id': 'employee_add', 'status': 'permission_denied' if revoked else 'answered',
                'reply': 'Access revoked.' if revoked else 'Website help: Add an employee\n1. Verified instructions.'}
    agent.backend.call = call
    first = client.post('/v1/chat', headers=headers(), json={'message': 'How do I add an employee?'})
    assert first.status_code == 200, first.text
    assert first.json()['data']['suggestions'][0]['label'] == 'View attendance'
    cid = first.json()['data']['conversation_id']
    saved = client.get('/v1/conversations/' + cid, headers=headers()).json()['data']['messages']
    assert saved[-1]['suggestions'][0]['label'] == 'View attendance'
    revoked = True
    saved = client.get('/v1/conversations/' + cid, headers=headers()).json()['data']['messages']
    assert saved[-1]['content'] == 'Access revoked.' and saved[-1]['suggestions'] == []
    assert agent.client is None


def test_followups_are_disabled_by_default_and_reauthorize_current_context():
    backend = NS(call=AsyncMock(return_value={'suggestions': [{'label': 'Book effort', 'question': 'How do I book effort?'}]}))
    identity = NS(credential='trusted')
    context = {'kind': 'website_help', 'topic_id': 'task_create', 'help_platform': 'web'}
    assert asyncio.run(followup_suggestions(backend, context, identity, False)) == []
    backend.call.assert_not_awaited()
    result = asyncio.run(followup_suggestions(backend, context, identity, True))
    assert result[0].label == 'Book effort'
    assert backend.call.call_args.args[2]['guide_id'] == 'task_create'
    backend.call.side_effect = HTTPException(403, 'Revoked')
    assert asyncio.run(followup_suggestions(backend, context, identity, True)) == []


def test_report_suggestions_keep_scope_and_skip_long_or_unresolved_periods():
    backend = NS(call=AsyncMock(return_value={'suggestions': []})); identity = NS(credential='trusted')
    selected = {'action': 'booked_time', 'scope': 'team', 'employee_id': 123,
                'start_date': '2026-10-01', 'end_date': '2026-10-20'}
    asyncio.run(followup_suggestions(backend, {'selection': selected}, identity, True))
    assert backend.call.call_args.args[2]['scope'] == 'team'
    assert 'employee_id' not in backend.call.call_args.args[2]
    backend.call.reset_mock()
    selected['end_date'] = '2026-12-20'
    assert asyncio.run(followup_suggestions(backend, {'selection': selected}, identity, True)) == []
    backend.call.assert_not_awaited()
    for context in [
        {'module': 'attendance', 'report': 'work_summary', 'scope': 'self', 'month': '2026-10'},
        {'kind': 'attendance_export', 'module': 'attendance', 'scope': 'self', 'month': '2026-10'},
        {'kind': 'semantic_question', 'selection': {'action': 'clarify', 'scope': 'self'}},
    ]:
        assert asyncio.run(followup_suggestions(backend, context, identity, True)) == []
    backend.call.assert_not_awaited()
