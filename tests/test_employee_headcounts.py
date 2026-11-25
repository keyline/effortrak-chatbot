import asyncio
import json
from copy import deepcopy
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.query.exports import workbook
from app.query.interpreter import _interpret_once, employee_headcount
from app.query.models import Interpretation, QueryPlan
from app.query.presentation import present
from tests.query_support import backend, enable, headers
from tests.test_attendance_recency import ACTOR, PRIOR, agent


CATALOGUE = {'datasets': {'attendance': {
    'fields': ['employee_name', 'date', 'is_present', 'is_late', 'status', 'department'],
    'metrics': ['employee_count', 'employee_days', 'present_days', 'attendance_hours'],
}}}


def selection(metric='employee_days', **changes):
    return Interpretation(action='query', plan=QueryPlan(
        dataset='attendance', mode='aggregate', scope='company',
        start_date='2026-11-02', end_date='2026-11-02', metrics=[metric],
        group_by=['employee_name'], filters=[{'field': 'is_late', 'operator': '=', 'value': 1}],
        **changes))


def count_report(count=77, cohort='late', end='2026-11-02'):
    predicates = {'late': [('is_late', 1)], 'present': [('is_present', 1)],
                  'absent': [('is_present', 0), ('status', 'Absent')]}.get(cohort, [])
    plan = selection().plan.model_dump(mode='json')
    plan.update(metrics=['employee_count'], group_by=[], end_date=end, filters=[])
    plan['filters'] = [{'field': field, 'operator': '=', 'value': value} for field, value in predicates]
    return {'status': 'answered', 'dataset': 'attendance', 'scope': 'company', 'plan': plan,
        'period': {'start': '2026-11-02', 'end': end},
        'columns': [{'key': 'employee_count', 'label': 'Employee Count', 'type': 'number', 'unit': 'employees'}],
        'rows': [{'employee_count': count}], 'total_rows': 1, 'offset': 0, 'has_more': False}


@pytest.mark.parametrize('question', [
    'Can you tell how much employees were late on 02/11/2026?',
    'How many employees were late on 02/11/2026?',
    'Number of employees who arrived late on 02/11/2026',
    'Count the employees who were late on 02/11/2026',
    'Give me the employee count for 02/11/2026',
    'How many late employees on 02/11/2026?',
    'Total employees late on 02/11/2026',
    'How many staff members were late on 02/11/2026?',
    'Count unique people who arrived late on 02/11/2026',
])
def test_headcount_replaces_inherited_employee_list_without_changing_cohort(question):
    original = selection(offset=50)
    before = original.model_dump()
    result = employee_headcount(original, question, CATALOGUE)
    assert result.plan.metrics == ['employee_count']
    assert result.plan.group_by == result.plan.fields == result.plan.order_by == []
    assert result.plan.offset == 0 and result.plan.scope == 'company'
    assert result.plan.filters == original.plan.filters
    assert result.plan.start_date == original.plan.start_date
    assert original.model_dump() == before


@pytest.mark.parametrize('question', [
    'Who arrived late on 02/11/2026?', 'Show the employee list for 02/11/2026',
    'How many days was each employee late?', 'Count employees and show their names',
    'Show employee count along with total attendance hours',
    'Show employees who were consistently late this month',
])
def test_lists_day_counts_breakdowns_and_combined_requests_keep_their_shape(question):
    original = selection()
    assert employee_headcount(original, question, CATALOGUE) is original


@pytest.mark.parametrize(('question', 'groups'), [
    ('How many employees were late by department?', ['department']),
    ('How many employees were late per day?', ['date']),
])
def test_explicit_headcount_breakdowns_keep_dimensions_but_count_people(question, groups):
    original = selection()
    original.plan.group_by = groups
    result = employee_headcount(original, question, CATALOGUE)
    assert result.plan.metrics == ['employee_count'] and result.plan.group_by == groups


def test_present_headcount_counts_punched_in_employees_instead_of_calendar_rows():
    original = selection('present_days').model_copy(update={
        'plan': selection('present_days').plan.model_copy(update={'filters': []})})
    result = employee_headcount(original, 'How much employees were present on 02/11/2026?', CATALOGUE)
    assert result.plan.metrics == ['employee_count'] and result.plan.group_by == []
    assert [item.model_dump() for item in result.plan.filters] == [
        {'field': 'is_present', 'operator': '=', 'value': 1}]


