import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.attendance_report import MONTHLY_OVERVIEW, format_monthly_report
from app.auth import Identity
from app.chat_service import ChatService
from app.config import Settings


def test_monthly_report_explains_employee_days_and_keeps_full_aggregates():
    data = dict(scope='company', month='2026-10',
                period=dict(start='2026-10-01', end='2026-10-31', as_of='2026-10-08'),
                employee_count=2, summary={'Present': 33, 'Late': 9},
                unclassified_employee_days=46, worked_minutes=945,
                employee_summaries=[dict(employee_id=101, status_counts={'Present': 20},
                                         unclassified_days=23, worked_minutes=480),
                                    dict(employee_id=102, status_counts={'Present': 13, 'Late': 9},
                                         unclassified_days=23, worked_minutes=465)],
                rows=[], details_truncated=True, notice='Not payroll payable days.')
    text = format_monthly_report(data)
    assert 'Employees included: 2' in text
    assert 'employee-days, not unique employees' in text
    assert 'Present: 33' in text
    assert 'Employee #102: Late: 9; Present: 13' in text
    assert 'Total worked time: 15h 45m' in text
    assert 'Unclassified days are not absences' in text
    assert '2026-10-31' in text
    data['rows'] = [dict(employee_id=101, employee_name='Test Employee'),
                    dict(employee_id=102, employee_name='Another\nEmployee <name>')]
    named = format_monthly_report(data)
    assert 'Employee #101 (Test Employee): Present: 20' in named
    assert 'Employee #102 (Another Employee <name>): Late: 9; Present: 13' in named
    assert 'Another\nEmployee' not in named


@pytest.mark.parametrize('message', ['Show company attendance for 2026-10',
                                    'Show my attendance for this month',
                                    'Give me team attendance summary for 2026-09.'])
def test_general_monthly_requests_get_consistent_report(message):
    assert MONTHLY_OVERVIEW.fullmatch(message)


@pytest.mark.parametrize('message', ['How many late days in 2026-10?',
                                    'Show company attendance for yesterday',
                                    'Export team attendance for 2026-10 as pdf'])
def test_targeted_daily_and_export_requests_are_not_overridden(message):
    assert not MONTHLY_OVERVIEW.fullmatch(message)


def test_model_summary_is_replaced_only_for_general_monthly_report():
    import asyncio

    service = ChatService(Settings(chat_mode='demo', data_mode='sample'))
    call = SimpleNamespace(type='function_call', name='get_attendance', call_id='one',
                           arguments='{"scope":"company","employee_id":null,"month":"2026-10"}')
    create = AsyncMock(side_effect=[SimpleNamespace(output=[call], output_text=''),
                                   SimpleNamespace(output=[], output_text='There are 33 employees present.')])
    service.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    service.execute_tool = AsyncMock(return_value=dict(
        scope='company', month='2026-10', period=dict(start='2026-10-01', end='2026-10-31', as_of='2026-10-08'),
        employee_count=2, summary={'Present': 33}, unclassified_employee_days=0,
        worked_minutes=480, employee_summaries=[], notice='Calendar status counts only.'))
    identity = Identity(1, 101, 'Test', 'Admin', ('self', 'company'), False)
    reply, tools, reports = asyncio.run(service.model_reply('Show company attendance for 2026-10', [], identity))
    assert 'Employees included: 2' in reply
    assert 'Present: 33' in reply
    assert '33 employees present' not in reply
    assert tools == ['get_attendance']
    assert reports == []
