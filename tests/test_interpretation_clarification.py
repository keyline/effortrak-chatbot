import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.auth import Identity
from app.query.interpreter import _interpret_once


CATALOGUE = {'datasets': {'attendance': {
    'fields': ['employee_name', 'date', 'status', 'punch_in', 'punch_out'], 'metrics': ['employee_days'],
}}}
IDENTITY = Identity(1, 1, 'Signed In User', 'Admin', ('self', 'company'))


def agent(subject, effort=None):
    envelope = {'subject': subject, 'selection': {'action': 'query', 'plan': {
        'dataset': 'attendance', 'start_date': '2026-11-17', 'end_date': '2026-11-17',
        'fields': ['employee_name', 'date', 'status'],
    }}}
    return NS(settings=NS(openai_model='gpt-6-luna', openai_reasoning_effort=effort),
        create_response=AsyncMock(return_value=NS(status='completed', output=[
            NS(type='function_call', name='interpret_query', arguments=json.dumps(envelope))])))


@pytest.mark.parametrize(('question', 'subject', 'scope'), [
    ('Show my attendance.', {'kind': 'self'}, 'self'),
    ('Show Example Employee attendance.', {'kind': 'employee', 'employee_name': 'Example Employee'}, 'company'),
])
def test_date_answer_completes_trusted_pending_request_without_losing_subject(question, subject, scope):
    service = agent(subject)
    context = {'kind': 'nl_query', 'pending_question': question, 'plan': None}
    result = asyncio.run(_interpret_once(service, '17/11/2026', context, IDENTITY, CATALOGUE, None))
    assert result.action == 'query' and result.plan.scope == scope
    assert str(result.plan.start_date) == '2026-11-17'
    if scope == 'company':
        assert result.plan.employee_name == 'Example Employee'


def test_unrelated_old_question_does_not_ground_an_invented_employee():
    service = agent({'kind': 'employee', 'employee_name': 'Example Employee'})
    result = asyncio.run(_interpret_once(service, '17/11/2026',
        {'kind': 'nl_query', 'question': 'Show Example Employee attendance.', 'plan': None},
        IDENTITY, CATALOGUE, None))
    assert result.action == 'clarify' and result.plan is None


@pytest.mark.parametrize('effort', ['none', 'low'])
def test_luna_sampling_is_only_enabled_without_reasoning(effort):
    service = agent({'kind': 'self'}, effort)
    asyncio.run(_interpret_once(service, 'Show my attendance on 17/11/2026', {}, IDENTITY, CATALOGUE, None))
    kwargs = service.create_response.call_args.kwargs
    if effort == 'none':
        assert kwargs['temperature'] == 0
    else:
        assert 'temperature' not in kwargs
