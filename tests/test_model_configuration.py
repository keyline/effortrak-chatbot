import asyncio

import pytest

from app.config import Settings
from app.query.service import ChatService
from tests.test_token_usage import setup


@pytest.mark.parametrize('metered', [True, False])
@pytest.mark.parametrize(('model', 'effort'), [('gpt-6-luna', 'none'), ('gpt-4.1-mini', None)])
def test_model_settings_reach_generation_and_exact_token_budget(tmp_path, metered, model, effort):
    meter, client, identity, calls = setup(tmp_path)
    settings = Settings(_env_file=None, chat_mode='demo', openai_model=model,
                        openai_reasoning_effort=effort)
    service = ChatService(settings)
    service.client = client
    service.token_meter = meter if metered else None

    asyncio.run(service.create_response(identity, 'nl_sql_interpretation', model=model,
                                       input='Synthetic question', max_output_tokens=100, store=False))

    generated = client.responses.create.call_args.kwargs
    assert generated['model'] == model
    assert generated['max_output_tokens'] == 100
    if effort is None:
        assert 'reasoning' not in generated
    else:
        assert generated['reasoning'] == {'effort': effort}
    if metered:
        counted = client.responses.input_tokens.count.call_args.kwargs
        assert counted.get('reasoning') == generated.get('reasoning')
        assert counted['model'] == model
        assert calls[0][2]['tokens'] == 140
        assert calls[1][2]['total_tokens'] == 50
    else:
        client.responses.input_tokens.count.assert_not_awaited()
