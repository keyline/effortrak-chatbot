from copy import deepcopy
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.query.exports import fingerprint, pdf, workbook
from app.query.presentation import charts_for, present
from app.query.wording import report_view
from app.query.models import QueryPlan
from tests.query_support import backend as backend_fixture, enable, headers

backend = backend_fixture


def report():
    return {'dataset':'attendance','scope':'company',
        'period':{'start':'2026-10-01','end':'2026-10-31'},
        'plan':{'dataset':'attendance','mode':'aggregate','group_by':['employee_name'],
            'filters':[{'field':'is_late','operator':'=','value':1}], 'having':[],
            'order_by':[{'field':'employee_days','direction':'desc'}]},
        'columns':[{'key':'employee_name','label':'Employee Name','type':'string'},
            {'key':'employee_count','label':'Employee Count','type':'number','unit':'employees'},
            {'key':'employee_days','label':'Employee Days','type':'number','unit':'employee-days'}],
        'rows':[{'employee_name':'Example A','employee_count':1,'employee_days':17},
            {'employee_name':'Example B','employee_count':1,'employee_days':12}],
        'total_rows':2,'offset':0,'has_more':False,'notice':'Attendance rules apply.'}


def test_late_occurrences_have_clear_labels_and_no_redundant_headcount_or_chart_warning():
    data = report()
    original = deepcopy(data)
    reply = present(data,{},'bar')
    assert reply.startswith('Late arrivals from 1 to 31 October 2026.')
    assert 'ranked by the number of days they arrived late' in reply
    assert 'same unit' not in reply and 'consistently' not in reply
    assert [col['label'] for col in reply.tables[0]['columns']] == ['Employee Name','Days late']
    assert [row['employee_days'] for row in reply.tables[0]['rows']] == [17,12]
    assert 'employee_count' not in reply.tables[0]['rows'][0]
    assert reply.charts[0]['series'] == [{'name':'Days late','values':[17,12]}]
    assert reply.charts[0]['unit'] == 'employee-days'
    assert data == original


@pytest.mark.parametrize('filters,label,title',[
    ([{'field':'is_present','operator':'=','value':1}], 'Days present','Present employees'),
    ([{'field':'status','operator':'=','value':'Absent'}], 'Days absent','Absent employees'),
    ([{'field':'status','operator':'in','value':['Late']}], 'Days late','Late arrivals'),
    ([{'field':'is_late','operator':'!=','value':0}], 'Days late','Late arrivals'),
    ([{'field':'is_late','operator':'=','value':0}], 'Attendance days',None),
    ([{'field':'status','operator':'in','value':['Present','Late']}], 'Attendance days',None),
    ([], 'Attendance days',None),
])
def test_wording_follows_verified_filters_rather_than_question_phrasing(filters,label,title):
    data = report()
    data['plan']['filters'] = filters
    view = report_view(data)
    assert view['columns'][-1]['label'] == label
    assert view.get('display_title') == title


@pytest.mark.parametrize('change',['same_name_group','headcount_only','count_sort','count_having','department_group'])
def test_meaningful_headcounts_are_preserved(change):
    data = report()
    if change == 'same_name_group': data['rows'][0]['employee_count'] = 2
    elif change == 'headcount_only': data['columns'].pop()
    elif change == 'count_sort': data['plan']['order_by'] = [{'field':'employee_count','direction':'desc'}]
    elif change == 'count_having': data['plan']['having'] = [{'field':'employee_count','operator':'>','value':0}]
    elif change == 'department_group': data['plan']['group_by'] = ['department']
    assert any(col['key']=='employee_count' for col in report_view(data)['columns'])


def test_exports_use_same_business_labels_and_keep_original_receipt_and_threshold():
    data = report()
    data['plan']['having'] = [{'field':'employee_days','operator':'>=','value':10}]
    original = deepcopy(data)
    receipt = fingerprint(data)
    sheet = load_workbook(BytesIO(workbook(data,[])))['Report']
    assert sheet['A7'].value == 'Employee Name'
    assert sheet['B7'].value == 'Days late'
    assert sheet['B8'].value == 17 and sheet['B9'].value == 12
    assert pdf(data,[]).startswith(b'%PDF')
    assert data == original and fingerprint(data) == receipt


def test_no_chart_is_generated_for_a_table_selection_and_mixed_meaningful_units_stay_guarded():
    assert charts_for(report(),None) == ([], '')
    data = report()
    data['rows'][0]['employee_count'] = 2
    charts,notice = charts_for(data,'bar')
    assert charts == [] and 'same unit' in notice


def test_chat_endpoint_uses_the_shared_view_and_keeps_the_authorized_query(backend):
    client,state = backend
    data = report()
    plan = QueryPlan.model_validate({**data['plan'],'scope':'company','start_date':'2026-10-01','end_date':'2026-10-31',
        'metrics':['employee_count','employee_days']})
    data['plan'] = plan.model_dump(mode='json')
    data['status'] = 'answered'
    state.update(scopes=['self','company'],result=data)
    client.app.state.chat.catalogue = {'datasets':{'attendance':{
        'fields':['employee_name','is_late'], 'metrics':['employee_count','employee_days']}}}
    model = enable(client,state,[{'action':'query','plan':data['plan'],'chart':None}])
    response = client.post('/v1/chat',headers=headers(),json={'message':'Which employees had frequent late arrivals last month?'})
    assert response.status_code == 200, response.text
    answer = response.json()['data']
    assert answer['charts'] == []
    assert answer['tables'][0]['columns'][-1]['label'] == 'Days late'
    assert answer['tables'][0]['rows'][0] == {'employee_name':'Example A','employee_days':17}
    assert state['query_bodies'][0]['plan']['filters'] == data['plan']['filters']
    assert state['query_bodies'][0]['plan']['scope'] == 'company'
    assert model.await_count == 1
