from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from app.query.models import Interpretation, QueryPlan
from app.query.refinements import apply_refinement
from app.query.interpreter import INSTRUCTIONS
from app.query.prompt_schema import interpretation_prompt_context
from tests.query_support import backend as backend_fixture, enable, headers

backend = backend_fixture


def prior():
    return QueryPlan(dataset='attendance', mode='aggregate', scope='company',
        start_date='2026-10-01', end_date='2026-10-31', group_by=['employee_id','employee_name'],
        metrics=['employee_days'], filters=[{'field':'is_late','operator':'=','value':1}],
        having=[{'field':'employee_days','operator':'>','value':1}],
        order_by=[{'field':'employee_days','direction':'desc'}]).model_dump(mode='json')


def test_date_columns_keep_period_threshold_at_its_original_grain_and_preserve_the_selection():
    original = prior()
    original['employee_id'] = 101
    before = deepcopy(original)
    result = apply_refinement(Interpretation(action='refine', refinement={
        'operation':'columns','columns':['date','status']}), {'kind':'nl_query','plan':original})
    plan = result.plan.model_dump(mode='json')
    assert plan['having_group_by'] == original['group_by']
    assert plan['group_by'] == ['employee_id','employee_name','date','status']
    for key in ('having','filters','metrics','scope','employee_id','start_date','end_date','order_by'):
        assert plan[key] == original[key]
    second = apply_refinement(Interpretation(action='refine', refinement={
        'operation':'columns','columns':['punch_in']}), {'kind':'nl_query','plan':plan})
    assert second.plan.having_group_by == original['group_by']
    assert original == before


def test_unquantified_frequency_and_ordinary_reports_need_no_cohort_threshold():
    original = prior()
    original['having'] = []
    result = apply_refinement(Interpretation(action='refine', refinement={
        'operation':'columns','columns':['date']}), {'kind':'nl_query','plan':original})
    assert result.plan.having == [] and result.plan.having_group_by is None
    assert 'Unquantified frequency must always have having=[]' in INSTRUCTIONS
    assert 'Only explicitly requested numeric aggregate thresholds' in QueryPlan.model_fields['having'].description


def test_explicit_aggregation_resets_the_old_cohort_threshold():
    original = prior()
    original.update(group_by=['employee_id','employee_name','date'], having_group_by=['employee_id','employee_name'])
    result = apply_refinement(Interpretation(action='refine', refinement={
        'operation':'aggregate','metrics':['employee_days'],'group_by':['date']}), {'kind':'nl_query','plan':original})
    assert result.plan.having == [] and result.plan.having_group_by is None


def test_prompt_keeps_an_explicit_whole_selection_threshold_grain():
    plan = {**prior(), 'having_group_by': []}
    assert interpretation_prompt_context({'kind':'nl_query','plan':plan})['plan']['having_group_by'] == []


@pytest.mark.parametrize('change', [
    {'having_group_by':['department']}, {'having_group_by':['employee_id','employee_id']},
    {'having_group_by':['employee_id'],'having':[]},
    {'having_group_by':['employee_id'],'dataset':'effort'},
    {'having_group_by':['employee_id'],'mode':'details','fields':['employee_name'],'metrics':[],'group_by':[],'having':[]},
])
def test_invalid_cohort_grains_are_rejected_before_execution(change):
    with pytest.raises(ValidationError):
        QueryPlan.model_validate({**prior(), **change})


@pytest.mark.parametrize('platform', ['web','android','ios'])
def test_chat_followup_uses_real_php_cohort_execution_with_fictional_records(backend, platform):
    php = shutil.which('php') or 'D:/xampp/php/php.exe'
    web = Path(__file__).resolve().parents[2] / 'effortrak_web'
    if not Path(php).is_file() or not (web / 'tests/bootstrap.php').is_file():
        pytest.skip('Cross-service check requires the sibling PHP backend and PHP CLI')
    client, state = backend
    state['scopes'] = ['self','company']
    original = prior()
    change = {'action':'refine', 'refinement':{'operation':'columns','columns':['date']}}
    followup = apply_refinement(Interpretation.model_validate(change), {'kind':'nl_query','plan':original}).plan.model_dump(mode='json')
    model = enable(client, state, [{'action':'query','plan':original}, change])
    client.app.state.chat.catalogue = {'datasets':{'attendance':{
        'fields':['employee_id','employee_name','date','is_late'], 'metrics':['employee_days']}}}
    rows = [
        {'employee_id':101,'employee_name':'Example A','date':'2026-10-01','is_late':1},
        {'employee_id':101,'employee_name':'Example A','date':'2026-10-02','is_late':1},
        {'employee_id':102,'employee_name':'Example B','date':'2026-10-01','is_late':1},
    ]
    php_code = r'''require 'tests/bootstrap.php';
        $data=json_decode(stream_get_contents(STDIN),true,512,JSON_THROW_ON_ERROR);
        $plan=App\Services\Ai\Query\Plan::validate($data['plan']);
        echo json_encode(App\Services\Ai\Query\CanonicalRows::execute($plan,$data['rows']),JSON_THROW_ON_ERROR);'''

    def result(plan):
        executed = subprocess.run([php, '-r', php_code], cwd=web,
            input=json.dumps({'plan':plan,'rows':rows}), text=True, capture_output=True, check=True)
        data = json.loads(executed.stdout)
        return {**data,'status':'answered','dataset':'attendance','scope':'company','plan':plan,
            'period':{'start':'2026-10-01','end':'2026-10-31'},'total_rows':len(data['rows']),
            'offset':0,'limit':50,'has_more':False,'notice':''}

    state['result'] = result(original)
    first = client.post('/v1/chat', headers=headers(), json={
        'message':'Show employees who arrived late more than once last month', 'client_platform':platform})
    assert first.status_code == 200, first.text
    state['result'] = result(followup)
    second = client.post('/v1/chat', headers=headers(), json={'message':'Include their dates too',
        'conversation_id':first.json()['data']['conversation_id'], 'client_platform':platform})
    assert second.status_code == 200, second.text
    answer = second.json()['data']
    assert [row['date'] for row in answer['tables'][0]['rows']] == ['2026-10-01','2026-10-02']
    assert {row['employee_name'] for row in answer['tables'][0]['rows']} == {'Example A'}
    assert 'couldn\'t find' not in answer['reply']
    assert 'Employees are ranked' not in answer['reply']
    assert state['query_bodies'][-1]['plan']['having_group_by'] == ['employee_id','employee_name']
    assert state['query_bodies'][-1]['plan']['scope'] == 'company'
    assert model.await_count == 2
