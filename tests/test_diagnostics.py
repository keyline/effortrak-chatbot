import json
import logging
from logging.handlers import RotatingFileHandler

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.diagnostics import configure_logging
from app.main import create_app


def read_events(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def settings(tmp_path):
    return Settings(_env_file=None, data_mode='sample', chat_mode='demo',
                    database_path=str(tmp_path / 'test.sqlite3'),
                    application_log_path=str(tmp_path / 'private' / 'application.log'))


def test_lifecycle_and_request_failures_are_private(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)

    @app.get('/test-failure/{identifier}')
    async def failure(identifier: str):
        raise RuntimeError('private-exception-secret')

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/test-failure/private-id?token=private-query',
                          headers={'Authorization': 'Bearer private-token'}).status_code == 500
    text = config.application_log_file.read_text(encoding='utf-8')
    for secret in ('private-exception-secret', 'private-id', 'private-query', 'private-token'):
        assert secret not in text
    events = read_events(config.application_log_file)
    assert events[0]['event'] == 'service_starting'
    assert events[-1]['event'] == 'service_stopped'
    failure = next(event for event in events if event['event'].startswith('request_failed'))
    assert failure['exception_type'] == 'RuntimeError'
    assert failure['stack']
    assert 'route=/test-failure/{identifier}' in failure['event']
    assert all(event['pid'] > 0 and event['time'].endswith('Z') for event in events)


def test_startup_failure_is_recorded(tmp_path, monkeypatch):
    config = settings(tmp_path)

    def broken_store(path):
        raise OSError('private-database-path')

    monkeypatch.setattr('app.main.Store', broken_store)
    with pytest.raises(OSError), TestClient(create_app(config)):
        pass
    events = read_events(config.application_log_file)
    assert any(event['event'] == 'service_lifecycle_failed' and
               event['exception_type'] == 'OSError' for event in events)
    assert 'private-database-path' not in config.application_log_file.read_text()


def test_rotation_and_reconfiguration_do_not_duplicate_events(tmp_path):
    path = tmp_path / 'application.log'
    configure_logging(path)
    configure_logging(path)
    logger = logging.getLogger('app')
    handlers = [handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)]
    assert len(handlers) == 1
    handler = handlers[0]
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 5
    handler.maxBytes = 400
    for i in range(30):
        logger.info('rotation_check number=%s', i)
    backups = list(tmp_path.glob('application.log.*'))
    assert len(backups) == 5
    assert read_events(path)[-1]['event'] == 'rotation_check number=29'