def test_threshold_qualified_count_is_clarified_instead_of_dropping_threshold():
    original = selection(having=[{'field': 'employee_days', 'operator': '>=', 'value': 5}])
    result = employee_headcount(original, 'Count employees late at least 5 times this month', CATALOGUE)
    assert result.action == 'clarify' and result.plan is None
    assert original.plan.having[0].value == 5


def test_restricted_fields_are_denied_before_count_normalization_can_remove_them():
    catalogue = deepcopy(CATALOGUE)
    catalogue['datasets']['attendance']['fields'].append('reason')
    catalogue['datasets']['attendance']['restricted_fields'] = {'reason': ['SUPER ADMIN']}
    model = agent({'action': 'query', 'plan': selection().plan.model_copy(
        update={'group_by': ['reason']}).model_dump(mode='json')})
    envelope = json.loads(model.create_response.return_value.output[0].arguments)
    envelope['subject'] = {'kind': 'company'}
    model.create_response.return_value.output[0].arguments = json.dumps(envelope)
    result = asyncio.run(_interpret_once(model, 'Count employees with reasons', PRIOR, ACTOR, catalogue, None))
    assert result.action == 'denied'


@pytest.mark.parametrize(('cohort', 'count', 'sentence'), [
    ('present', 154, '154 employees were present'),
    ('late', 77, '77 employees arrived late'),
    ('absent', 1, '1 employee was absent'),
    ('late', 0, '0 employees arrived late'),
])
def test_scalar_attendance_counts_use_plain_sentences_without_duplicate_tables(cohort, count, sentence):
    data = count_report(count, cohort)
    before = deepcopy(data)
    result = present(data, {})
    assert str(result) == sentence + ' on 2 November 2026.'
    assert result.tables == [] and result.charts == []
    assert data == before


def test_multi_day_headcount_does_not_claim_daily_presence_and_exports_keep_correct_units():
    data = count_report(154, 'present', '2026-11-20')
    result = present(data, {})
    assert str(result) == '154 employees were present at least once from 2 to 20 November 2026.'
    sheet = load_workbook(BytesIO(workbook(data, [])))['Report']
    assert sheet['A7'].value == 'Employees present' and sheet['A8'].value == 154
    assert data['columns'][0]['label'] == 'Employee Count'


@pytest.mark.parametrize('platform', ['web', 'android', 'ios'])
@pytest.mark.parametrize('cohort', ['present', 'late'])
def test_chat_endpoint_corrects_model_list_or_day_metric_and_saves_natural_count(backend, platform, cohort):
    client, state = backend
    expected = count_report(154 if cohort == 'present' else 77, cohort)
    wrong = selection('present_days' if cohort == 'present' else 'employee_days')
    if cohort == 'present':
        wrong.plan.filters = []
    prior = selection().plan.model_dump(mode='json')
    prior['filters'] = [{'field': 'is_present', 'operator': '=', 'value': 1}]
    initial = {**expected, 'plan': prior, 'columns': [
        {'key': 'employee_name', 'label': 'Employee Name', 'type': 'string'},
        {'key': 'employee_days', 'label': 'Employee Days', 'type': 'number', 'unit': 'employee-days'}],
        'rows': [{'employee_name': 'Fictional Employee', 'employee_days': 1}]}
    state.update(scopes=['self', 'company'], result=initial)
    client.app.state.chat.catalogue = CATALOGUE
    next_selection = ({'action': 'refine', 'refinement': {'operation': 'filter',
                       'filters': expected['plan']['filters']}} if cohort == 'late' else
                      {'action': 'query', 'plan': wrong.plan.model_dump(mode='json')})
    enable(client, state, [{'action': 'query', 'plan': prior}, next_selection])
    first = client.post('/v1/chat', headers=headers(), json={
        'message': 'Show days present per employee on 02/11/2026', 'client_platform': platform})
    assert first.status_code == 200, first.text
    assert first.json()['data']['tables'][0]['rows'][0]['employee_days'] == 1
    state['result'] = expected
    response = client.post('/v1/chat', headers=headers(), json={
        'message': f'Can you tell how much employees were {cohort} on 02/11/2026?',
        'client_platform': platform, 'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['status'] == 'answered' and data['tables'] == []
    phrase = 'employees were present' if cohort == 'present' else 'employees arrived late'
    assert phrase in data['reply']
    plan = state['query_bodies'][-1]['plan']
    assert plan['metrics'] == ['employee_count'] and plan['group_by'] == []
    assert plan['filters'] == expected['plan']['filters']
    saved = client.get('/v1/conversations/' + data['conversation_id'], headers=headers())
    assert saved.status_code == 200
    assert saved.json()['data']['messages'][-1]['content'] == data['reply']
