import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.messages import ACCESS_CHANGED, USAGE_LIMIT, public_error
from app.reply import AssistantReply


@pytest.mark.parametrize('status, original, expected', [
    (429, 'Company AI token allowance is insufficient for this request. Contact your administrator.', USAGE_LIMIT),
    (409, 'Permissions changed. Start a new conversation.', ACCESS_CHANGED),
    (410, 'This report expired. Generate it again.', 'This report has expired. Please generate a new report to download it.'),
    (502, 'The AI returned an invalid reporting selection. Please rephrase it.', "We couldn't understand the report you need. Please describe it another way, including the employee, dates and details you'd like to see."),
])
def test_public_errors_keep_status_and_recovery_headers(tmp_path, status, original, expected):
    app = create_app(Settings(_env_file=None, data_mode='sample', chat_mode='demo',
        database_path=str(tmp_path / 'messages.sqlite3'), application_log_path=str(tmp_path / 'messages.log')))

    @app.get('/test-message')
    def warning():
        raise HTTPException(status, original, headers={'Retry-After': '5'})

    with TestClient(app) as client:
        response = client.get('/test-message')
    assert response.status_code == status
    assert response.json() == {'success': False, 'message': expected}
    assert response.headers['Retry-After'] == '5'


def test_busy_is_not_an_exhausted_allowance_and_unknown_details_stay_private():
    busy = public_error('Chat is busy. Please retry shortly.', 429)
    assert busy != USAGE_LIMIT and 'wait' in busy
    assert 'administrator' not in busy
    assert 'private database secret' not in public_error('private database secret', 500)
    assert 'sign in again' in public_error('unknown token failure', 401)


def test_denial_wording_does_not_change_context_status_or_report_names():
    context = {'plan': {'scope': 'self'}}
    reply = AssistantReply('That employee selection is not available within your personal scope.',
        status='denied', query_context=context)
    assert 'your own records' in reply and 'administrator' in reply
    assert reply.status == 'denied' and reply.query_context is context
    report = 'Project: Company AI token allowance\nTotal: 2h 15m'
    assert AssistantReply(report) == report
