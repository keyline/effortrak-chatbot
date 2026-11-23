from copy import deepcopy
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.query.exports import workbook
from app.query.interpreter import INSTRUCTIONS
from app.query.models import QueryPlan
from app.query.presentation import present
from tests.query_support import backend as backend_fixture, enable, headers

backend = backend_fixture
FIELDS = ['employee_name', 'date', 'status', 'attendance_hours', 'punch_in', 'punch_out']


def detail():
    plan = QueryPlan(dataset='attendance', scope='company', start_date='2026-11-17', end_date='2026-11-17')
    resolved = plan.model_copy(update={'fields': FIELDS})
    rows = []
    for name, status, punch_in, punch_out in [
        ('Employee A', 'Present', '09:00:00', '18:00:00'),
        ('Employee B', 'Late', '10:00:00', None),
        ('Employee C', 'Absent', None, None),
        ('Employee D', 'Paid Leave', None, None),
        ('Employee E', 'Holiday', None, None),
        ('Employee F', 'Week Off', None, None),
    ]:
        rows.append(dict(employee_name=name, date='2026-11-17', status=status,
                         attendance_hours=9 if punch_out else 0, punch_in=punch_in, punch_out=punch_out))
    data = dict(status='answered', dataset='attendance', scope='company', plan=resolved.model_dump(mode='json'),
        period={'start':'2026-11-17','end':'2026-11-17'},
        columns=[dict(key=field,label=field.replace('_',' ').title(),type='number' if field=='attendance_hours' else 'string',
                      unit='hours' if field=='attendance_hours' else '') for field in FIELDS],
        rows=rows,total_rows=6,offset=0,has_more=False)
    return plan, data


@pytest.mark.parametrize('platform',['web','android','ios'])
def test_new_attendance_report_does_not_reuse_a_previous_late_day_count(backend, platform):
    client, state = backend
    plan, data = detail()
    prior = QueryPlan(dataset='attendance',mode='aggregate',scope='company',
        start_date='2026-11-01',end_date='2026-11-21',group_by=['employee_name'],metrics=['employee_days'],
        filters=[{'field':'is_late','operator':'=','value':1}])
    previous = dict(status='answered',dataset='attendance',scope='company',plan=prior.model_dump(mode='json'),
        period={'start':'2026-11-01','end':'2026-11-21'},
        columns=[dict(key='employee_name',label='Employee',type='string'),dict(key='employee_days',label='Days',type='number',unit='employee-days')],
        rows=[dict(employee_name='Employee B',employee_days=3)],total_rows=1,offset=0,has_more=False)
    state.update(scopes=['self','company'],result=previous)
    client.app.state.chat.catalogue = {'datasets':{'attendance':{
        'fields': FIELDS+['is_late','is_present'], 'metrics':['employee_days','present_days'], 'default_fields':FIELDS}}}
    model = enable(client,state,[{'action':'query','plan':prior.model_dump(mode='json')},
                                {'action':'query','plan':plan.model_dump(mode='json')}])
    first = client.post('/v1/chat',headers=headers(),json={
        'message':'Who arrived late this month?', 'client_platform':platform})
    assert first.status_code == 200, first.text
    state['result'] = data
    second = client.post('/v1/chat',headers=headers(),json={
        'message':'Show the attendance report for 17/11/2026 with punch-in and punch-out times',
        'conversation_id':first.json()['data']['conversation_id'],'client_platform':platform})
    assert second.status_code == 200, second.text
    table = second.json()['data']['tables'][0]
    assert [col['key'] for col in table['columns']] == FIELDS
    assert table['rows'][2]['status'] == 'Absent' and table['rows'][2]['punch_in'] is None
    assert table['rows'][1]['status'] == 'Late' and table['rows'][1]['punch_out'] is None
    assert [row['status'] for row in table['rows'][3:]] == ['Paid Leave','Holiday','Week Off']
    assert state['query_bodies'][-1]['plan']['mode'] == 'details'
    assert state['query_bodies'][-1]['plan']['filters'] == []
    assert model.await_count == 2


def test_detail_exports_preserve_clear_absence_and_nonworking_statuses():
    _, data = detail()
    before = deepcopy(data)
    sheet = load_workbook(BytesIO(workbook(data,[])))['Report']
    assert sheet['C7'].value == 'Status'
    assert sheet['C10'].value == 'Absent' and sheet['E10'].value is None
    assert sheet['C11'].value == 'Paid Leave'
    assert data == before


def test_calendar_counts_cannot_be_confused_with_confirmed_presence():
    plan, data = detail()
    data['plan'] = plan.model_copy(update={'mode':'aggregate','group_by':['employee_name','date'],
                                          'metrics':['employee_days']}).model_dump(mode='json')
    data['columns'] = [col for col in data['columns'] if col['key'] in ['employee_name','date']]+[
        dict(key='employee_days',label='Employee Days',type='number',unit='employee-days')]
    data['rows'] = [dict(employee_name='Employee C',date='2026-11-17',employee_days=1)]
    reply = present(data,{})
    assert reply.tables[0]['columns'][-1]['label'] == 'Days covered'
    data['columns'] = [dict(key='present_days',label='Present Days',type='number',unit='employee-days')]
    data['rows'] = [dict(present_days=2)]
    reply = present(data,{})
    assert 'Days present: 2' in reply
    assert 'plain attendance report is mode=details' in INSTRUCTIONS
    assert 'Never treat employee_days=1 as proof of attendance' in INSTRUCTIONS
