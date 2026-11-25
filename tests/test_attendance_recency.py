import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.auth import Identity
from app.query.interpreter import attendance_recency, interpret
from app.query.models import (
    DATED_SPECIALIZED_REPORTS, OPTIONALLY_DATED_SPECIALIZED_REPORTS,
    SpecializedPlan, compact_interpretation_schema,
)
from tests.query_support import backend, enable, headers
from tests.test_prompt_schema import expanded


ACTOR = Identity(1, 101, 'Fictional User', 'Admin', ('self', 'company'))
CATALOGUE = {'datasets': {'attendance': {
    'fields': ['date', 'employee_name', 'is_present', 'is_late', 'status'], 'metrics': ['employee_days'],
}}}
PRIOR = {'kind': 'nl_query', 'plan': {'dataset': 'attendance', 'mode': 'aggregate', 'scope': 'company',
    'start_date': '2026-11-01', 'end_date': '2026-11-24', 'metrics': ['employee_days'],
    'group_by': ['employee_name'], 'filters': [{'field': 'is_late', 'operator': '=', 'value': 1}]}}


def agent(selection):
    envelope = {'subject': {'kind': 'self'}, 'selection': selection}
    response = NS(status='completed', output=[NS(type='function_call', name='interpret_query', arguments=json.dumps(envelope))])
    return NS(settings=NS(openai_model='test-model'), create_response=AsyncMock(return_value=response))


@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_exact_question_is_saved_as_useful_clarification(backend, platform):
    client, state = backend
    model = enable(client, state, [])
    result = client.post('/v1/chat', headers=headers(), json={
        'message': 'Hello, can you see my last attendance day?', 'client_platform': platform})
    assert result.status_code == 200
    data = result.json()['data']
    assert data['status'] == 'clarify' and 'most recent day with a punch-in' in data['reply']
    assert data['tables'] == [] and state['query_bodies'] == []
    model.assert_not_awaited()
    history = client.get('/v1/conversations/' + data['conversation_id'], headers=headers())
    assert history.status_code == 200
    stored = history.json()['data']['messages'][-1]
    assert stored['status'] == 'clarify' and stored['content'] == data['reply']
    assert stored['tables'] == []


def test_existing_late_context_cannot_route_ambiguous_recency_to_specialized_report():
    service = agent({'action': 'specialized', 'specialized': {'kind': 'late_punchin'}})
    result = asyncio.run(interpret(service, 'Hello, can you see my last attendance day?', PRIOR, ACTOR, CATALOGUE))
    assert result.action == 'clarify' and result.clarification == 'attendance_recency'
    service.create_response.assert_not_awaited()


@pytest.mark.parametrize('message', [
    'Show my attendance last month', 'Who was present last week?',
    'Show my attendance for the last 3 days', 'Show attendance last Friday',
])
def test_relative_calendar_periods_are_not_latest_record_requests(message):
    assert not attendance_recency(message)


@pytest.mark.parametrize('message', [
    'When was I last present?', 'Show my latest attendance',
    'My most recent punch-in day', 'When did I last clock in?',
])
def test_event_recency_is_recognized_without_exact_question_matching(message):
    assert attendance_recency(message)


@pytest.mark.parametrize('bad', ['missing_presence', 'stale_late', 'employee_pagination'])
def test_latest_present_requires_matching_filter_and_date_pagination(bad):
    plan = {'dataset': 'attendance', 'scope': 'self', 'mode': 'aggregate',
        'start_date': '2026-11-01', 'end_date': '2026-11-24', 'metrics': ['employee_days'],
        'group_by': ['date'], 'filters': [{'field': 'is_present', 'operator': '=', 'value': 1}],
        'order_by': [{'field': 'date', 'direction': 'desc'}], 'limit': 1}
    if bad == 'missing_presence':
        plan['filters'] = []
    elif bad == 'stale_late':
        plan['filters'].append({'field': 'is_late', 'operator': '=', 'value': 1})
    else:
        plan.update(mode='details', fields=['date'], metrics=[], group_by=[])
    service = agent({'action': 'query', 'plan': plan})
    result = asyncio.run(interpret(service, 'Show my latest present day this month', PRIOR, ACTOR, CATALOGUE))
    assert result.action == 'clarify' and result.plan is None
    payload = json.loads(service.create_response.call_args.kwargs['input'][0]['content'])
    assert payload['previous_selection'] == {}


def test_clear_bounded_latest_present_request_keeps_only_requested_matching_condition():
    plan = {'dataset': 'attendance', 'mode': 'aggregate', 'scope': 'self',
        'start_date': '2026-11-01', 'end_date': '2026-11-24', 'metrics': ['employee_days'],
        'group_by': ['date'], 'filters': [{'field': 'is_present', 'operator': '=', 'value': 1}],
        'order_by': [{'field': 'date', 'direction': 'desc'}], 'limit': 1}
    result = asyncio.run(interpret(agent({'action': 'query', 'plan': plan}),
        'Show my most recent punch-in this month', PRIOR, ACTOR, CATALOGUE))
    assert result.action == 'query' and result.plan.filters[0].field == 'is_present'
    assert result.plan.scope == 'self' and result.plan.limit == 1


@pytest.mark.parametrize('kind', DATED_SPECIALIZED_REPORTS)
@pytest.mark.parametrize('dates', [{}, {'start_date': '2026-11-01'},
    {'start_date': '2026-11-20', 'end_date': '2026-11-01'},
    {'start_date': '2026-01-01', 'end_date': '2026-11-01'}])
def test_invalid_specialized_periods_are_rejected_before_backend_access(kind, dates):
    with pytest.raises(ValidationError, match='Specialized dated reports'):
        SpecializedPlan(kind=kind, **dates)


@pytest.mark.parametrize('kind', DATED_SPECIALIZED_REPORTS + OPTIONALLY_DATED_SPECIALIZED_REPORTS)
def test_supported_specialized_period_boundary(kind):
    start = date(2026, 1, 1)
    maximum = 31 if kind == 'work_summary' else 93
    SpecializedPlan(kind=kind, start_date=start, end_date=start + timedelta(days=maximum - 1))
    with pytest.raises(ValidationError):
        SpecializedPlan(kind=kind, start_date=start, end_date=start + timedelta(days=maximum))


@pytest.mark.parametrize('kind', ['leave_balance', 'work_arrangements', 'last_location', 'lms_location'])
def test_supported_undated_reports_still_work(kind):
    SpecializedPlan(kind=kind)


def test_provider_date_branches_match_runtime_required_and_optional_periods():
    schema = expanded(compact_interpretation_schema({}))
    branch = schema['properties']['selection']['anyOf'][2]
    options = branch['properties']['specialized']['anyOf']
    for option in options:
        props = option['properties']
        assert props['start_date']['type'] == props['end_date']['type']
        if props['start_date']['type'] == 'null':
            assert not set(props['kind']['enum']) & set(DATED_SPECIALIZED_REPORTS)


def test_undated_specialized_selection_requests_period_instead_of_backend_rejection():
    service = agent({'action': 'specialized', 'specialized': {'kind': 'late_punchin'}})
    result = asyncio.run(interpret(service, 'Show my late arrivals', {}, ACTOR, CATALOGUE))
    assert result.action == 'clarify' and result.clarification == 'period'
    assert service.create_response.await_count == 2
