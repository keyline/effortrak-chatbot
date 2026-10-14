import pytest

from app.task_report import task_result
from tests.test_backend import backend, headers


def test_project_batch_counts_employees_not_authorized_scope():
    data = dict(scope='company', date='all history (newest first)', employee_ids=[54] + list(range(1, 27)),
                project_id=7, project_name='Example', matching_employee_count=4,
                task_count=97, planned_minutes=120, booked_minutes=90, page=1, page_size=2, has_more=True,
                rows=[dict(task_id=number, employee_id=54, employee_name='Example Employee', task_date='2026-09-21',
                           work_status_id=1, status_name='Finished', planned_minutes=60, booked_minutes=45,
                           description='Local task') for number in (1, 2)])
    text = str(task_result(data, daily=True).local_reply)
    assert 'Employees in displayed tasks: 1' in text
    assert 'Employees across all matching tasks: 4' in text
    assert 'Employees included: 27' not in text


def test_monthly_count_deduplicates_employees_across_statuses():
    data = dict(scope='company', month='2026-09', employee_ids=[1, 2, 3],
                rows=[dict(employee_id=1, work_status_id=status, task_count=1, planned_minutes=60) for status in (1, 2)])
    text = str(task_result(data, daily=False).local_reply)
    assert 'Employees with matching tasks: 1' in text


@pytest.mark.parametrize('count', [-1, 2, True, None])
def test_invalid_backend_matching_counts_are_rejected(backend, count):
    client, state = backend
    state.update(modules=['tasks'], matching_employee_count=count)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show my tasks for 2026-09-01'})
    assert response.status_code == 502
