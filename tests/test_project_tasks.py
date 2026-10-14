from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_backend import backend, headers


def setup_project(client, state):
    state.update(modules=['tasks'], scopes=['self', 'company'], task_scopes=['company'])
    service = client.app.state.chat
    service.settings.chat_mode = 'openai'
    create = AsyncMock(side_effect=AssertionError('Project names/descriptions must stay local.'))
    service.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=AsyncMock())
    return create


@pytest.mark.parametrize('message,start,end', [
    ('Show project IIHM task details from 01/10/2026 to 07/10/2026', '2026-10-01', '2026-10-07'),
    ('Show IIHM project task details for 01/10/2026', '2026-10-01', '2026-10-01'),
    ('Show project IIHM tasks for 2026-09', '2026-09-01', '2026-09-30'),
    ('Show project IIHM tasks from 28/09/2026 to 04/10/2026', '2026-09-28', '2026-10-04'),
])
def test_project_queries_use_one_verified_project_and_local_rendering(backend, message, start, end):
    client, state = backend
    create = setup_project(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    data = response.json()['data']
    assert data['tools_used'] == ['project_search', 'get_project_tasks']
    assert state['last_project_search']['name'] == 'IIHM'
    assert 'Project: IIHM' in data['reply']
    assert 'PRIVATE_TASK_DESCRIPTION' in data['reply']
    assert state['last_task_body']['project_id'] == 7
    assert state['last_task_body']['start_date'] == start
    assert state['last_task_body']['end_date'] == end
    assert create.await_count == 0
    client.app.state.chat.client.responses.create = AsyncMock(return_value=SimpleNamespace(output=[], output_text='Hello.'))
    response = client.post('/v1/chat', headers=headers(), json={'message': 'hi', 'conversation_id': data['conversation_id']})
    assert response.status_code == 200
    payload = str(client.app.state.chat.client.responses.create.call_args_list)
    assert 'IIHM' not in payload
    assert 'PRIVATE_TASK_DESCRIPTION' not in payload
    assert 'project_id' not in payload


def test_project_clarification_and_paging_keep_same_project(backend):
    client, state = backend
    setup_project(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show project IIHM tasks'})
    assert 'Which date' in first.json()['data']['reply']
    assert 'project-tasks' not in state['calls']
    cid = first.json()['data']['conversation_id']
    second = client.post('/v1/chat', headers=headers(), json={'message': '01/10/2026 to 07/10/2026', 'conversation_id': cid})
    assert second.status_code == 200
    third = client.post('/v1/chat', headers=headers(), json={'message': 'next page', 'conversation_id': cid})
    assert third.status_code == 200
    assert state['last_task_body']['page'] == 2
    assert state['last_task_body']['project_id'] == 7
    assert state['last_task_body']['start_date'] == '2026-10-01'


@pytest.mark.parametrize('matches,truncated', [([], False), ([{'project_id': 7, 'name': 'IIHM'}, {'project_id': 8, 'name': 'IIHM'}], False),
                                                       ([{'project_id': 7, 'name': 'IIHM'}], True)])
def test_unresolved_project_never_reads_all_tasks(backend, matches, truncated):
    client, state = backend
    setup_project(client, state)
    state.update(project_matches=matches, project_has_more=truncated)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show project IIHM tasks today'})
    assert response.status_code == 200
    assert 'project-tasks' not in state['calls']
    assert 'daily-tasks' not in state['calls']


@pytest.mark.parametrize('message', ['Show project IIHM tasks from 12/10/2026 to 01/10/2026',
                                    'Show project IIHM tasks from 30/02/2026 to 01/03/2026',
                                    'Show project IIHM tasks from 01/01/2026 to 31/12/2026'])
def test_invalid_project_periods_do_not_search_or_read(backend, message):
    client, state = backend
    setup_project(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert 'project-search' not in state['calls']
    assert 'project-tasks' not in state['calls']


def test_project_response_cannot_substitute_project(backend):
    client, state = backend
    setup_project(client, state)
    state['returned_project_id'] = 99
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show project IIHM tasks today'})
    assert response.status_code == 502


def test_project_task_visibility_denial_is_enforced(backend):
    client, state = backend
    setup_project(client, state)
    state['tasks_denied'] = True
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show project IIHM tasks today'})
    assert response.status_code == 403


def test_invalid_project_date_can_be_corrected_without_losing_name(backend):
    client, state = backend
    setup_project(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show project IIHM tasks for 30/02/2026'})
    assert first.status_code == 200
    second = client.post('/v1/chat', headers=headers(), json={'message': '01/10/2026', 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_task_body']['project_id'] == 7


def test_project_with_explicit_employee_remains_filtered(backend):
    client, state = backend
    setup_project(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show project IIHM tasks for employee ID 101 on 01/10/2026'})
    assert response.status_code == 200
    assert state['last_task_body']['employee_id'] == 101


def test_last_seven_days_project_period_is_inclusive(backend):
    from datetime import date
    client, state = backend
    setup_project(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Show project IIHM tasks for last 7 days'})
    assert response.status_code == 200
    assert (date.fromisoformat(state['last_task_body']['end_date']) - date.fromisoformat(state['last_task_body']['start_date'])).days == 6


@pytest.mark.parametrize('message', ['show me last 5 task of ARC Promotion', 'Show project ARC Promotion last 5 tasks'])
def test_latest_project_tasks_use_local_lookup_without_a_date(backend, message):
    client, state = backend
    create = setup_project(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert first.status_code == 200
    assert state['last_project_search']['name'] == 'ARC Promotion'
    assert state['last_task_body']['latest_limit'] == 5
    assert state['last_task_body']['start_date'] is None
    assert 'newest first' in first.json()['data']['reply']
    assert create.await_count == 0
    second = client.post('/v1/chat', headers=headers(), json={'message': 'next 5 tasks', 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_task_body']['page'] == 2
    assert state['last_task_body']['project_id'] == 7
    assert state['last_task_body']['latest_limit'] == 5


def test_latest_followup_keeps_project_but_does_not_assume_previous_month(backend):
    client, state = backend
    setup_project(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show project IIHM tasks for 2026-09'})
    response = client.post('/v1/chat', headers=headers(), json={'message': 'show last 5 tasks', 'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200
    assert state['last_task_body']['project_id'] == 7
    assert state['last_task_body']['start_date'] is None
    assert state['last_task_body']['latest_limit'] == 5


@pytest.mark.parametrize('limit', [0, 51, 999999])
def test_invalid_latest_limit_does_not_query_backend(backend, limit):
    client, state = backend
    setup_project(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': f'Show last {limit} tasks of IIHM'})
    assert response.status_code == 200
    assert 'project-tasks' not in state['calls']
    assert 'project-search' not in state['calls']


@pytest.mark.parametrize('message', [
    'Show me last 5 task history of keyline tracking application Project',
    'Tell me last five task history of Keyline Tracking Application project',
    'Could you please show the latest five tasks for project Keyline Tracking Application?',
    'Give me the five most recent tasks for Keyline Tracking Application',
    'What are the newest five task records belonging to Keyline Tracking Application project?',
    'Show project: Keyline Tracking Application recent tasks',
    'For project Keyline Tracking Application show the last 5 tasks',
    'List recent tasks related to Keyline Tracking Application project',
])
def test_natural_project_wordings_resolve_locally(backend, message):
    client, state = backend
    create = setup_project(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert state['last_project_search']['name'].casefold() == 'keyline tracking application'
    assert state['last_task_body']['latest_limit'] == 5
    assert state['last_task_body']['project_id'] == 7
    assert state['last_task_body']['start_date'] is None
    assert create.await_count == 0


@pytest.mark.parametrize('name_reply', ['Keyline Tracking Application', 'Project Keyline Tracking Application',
                                      'The project name is Keyline Tracking Application'])
def test_project_name_clarification_keeps_count_without_model_fallback(backend, name_reply):
    client, state = backend
    create = setup_project(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show my company latest five task history'})
    assert first.status_code == 200
    assert 'Which project' in first.json()['data']['reply']
    assert 'project-tasks' not in state['calls']
    second = client.post('/v1/chat', headers=headers(), json={'message': name_reply, 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_project_search']['name'] == 'Keyline Tracking Application'
    assert state['last_task_body']['latest_limit'] == 5
    assert state['last_task_body']['scope'] == 'company'
    assert create.await_count == 0


def test_ambiguous_project_correction_preserves_original_request(backend):
    client, state = backend
    setup_project(client, state)
    state['project_matches'] = [{'project_id': 7, 'name': 'Tracking'}, {'project_id': 8, 'name': 'Tracking App'}]
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show latest five tasks of Tracking'})
    assert 'project-tasks' not in state['calls']
    state['project_matches'] = [{'project_id': 8, 'name': 'Tracking App'}]
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Tracking App', 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_task_body']['project_id'] == 8
    assert state['last_task_body']['latest_limit'] == 5


def test_period_and_employee_filters_survive_project_clarification(backend):
    client, state = backend
    setup_project(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show project task history for employee ID 101 on 01/10/2026'})
    assert first.status_code == 200
    assert 'project-tasks' not in state['calls']
    second = client.post('/v1/chat', headers=headers(), json={'message': 'Keyline Tracking Application', 'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_task_body']['employee_id'] == 101
    assert state['last_task_body']['start_date'] == '2026-10-01'


def test_task_claim_without_tool_result_is_not_shown_as_a_fact(backend):
    client, state = backend
    setup_project(client, state)
    client.app.state.chat.client.responses.create = AsyncMock(return_value=SimpleNamespace(output=[], output_text='There are no task records for that project.'))
    response = client.post('/v1/chat', headers=headers(), json={'message': 'Keyline Tracking Application'})
    assert response.status_code == 200
    assert 'not verified task records' in response.json()['data']['reply']
    assert 'There are no task records' not in response.json()['data']['reply']


@pytest.mark.parametrize('message', ['Show me project task history', 'Could you show me project task details',
                                   'Please give me project task history'])
def test_missing_project_is_not_a_command_word_lookup(backend, message):
    client, state = backend
    setup_project(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert 'Which project' in response.json()['data']['reply']
    assert 'project-search' not in state['calls']
    assert 'project-tasks' not in state['calls']


@pytest.mark.parametrize('followup', ['Show me more', 'Next batch', 'Continue', 'More five tasks'])
def test_batch_followup_variants_preserve_project_and_count(backend, followup):
    client, state = backend
    setup_project(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Give me latest five tasks of IIHM'})
    response = client.post('/v1/chat', headers=headers(), json={'message': followup, 'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200
    assert state['last_task_body']['page'] == 2
    assert state['last_task_body']['project_id'] == 7
    assert state['last_task_body']['latest_limit'] == 5


@pytest.mark.parametrize('message', [
    '\u201cLast five task history of Keyline Tracking Application Project.\u201d',
    '\u2018Last five task history of Keyline Tracking Application Project.\u2019',
    '"Last five task history of Keyline Tracking Application Project."',
    'Show latest five tasks for project \u201cKeyline Tracking Application\u201d.',
    '\u201cShow Keyline Tracking Application project task details for 2026-09.\u201d',
])
def test_quoted_sentences_and_project_names_do_not_change_lookup(backend, message):
    client, state = backend
    create = setup_project(client, state)
    response = client.post('/v1/chat', headers=headers(), json={'message': message})
    assert response.status_code == 200
    assert state['last_project_search']['name'] == 'Keyline Tracking Application'
    assert state['last_task_body']['project_id'] == 7
    if 'five' in message:
        assert state['last_task_body']['latest_limit'] == 5
    assert create.await_count == 0


def test_smart_quoted_project_name_reply_preserves_request(backend):
    client, state = backend
    setup_project(client, state)
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show latest five tasks'})
    second = client.post('/v1/chat', headers=headers(), json={'message': '\u201cKeyline Tracking Application\u201d',
                                                           'conversation_id': first.json()['data']['conversation_id']})
    assert second.status_code == 200
    assert state['last_project_search']['name'] == 'Keyline Tracking Application'
    assert state['last_task_body']['latest_limit'] == 5


def test_quotes_inside_project_names_are_preserved():
    from app.project_query import project_name
    assert project_name('Show last five tasks of O\u2019Reilly Application project') == 'O\u2019Reilly Application'
