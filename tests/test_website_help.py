import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.website_help import website_help, refresh_help_history
from tests.test_backend import backend, headers
from tests.test_semantic_query import enable


@pytest.mark.parametrize('question', [
    'How do I add an employee?', 'How to create a task?', 'Where can I find my attendance?',
    'Help me register staff', 'Show me the steps to apply for leave', 'Why can\'t I access LMS?',
])
def test_help_precedes_model_and_report_routing(backend, question):
    client, state = backend
    model = enable(client, state, [])
    service = client.app.state.chat
    original = service.backend.call
    calls = []

    async def call(endpoint, token, body=None):
        if endpoint != 'help':
            return await original(endpoint, token, body)
        calls.append(body)
        return dict(topic_id='employee_add', status='answered', reply='Website help: Add an employee\n1. Open the employee creation page.')

    service.backend.call = call
    response = client.post('/v1/chat', headers=headers(), json={'message': question})
    assert response.status_code == 200, response.text
    assert response.json()['data']['reply'].startswith('Website help:')
    assert calls == [{'message': question, 'previous_topic': None}]
    assert model.await_count == 0
    assert 'manager-report' not in state['calls'] and 'attendance' not in state['calls']
    cid = response.json()['data']['conversation_id']
    history = client.get('/v1/conversations/' + cid, headers=headers()).json()['data']
    assert history['messages'][-1]['content'].startswith('Website help:')
    assert client.get('/v1/conversations/' + cid, headers=headers(company=2)).status_code == 404


@pytest.mark.parametrize('question', [
    'Show my attendance for this month.', 'How many employees are present today?',
    'How much effort did Joydeep book last month?', 'Convert it into a pie chart',
    'Add an employee named Alice', 'Show approved leave for my company this month.',
    'Show my latest 10 tasks', 'video',
])
def test_report_and_mutation_requests_are_not_intercepted(question):
    backend = SimpleNamespace(call=AsyncMock())
    assert asyncio.run(website_help(question, {}, SimpleNamespace(credential='test'), backend)) is None
    backend.call.assert_not_awaited()


def test_help_followup_uses_topic_not_cached_answer():
    backend = SimpleNamespace(call=AsyncMock(return_value=dict(topic_id='leave_apply',
        status='permission_denied', reply='Your account no longer has access.')))
    result = asyncio.run(website_help('show the video', {'kind': 'website_help', 'topic_id': 'leave_apply'},
        SimpleNamespace(credential='test'), backend))
    assert result[0] == 'Your account no longer has access.'
    assert backend.call.call_args.args[2]['previous_topic'] == 'leave_apply'
    assert result[0].query_context['kind'] == 'website_help'


@pytest.mark.parametrize('status', [404, 503])
def test_partial_deployment_does_not_fall_back_to_paid_model(status):
    backend = SimpleNamespace(call=AsyncMock(side_effect=HTTPException(status, 'Unavailable')))
    result = asyncio.run(website_help('How do I create a task?', {}, SimpleNamespace(credential='test'), backend))
    assert 'currently unavailable' in result[0]


def test_invalid_backend_help_response_is_rejected():
    backend = SimpleNamespace(call=AsyncMock(return_value={'topic_id': 'bad', 'status': 'answered', 'reply': ''}))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(website_help('How do I create a task?', {}, SimpleNamespace(credential='test'), backend))
    assert exc.value.status_code == 502


def test_history_rechecks_help_once_per_topic_and_removes_revoked_links():
    backend = SimpleNamespace(call=AsyncMock(return_value=dict(topic_id='lms_lead_add',
        status='permission_denied', reply='Your account does not have LMS access.')))
    rows = [dict(role='assistant', content='Website help: Create lead\nOpen page: admin/lms/leads/create',
        query_context=json.dumps({'kind': 'website_help', 'topic_id': 'lms_lead_add'}))] * 2
    result = asyncio.run(refresh_help_history(rows, SimpleNamespace(credential='test'), backend))
    assert all('Open page:' not in row['content'] for row in result)
    assert backend.call.await_count == 1
    assert 'Open page:' in rows[0]['content']


