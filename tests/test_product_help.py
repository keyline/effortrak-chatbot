import pytest

from app.product_help import PRODUCT_OVERVIEW, product_help
from tests.test_backend import backend, headers
from tests.test_semantic_query import enable, request


@pytest.mark.parametrize('message', [
    'Tell me about Effortrak', 'What is Effortrak?',
    'Could you please explain Effortrak?', 'What does Effortrak do?',
    'Give me an overview of Effortrak.', 'Hello, tell me about Effortrak!',
    'What are the main features of Effortrak?', 'What is Effortrak used for?',
])
def test_common_product_questions(message):
    result = product_help(message, {})
    assert str(result[0]) == PRODUCT_OVERVIEW
    assert result[0].query_context == {'kind': 'product_help'}


@pytest.mark.parametrize('message', [
    'How do I apply for leave in Effortrak?', 'Show my attendance in Effortrak',
    'What is Effortrak pricing?', 'Tell me about Effortrak employee Joydeep',
    'Which Effortrak modules are enabled for my company?',
    'Tell me about Effortrak and export all employee records',
    'features', 'Tell me more',
])
def test_product_match_does_not_swallow_other_requests(message):
    assert product_help(message, {}) is None


@pytest.mark.parametrize('platform', ['web', 'mobile'])
def test_product_help_is_local_and_clears_employee_context(backend, platform):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', start_date=None, end_date=None),
        request(action='attendance', subject='unspecified', employee_name=None,
            start_date=None, end_date=None, period_kind='none')])

    def send(message, conversation_id=None):
        response = client.post('/v1/chat', headers=headers(), json={
            'message': message, 'conversation_id': conversation_id, 'client_platform': platform})
        assert response.status_code == 200, response.text
        return response.json()['data']

    first = send('Show Joydeep booked effort')
    calls_before = list(state['calls'])
    overview = send('Tell me about Effortrak', first['conversation_id'])
    assert overview['reply'] == PRODUCT_OVERVIEW
    assert create.await_count == 1
    # Identity checks remain necessary, but no report/search/help tool is called.
    added_calls = state['calls'][len(calls_before):]
    assert set(added_calls) <= {'context'}
    assert not overview['artifacts']
    more = send('What about its features?', first['conversation_id'])
    assert more['reply'] == PRODUCT_OVERVIEW
    assert create.await_count == 1
    fresh = send('Show attendance', first['conversation_id'])
    assert 'Whose records' in fresh['reply']


def test_semantic_paraphrase_uses_reviewed_answer_without_inheriting_person(backend):
    client, state = backend
    create = enable(client, state, [request(action='booked_time', start_date=None, end_date=None),
        request(action='product_overview', use_previous=True)])
    first = client.post('/v1/chat', headers=headers(), json={'message': 'Show Joydeep booked effort'})
    response = client.post('/v1/chat', headers=headers(), json={
        'message': 'Could you outline the purpose of this platform?',
        'conversation_id': first.json()['data']['conversation_id']})
    assert response.status_code == 200, response.text
    assert response.json()['data']['reply'] == PRODUCT_OVERVIEW
    assert create.await_count == 2
