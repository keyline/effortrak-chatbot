from copy import deepcopy

import pytest
from fastapi import HTTPException

from app.query.contracts import validate_result
from app.query.exports import fingerprint
from app.query.models import QueryPlan
from app.query.presentation import present


def detail():
    return dict(dataset='attendance', scope='company', period={'start':'2026-11-01','end':'2026-11-02'},
        columns=[dict(key='employee_name',label='Employee',type='string'), dict(key='date',label='Date',type='string')],
        rows=[{'employee_name':'Same Name','date':'2026-11-01'}, {'employee_name':'Same Name','date':'2026-11-02'},
              {'employee_name':'Same Name','date':'2026-11-01'}],
        row_employee_ids=[101,101,102], total_rows=40, offset=0, has_more=True)


def test_employee_groups_use_ids_and_preserve_only_loaded_page_rows():
    data = detail()
    before = deepcopy(data)
    table = present(data, {}).tables[0]
    assert table['employee_groups'] == [
        {'employee_id':101,'name':'Same Name','row_indices':[0,1]},
        {'employee_id':102,'name':'Same Name','row_indices':[2]}]
    assert table['rows'] == data['rows'] and table['has_more']
    assert data == before


@pytest.mark.parametrize('ids', [None, [101,102,103], [101,None,101], [101,True,101]])
def test_summary_and_legacy_or_incomplete_metadata_keep_flat_table(ids):
    data = detail()
    data['row_employee_ids'] = ids
    assert 'employee_groups' not in present(data, {}).tables[0]


def test_export_fingerprint_detects_changed_employee_even_with_identical_names():
    data = detail()
    before = fingerprint(data)
    data['row_employee_ids'][2] = 103
    assert fingerprint(data) != before


def test_valid_employee_metadata_passes_the_report_contract():
    data = detail()
    plan = QueryPlan(dataset='attendance', scope='company', start_date='2026-11-01', end_date='2026-11-02', fields=['employee_name','date'])
    data['plan'] = plan.model_dump(mode='json')
    assert validate_result(plan, data, {}).scope == 'company'


@pytest.mark.parametrize('ids', [[101], [101,True,102], [101,-1,102]])
def test_contract_rejects_bad_employee_metadata(ids):
    data = detail()
    plan = QueryPlan(dataset='attendance', scope='company', start_date='2026-11-01', end_date='2026-11-02', fields=['employee_name','date'])
    data['plan'] = plan.model_dump(mode='json')
    data['row_employee_ids'] = ids
    with pytest.raises(HTTPException) as exc:
        validate_result(plan, data, {})
    assert exc.value.status_code == 502


def employee_page(limit=50, offset=0):
    data = detail()
    data['period'] = {'start':'2026-10-17','end':'2026-11-19'}
    data['columns'].append(dict(key='attendance_hours',label='Attendance Hours',type='number',unit='hours'))
    ids = [101,102,103][offset:offset+limit]
    data['rows'] = [dict(employee_name='Same Name',date=f'day {day}',attendance_hours=1) for employee in ids for day in range(34)]
    data['row_employee_ids'] = [employee for employee in ids for day in range(34)]
    data.update(total_rows=102,offset=offset,has_more=offset+len(ids)<3,
        employee_pagination=dict(total=3,count=len(ids),offset=offset,limit=limit))
    plan = QueryPlan(dataset='attendance',scope='company',start_date='2026-10-17',end_date='2026-11-19',
        fields=['employee_name','date','attendance_hours'],limit=limit,offset=offset)
    data['plan'] = plan.model_dump(mode='json')
    return plan, data


def test_complete_employee_page_is_not_cut_at_fifty_records_and_header_is_two_lines():
    plan, data = employee_page()
    validate_result(plan,data,{})
    reply = present(data,{})
    assert len(reply.tables[0]['rows']) == 102
    assert [len(group['row_indices']) for group in reply.tables[0]['employee_groups']] == [34,34,34]
    assert len(reply.splitlines()) == 2
    assert '3 employees · 102 records' in reply
    assert 'Hours may' not in reply and 'Total attendance hours' not in reply
    assert 'Total attendance hours: 102h 00m' in reply.tables[0]['summary']
    assert not reply.tables[0]['has_more']


def test_employee_page_followup_keeps_full_employee_and_export_stays_row_based():
    for limit,offset in [(2,0),(2,2),(2,4)]:
        plan,data = employee_page(limit,offset)
        validate_result(plan,data,{})
        if data['rows']:
            assert len(data['rows']) == data['employee_pagination']['count']*34
        with pytest.raises(HTTPException):
            validate_result(plan,data,{},exporting=True)


@pytest.mark.parametrize('change',[{'count':2},{'offset':1},{'limit':1},{'total':2}])
def test_invalid_employee_page_cannot_bypass_row_limits(change):
    plan,data = employee_page()
    data['employee_pagination'].update(change)
    with pytest.raises(HTTPException):
        validate_result(plan,data,{})
