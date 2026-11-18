import json

import pytest

from app.query.models import QueryPlan
from app.query.prompt_schema import interpretation_prompt_context
from app.query.refinements import choose_project
from app.reply import AssistantReply
from tests.query_support import backend as backend_fixture, enable, headers, request

backend = backend_fixture
CHOICES = [
    {'project_id': 7, 'project_name': 'Fictional Private Project A'},
    {'project_id': 9, 'project_name': 'Fictional Private Project B'},
]
PLAN = {
    'dataset': 'effort', 'mode': 'aggregate', 'scope': 'self', 'project_name': 'Fictional',
    'start_date': '2026-09-01', 'end_date': '2026-09-30', 'metrics': ['booked_hours'],
    'group_by': ['employee_name'], 'offset': 50,
    'filters': [
        {'field': 'project_name', 'operator': '=', 'value': 'Fictional'},
        {'field': 'billing', 'operator': '=', 'value': 'Billable'},
    ],
}


def pending():
    return {'kind': 'nl_query', 'plan': PLAN, 'project_choices': CHOICES,
            'project_choice_message': 'Choose a listed project.', 'chart': 'bar', 'export': 'both'}


@pytest.mark.parametrize('operator', ['=', 'contains'])
def test_duplicate_project_name_constraint_does_not_conflict_with_resolved_id(operator):
    plan = QueryPlan.model_validate({**PLAN, 'filters': [
        {**PLAN['filters'][0], 'operator': operator}, PLAN['filters'][1],
        {'field':'project_name', 'operator':'!=', 'value':'Excluded project'},
    ]})
    assert [item.model_dump() for item in plan.filters] == [
        PLAN['filters'][1], {'field':'project_name','operator':'!=','value':'Excluded project'},
    ]


@pytest.mark.parametrize('text', ['2', 'option 2', 'project ID: 9', 'id 9', 'Fictional Private Project B'])
def test_choice_preserves_report_and_removes_only_duplicate_name_filter(text):
    selection = choose_project(text, pending())
    plan = selection.plan
    assert plan.project_id == 9 and plan.project_name is None and plan.offset == 0
    assert plan.scope == 'self' and plan.start_date.isoformat() == '2026-09-01'
    assert plan.metrics == ['booked_hours'] and plan.group_by == ['employee_name']
    assert [item.model_dump() for item in plan.filters] == [PLAN['filters'][1]]
    assert selection.chart == 'bar' and selection.export == 'both'


def test_unknown_choice_does_not_execute_and_choices_never_enter_prompts():
    result = choose_project('project ID 999', pending())
    assert isinstance(result, AssistantReply) and result.status == 'clarify'
    assert result.query_context['project_choices'] == CHOICES
    assert choose_project('Show my attendance today', pending()) is None
    prompt = interpretation_prompt_context(pending())
    assert 'project_choices' not in prompt and 'project_choice_message' not in prompt
    assert 'Fictional Private Project' not in json.dumps(prompt)
    assert len(pending()['project_choices']) == 2


@pytest.mark.parametrize('platform,choice', [('web','2'), ('android','project ID: 9'), ('ios',CHOICES[1]['project_name'])])
def test_chat_project_choice_reauthorizes_and_exports_without_another_model_call(backend, platform, choice):
    client, state = backend
    original = client.app.state.backend.call
    queries = []
    async def call(endpoint, token, body=None):
        if endpoint == 'query-catalogue':
            return {'version': 1, 'datasets': {'effort': {
                'fields': ['employee_name', 'project_name', 'project_id', 'billing'],
                'metrics': ['booked_hours'], 'default_fields': ['employee_name'],
            }}}
        if endpoint == 'query':
            queries.append(body['plan'])
            if state.get('revoke_project'):
                from fastapi import HTTPException
                raise HTTPException(403, 'Module read permission is not granted for this scope.')
            if body['plan']['project_id'] is None:
                return {'status': 'clarify', 'message': 'Choose 1 or 2.', 'project_choices': CHOICES}
            assert body['plan']['project_id'] == 9
            assert body['plan']['filters'] == [PLAN['filters'][1]]
            return {
                'status': 'answered', 'dataset': 'effort', 'scope': 'self', 'plan': body['plan'],
                'columns': [{'key': 'employee_name', 'label': 'Employee', 'type': 'string'},
                            {'key': 'booked_hours', 'label': 'Booked hours', 'type': 'number', 'unit': 'hours'}],
                'rows': [{'employee_name': 'Fictional employee', 'booked_hours': 3}],
                'period': {'start': '2026-09-01', 'end': '2026-09-30'},
                'total_rows': 1, 'offset': 0, 'limit': 5000 if body.get('export') else 50, 'has_more': False,
            }
        return await original(endpoint, token, body)
    client.app.state.backend.call = call
    model = enable(client, state, [request(plan={**PLAN, 'offset': 0}, chart='bar', export='both')])
    response = client.post('/v1/chat', headers=headers(), json={
        'message': 'Show my booked hours on Fictional projects in September 2026, export PDF and Excel',
        'client_platform': platform,
    })
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['status'] == 'clarify' and data['tables'] == []
    conversation = data['conversation_id']
    # A restarted interpreter must reload schema metadata, without asking the model again.
    client.app.state.chat.catalogue = None
    invalid = client.post('/v1/chat', headers=headers(), json={'message': '999', 'conversation_id': conversation})
    assert invalid.json()['data']['status'] == 'clarify' and len(queries) == 1
    chosen = client.post('/v1/chat', headers=headers(), json={'message': choice, 'conversation_id': conversation})
    assert chosen.status_code == 200, chosen.text
    answer = chosen.json()['data']
    assert answer['tables'][0]['rows'][0]['booked_hours'] == 3
    assert len(answer['artifacts']) == 2 and len(answer['charts']) == 1
    assert model.await_count == 1
    state['revoke_project'] = True
    denied = client.get(answer['artifacts'][0]['download_url'], headers=headers())
    assert denied.status_code == 403
    assert model.await_count == 1
