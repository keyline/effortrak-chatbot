from datetime import date, timedelta

import pytest

from app.work_report import wants_review
from tests.test_backend import backend, headers
from tests.test_semantic_query import enable, request


def totals(days=1):
    result = dict(days=days, punch_in='09:35:00' if days == 1 else None, punch_out='18:00:00' if days == 1 else None,
                  attendance_minutes=505 * days, productive_seconds=18000 * days, idle_seconds=3600 * days,
                  tracked_seconds=21600 * days, screenshot_count=60 * days, average_activity=75.0, activity_days=days)
    result.update({field + '_days': days for field in ('attendance_minutes', 'productive_seconds', 'idle_seconds', 'tracked_seconds', 'screenshot_count')})
    return result


def payload(args):
    start, end = date.fromisoformat(args['start_date']), date.fromisoformat(args['end_date'])
    as_of = date(2026, 10, 16)
    review_end = min(end, as_of - timedelta(days=1))
    review_start = review_end - timedelta(days=29)
    review = None
    if args['detailed_review']:
        review = dict(summary=totals(30), days=[dict(date=str(review_start + timedelta(days=i)), punch_in='09:35:00', punch_out='18:00:00',
            attendance_minutes=505, productive_seconds=18000, idle_seconds=3600, tracked_seconds=21600, screenshot_count=60,
            activity=75.0, eligible=True, late=False, missing_punchout=False, short_day=False) for i in range(30)])
    result = dict(data_source='effortrak_backend', scope=args['scope'], employee_ids=[101],
        period=dict(start=str(start), end=str(end), as_of=str(as_of)),
        review_period=dict(start=str(review_start), end=str(review_end)), detailed_review=args['detailed_review'],
        rows=[dict(employee_id=101, employee_name='Private Employee', tracking_available=True,
                   summary=totals((end - start).days + 1), attention=[], review=review)])
    if args.get('include_daily'):
        result['rows'][0]['daily'] = [dict(date=str(start + timedelta(days=i)), punch_in='09:35:00', punch_out='18:00:00',
            attendance_minutes=505, productive_seconds=18000, idle_seconds=3600, tracked_seconds=21600, screenshot_count=60,
            activity=75.0, eligible=True, late=False, missing_punchout=False, short_day=False) for i in range((end - start).days + 1)]
    return result


def wire(client, state, questions, change=None):
    mock = enable(client, state, questions)
    original = client.app.state.backend.call
    seen = []

    async def call(endpoint, token, body=None):
        if endpoint == 'work-report':
            seen.append(body)
            value = payload(body)
            if change:
                change(value)
            return value
        return await original(endpoint, token, body)

    client.app.state.backend.call = call
    return mock, seen


def question(action='work_summary', **kwargs):
    return request(action=action, subject='employee', employee_name='Joydeep Ghosh',
                   start_date='2026-09-01', end_date='2026-09-01', period_kind='day', **kwargs)


def test_default_work_summary_is_brief_and_never_sends_records_to_model(backend):
    client, state = backend
    mock, seen = wire(client, state, [question()])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep Ghosh work summary for 2026-09-01 including productive and idle time'})
    assert response.status_code == 200, response.text
    answer = response.json()['data']['reply']
    assert 'Private Employee' in answer and '09:35:00' in answer and 'Productive: 5h 0m' in answer
    assert 'Idle: 1h 0m' in answer and '75%' in answer and 'Screenshot captures: 60' in answer
    assert 'Needs attention' not in answer and 'Detailed review' not in answer
    assert seen[0]['detailed_review'] is False
    assert mock.await_count == 1
    assert 'Private Employee' not in str(mock.call_args) and '09:35:00' not in str(mock.call_args)


def test_recurring_concerns_only_add_short_attention_note(backend):
    client, state = backend
    def change(value):
        value['rows'][0]['attention'] = ['Late arrival: 6 of 20 recorded working days.']
    wire(client, state, [question()], change)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep work summary for 2026-09-01'})
    assert response.status_code == 200
    assert 'Needs attention' in response.json()['data']['reply']
    assert 'Detailed review' not in response.json()['data']['reply']


def test_detailed_review_is_explicit_and_followup_reuses_employee(backend):
    client, state = backend
    followup = request(action='work_review', subject='previous', employee_name=None, start_date=None, end_date=None,
                       period_kind='none', use_previous=True)
    _, seen = wire(client, state, [question(), followup])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep work summary for 2026-09-01'})
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show detailed 30 day analysis', 'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200, response.text
    assert seen[-1]['employee_id'] == 101 and seen[-1]['detailed_review'] is True
    assert 'Detailed review: 2026-08-03 to 2026-09-01' in response.json()['data']['reply']


def test_model_cannot_enable_detailed_review_for_routine_summary(backend):
    client, state = backend
    _, seen = wire(client, state, [question(action='work_review')])
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep work hours for 2026-09-01'})
    assert response.status_code == 200 and seen[0]['detailed_review'] is False


@pytest.mark.parametrize('mutation', ['foreign_employee', 'unrequested_review', 'tracking_leak', 'bad_score', 'extra_path', 'wrong_window', 'coerced_flag'])
def test_malformed_or_unauthorized_backend_work_data_is_rejected(backend, mutation):
    client, state = backend
    def change(value):
        row = value['rows'][0]
        if mutation == 'foreign_employee': row['employee_id'] = 999
        elif mutation == 'unrequested_review': value['detailed_review'] = True
        elif mutation == 'tracking_leak': row['tracking_available'] = False
        elif mutation == 'bad_score': row['summary']['average_activity'] = 120
        elif mutation == 'extra_path': row['screenshot_url'] = '/private/file.jpg'
        elif mutation == 'wrong_window': value['review_period']['end'] = '2026-08-31'
        else: row['tracking_available'] = 'true'
    wire(client, state, [question()], change)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep work summary for 2026-09-01'})
    assert response.status_code == 502


def test_unavailable_tracking_is_not_zero_activity_or_bad_performance(backend):
    client, state = backend
    def change(value):
        row = value['rows'][0]
        row['tracking_available'] = False
        for field in ('productive_seconds', 'idle_seconds', 'tracked_seconds', 'screenshot_count'):
            row['summary'][field] = None
            row['summary'][field + '_days'] = 0
        row['summary'].update(average_activity=None, activity_days=0)
    wire(client, state, [question()], change)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep work report for 2026-09-01'})
    assert response.status_code == 200
    assert 'unavailable under current module access' in response.json()['data']['reply']
    assert 'Needs attention' not in response.json()['data']['reply']


def test_review_requires_deliberate_request():
    assert not wants_review('Show employee work summary')
    assert not wants_review('Review employee work hours yesterday')
    assert not wants_review('Anything alarming?')
    assert not wants_review('Show detailed work hours for today')
    assert wants_review('Show detailed analysis')
    assert wants_review('Analyze last 30 days')