def test_mobile_platform_is_preserved_in_followups_and_history():
    backend = SimpleNamespace(call=AsyncMock(return_value=dict(platform='android', topic_id='mobile_leave_apply',
        status='answered', reply='Mobile app help: Apply for leave\n1. Tap Leave.')))
    identity = SimpleNamespace(credential='test')
    result = asyncio.run(website_help('How do I apply for leave on Android?', {}, identity, backend))
    context = result[0].query_context
    assert context['help_platform'] == 'android'
    asyncio.run(website_help('show the steps', context, identity, backend))
    assert backend.call.call_args.args[2]['platform'] == 'android'
    asyncio.run(website_help('on website', context, identity, backend))
    assert backend.call.call_args.args[2]['message'] == 'on website'
    rows = [dict(role='assistant', content='old', query_context=json.dumps(context))]
    assert asyncio.run(refresh_help_history(rows, identity, backend))[0]['content'].startswith('Mobile app help:')


def test_platform_clarification_retains_original_question():
    backend = SimpleNamespace(call=AsyncMock(side_effect=[
        dict(platform='web', topic_id=None, status='clarify', reply='Website or mobile?'),
        dict(platform='mobile', topic_id='mobile_leave_apply', status='answered', reply='Mobile app help: Apply for leave'),
    ]))
    identity = SimpleNamespace(credential='test')
    result = asyncio.run(website_help('How do I apply for leave on website or mobile?', {}, identity, backend))
    asyncio.run(website_help('mobile', result[0].query_context, identity, backend))
    assert 'apply for leave' in backend.call.call_args.args[2]['message']
    assert 'website' not in backend.call.call_args.args[2]['message']


def test_client_platform_hint_is_not_employee_scope_or_permission():
    backend = SimpleNamespace(call=AsyncMock(return_value=dict(platform='mobile', topic_id='mobile_task_create',
        status='permission_denied', reply='Task access is not granted.')))
    result = asyncio.run(website_help('How do I add a task?', {}, SimpleNamespace(credential='test'), backend, 'mobile'))
    assert result[0] == 'Task access is not granted.'
    assert backend.call.call_args.args[2]['client_platform'] == 'mobile'
    assert not {'company_id', 'user_id', 'scope'} & backend.call.call_args.args[2].keys()


@pytest.mark.parametrize('phrase,target', [
    ('Show the website steps instead.', 'web'),
    ('Can you show me the website instructions instead?', 'web'),
    ('Switch to the web version', 'web'),
    ('Show the mobile app steps instead.', 'mobile'),
    ('Give me the Android instructions instead', 'android'),
    ('Change to the iPhone version', 'ios'),
])
def test_platform_switch_in_chat_retains_topic_without_model_call(backend, phrase, target):
    client, state = backend
    model = enable(client, state, [])
    service = client.app.state.chat
    original = service.backend.call
    calls = []

    async def call(endpoint, token, body=None):
        if endpoint != 'help':
            return await original(endpoint, token, body)
        calls.append(body)
        platform = 'mobile' if len(calls) == 1 else target
        return dict(platform=platform, topic_id='effort_book' if platform == 'web' else 'mobile_effort',
                    status='answered', reply=('Website help: ' if platform == 'web' else 'Mobile app help: ') + 'Book effort against a task')

    service.backend.call = call
    first = client.post('/v1/chat', headers=headers(), json={'message': 'How do I book effort in the app?'})
    assert first.status_code == 200, first.text
    second = client.post('/v1/chat', headers=headers(), json={'message': phrase,
        'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200, second.text
    assert len(calls) == 2
    assert calls[-1]['previous_topic'] == 'mobile_effort'
    assert calls[-1]['platform'] == 'mobile'
    assert calls[-1]['message'] == phrase
    assert second.json()['data']['tools_used'] == ['website_help']
    model.assert_not_awaited()
