from copy import deepcopy

import pytest

from app.query.presentation import REPORT_TITLES, period_text, present


def report(dataset='effort'):
    return {'dataset': dataset, 'scope':'company',
        'period': {'start':'2026-11-11','end':'2026-11-17'},
        'project': {'project_id':7,'project_name':'Example Project'},
        'columns': [
            {'key':'employee_name','label':'Employee','type':'string'},
            {'key':'booked_hours','label':'Booked Hours','type':'number','unit':'hours'}],
        'rows': [{'employee_name':'Alice','booked_hours':3.5}, {'employee_name':'Bob','booked_hours':2.25}],
        'total_rows':2,'offset':0,'has_more':False,
        'notice':'Long internal report explanation that belongs in the exported report.'}


@pytest.mark.parametrize('dataset', REPORT_TITLES)
def test_all_report_types_use_clear_introductions_without_routine_boilerplate(dataset):
    data = report(dataset)
    before = deepcopy(data)
    reply = present(data, {}, 'bar')
    assert str(reply).startswith(REPORT_TITLES[dataset])
    assert 'for Example Project from 11 to 17 November 2026.' in reply
    assert 'Total booked hours: 5h 45m' in reply
    assert data['notice'] not in reply and 'result rows' not in reply
    assert '2026-11-11' not in reply and ' | ' not in reply
    assert reply.tables[0]['title'] == REPORT_TITLES[dataset]
    assert reply.tables[0]['rows'] == data['rows']
    assert reply.charts[0]['series'][0]['values'] == [3.5,2.25]
    assert reply.charts[0]['note'] == ''
    assert data == before


def test_empty_reports_give_help_without_empty_tables_or_chart_errors():
    data = report()
    data.update(rows=[], total_rows=0)
    reply = present(data, {}, 'pie')
    assert reply.startswith("I couldn't find any matching booked effort records")
    assert 'different date range or selection' in reply
    assert reply.tables == [] and reply.charts == []
    assert 'Showing' not in reply and 'chart' not in reply


def test_partial_and_missing_results_keep_their_important_qualifiers():
    data = report()
    data.update(offset=20, has_more=True, total_rows=23, total_is_exact=False)
    data['rows'][1]['booked_hours'] = None
    reply = present(data, {})
    assert 'Showing results 21 to 22 of at least 23.' in reply
    assert 'Ask for the next page' in reply
    assert 'Total booked hours (this page): Unavailable' in reply
    assert "Some values aren't available" in reply
    assert reply.tables[0]['rows'][1]['booked_hours'] is None


def test_own_records_and_material_attendance_caveat_are_clear():
    data = report('attendance')
    data['scope'] = 'self'
    data['columns'][1]['key'] = 'attendance_hours'
    for row in data['rows']:
        row['attendance_hours'] = row.pop('booked_hours')
    reply = present(data, {})
    assert reply.startswith("Your attendance")
    assert 'Hours may be incomplete when a punch-out is missing.' in reply


def test_date_ranges_are_readable_without_assuming_a_missing_period():
    assert period_text({'start':'2026-11-18','end':'2026-11-18'}) == ' on 18 November 2026'
    assert period_text({'start':'2026-10-29','end':'2026-11-02'}) == ' from 29 October 2026 to 2 November 2026'
    assert period_text({}) == ''
