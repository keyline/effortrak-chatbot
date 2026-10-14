import ssl
from unittest.mock import patch

from app.chat_service import ChatService
from app.config import Settings


def test_openai_uses_system_trust_with_full_certificate_verification():
    settings = Settings(_env_file=None, chat_mode='openai', openai_api_key='isolated-test-key',
                        openai_model='isolated-test-model')
    with patch('app.chat_service.DefaultAsyncHttpxClient') as http_client, \
            patch('app.chat_service.AsyncOpenAI') as sdk:
        ChatService(settings)
        tls = http_client.call_args.kwargs['verify']
        assert tls.verify_mode == ssl.CERT_REQUIRED
        assert tls.check_hostname is True
        assert sdk.call_args.kwargs['http_client'] is http_client.return_value
        assert sdk.call_args.kwargs['timeout'] == 30


def test_demo_does_not_create_an_openai_http_client():
    settings = Settings(_env_file=None, chat_mode='demo')
    with patch('app.chat_service.DefaultAsyncHttpxClient') as http_client:
        assert ChatService(settings).client is None
        http_client.assert_not_called()
