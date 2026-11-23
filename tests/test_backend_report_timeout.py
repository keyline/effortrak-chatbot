import asyncio

import httpx

from app.backend_client import BackendClient
from app.config import Settings


def test_large_report_read_has_headroom_without_expanding_other_backend_deadlines():
    seen = {}

    def handle(request):
        seen[request.url.path.rsplit('/', 1)[-1]] = request.extensions['timeout']
        return httpx.Response(200, json={'success': True, 'data': {'accepted': True}})

    async def run():
        client = BackendClient(Settings(_env_file=None, backend_url='http://127.0.0.1', ai_service_secret='x' * 32))
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            await client.call('query', 'fictional-token', {})
            await client.call('context', 'fictional-token')
        finally:
            await client.close()

    asyncio.run(run())
    assert seen['query']['read'] == 60
    assert seen['query']['connect'] == 5
    assert seen['context']['read'] == 20
