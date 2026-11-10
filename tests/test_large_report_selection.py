import asyncio
import copy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.attendance_report import format_daily_report, format_monthly_report
from app.manager_query import render_report
from app.report_charts import build_report_chart
from app.semantic_query import period_question, selection_question
from app.task_report import task_result
from app.work_report import WorkData, format_work_report
from tests.test_work_report import payload
from tests.test_semantic_query import request


@pytest.mark.parametrize('count', [51, 125, 501])
def test_complete_attendance_totals_with_bounded_detail(count):
    rows = [dict(employee_id=i + 1, employee_name=f'Person {i:04}', status='Present',
                 worked_minutes=480, punch_in='09:00:00', punch_out='17:00:00') for i in range(count)]
    data = dict(date='2026-09-01', month='2026-09', scope='company', employee_ids=list(range(1, count + 1)),
                rows=rows, employee_count=count, summary={'Present': count}, worked_minutes=count * 480,
                unclassified_employee_days=0, period=dict(start='2026-09-01', end='2026-09-30', as_of='2026-10-01'),
                employee_summaries=[dict(employee_id=i + 1, status_counts={'Present': 1}, unclassified_days=0,
                                         worked_minutes=480) for i in range(count)])
    original = copy.deepcopy(data)
    for renderer in (format_daily_report, format_monthly_report):
        first = renderer(data)
        last = renderer(data, page=(count + 49) // 50)
        assert f'Employees included: {count}' in first
        assert f'Person {count - 1:04}' in last
        assert 'Person 0050' not in first
        assert 'Report page: 1 of ' in first
    assert data == original


def test_task_monthly_page_totals_include_all_employees():
    data = dict(scope='company', month='2026-09', employee_ids=list(range(1, 126)), rows=[
        dict(employee_id=i, work_status_id=1, task_count=2, planned_minutes=60) for i in range(1, 126)])
    result = task_result(data, daily=False, page=3)
    assert 'Tasks: 250 | Total planned time: 125h 0m' in result.local_reply
    assert 'Employee #125 ' in result.local_reply
    assert 'Employee #1 ' not in result.local_reply
    assert len(result.model_payload['rows']) == 25
    assert len(result.chart_source['rows']) == 125


@pytest.mark.parametrize('action', ['booked_time', 'attendance', 'tasks', 'work_summary', 'approved_leave', 'locations'])
def test_local_page_followup_keeps_subject_and_period_and_period_change_resets(action):
    context = dict(kind='semantic_question', selection={**request(action=action, subject='company',
        page=2, start_date='2026-09-01', end_date='2026-09-30'), 'scope': 'company'})
    question, _ = selection_question('next page', context)
    assert question.page == 3 and question.subject == 'previous' and question.use_previous
    question, _ = period_question('last month', context)
    assert question.page == 1


@pytest.mark.parametrize('metric,kind', [('working_hours', 'bar'), ('working_hours', 'line'), ('activity', 'line')])
def test_large_work_report_and_chart_include_complete_authorized_selection(metric, kind):
    raw = payload(dict(scope='company', start_date='2026-09-01', end_date='2026-09-01', detailed_review=False, include_daily=True))
    base = raw['rows'][0]
    raw['rows'] = [{**copy.deepcopy(base), 'employee_id': i, 'employee_name': f'Person {i:04}'} for i in range(1, 126)]
    raw['employee_ids'] = list(range(1, 126))
    parsed = WorkData.model_validate_json(__import__('json').dumps(raw), strict=True)
    result = format_work_report(parsed, page=3)
    assert 'Person 0125' in result.local_reply and 'Person 0001' not in result.local_reply
    assert len(result.chart_source.rows) == 125
    backend = SimpleNamespace(work_report=AsyncMock(return_value=result))
    selected = dict(scope='company', chart_metric=metric, chart_type=kind, start_date='2026-09-01', end_date='2026-09-01')
    _, charts, _ = asyncio.run(build_report_chart(SimpleNamespace(backend=backend), SimpleNamespace(), selected))
    chart = charts[0]
    values = chart['series'][0]['values']
    if metric == 'activity':
        assert values == [75]
    else:
        assert sum(values) == pytest.approx(125 * 505 / 60, abs=.01)


def test_large_booked_report_full_totals_and_last_page():
    rows = [dict(employee_id=i, employee_name=f'Person {i:04}', booked_minutes=60, entry_count=1,
                 unavailable_count=0, billing={key:dict(minutes=60 if key == 'billable' else 0,
                 entry_count=1 if key == 'billable' else 0) for key in ('billable', 'non_billable', 'unclassified')})
            for i in range(1, 126)]
    data = dict(report='booked_time', rows=rows, start_date='2026-09-01', end_date='2026-09-30',
                details_truncated=False, notice='Recorded bookings only.')
    answer = render_report(data, page=3)
    assert 'Total booked: 125h 0m; timesheet entries: 125' in answer
    assert 'Person 0125' in answer and 'Person 0001' not in answer
    assert len(data['rows']) == 125
