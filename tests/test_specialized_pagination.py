import asyncio
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook

from app.auth import Identity
from app.query.exports import authorize, generate, snapshot
from app.query.models import SpecializedPlan
from app.query.presentation import present
from app.query.specialized import execute
from tests.query_support import backend as backend_fixture, enable, headers

backend = backend_fixture


ACTOR = Identity(1, 1, 'Fictional admin', 'ADMIN', ('self', 'company'))


def test_late_pages_are_distinct_and_exports_include_every_row():
    async def check():
        data = {'rows': [
            {'employee_id': i, 'employee_name': f'Fictional {i}', 'entries': [
                {'date': f'2026-09-{d:02d}', 'punch_in': '09:15:30', 'late_minutes': 0}
                for d in range(1, 31)]}
            for i in range(1, 5)]}
        backend = SimpleNamespace(call=AsyncMock(return_value=data), revalidate=AsyncMock())
        plan = SpecializedPlan(kind='late_punchin', scope='company', start_date='2026-09-01', end_date='2026-09-30')
        first, _ = await execute(backend, ACTOR, plan)
        second, _ = await execute(backend, ACTOR, plan.model_copy(update={'page': 2}))
        full, _ = await execute(backend, ACTOR, plan.model_copy(update={'page': 2}), exporting=True)
        assert (len(first['rows']), len(second['rows']), len(full['rows'])) == (100, 20, 120)
        assert second['offset'] == 100 and not second['has_more']
        assert first['rows'] + second['rows'] == full['rows']
        assert full['offset'] == 0 and not full['has_more']
        source = {'endpoint': 'specialized', 'arguments': plan.model_dump(mode='json')}
        receipt = snapshot(ACTOR, source, full, 300)
        assert len(generate(full, [], ['pdf', 'xlsx'], receipt)) == 2
        await authorize(receipt, ACTOR, backend)
        data['rows'][0]['entries'][0]['punch_in'] = '11:00:00'
        with pytest.raises(HTTPException) as error:
            await authorize(receipt, ACTOR, backend)
        assert error.value.status_code == 409
    asyncio.run(check())


def test_remote_pages_keep_backend_offsets_and_export_from_first_page():
    async def check():
        rows = [{'employee_name': f'Fictional {i}', 'visit_date': '2026-09-01'} for i in range(45)]
        async def call(endpoint, token, body):
            offset = (body['page'] - 1) * 20
            return {'status': 'answered', 'rows': rows[offset:offset + 20], 'has_more': offset + 20 < len(rows)}
        backend = SimpleNamespace(call=AsyncMock(side_effect=call))
        plan = SpecializedPlan(kind='visits', scope='company', start_date='2026-09-01', end_date='2026-09-30', page=2)
        page, _ = await execute(backend, ACTOR, plan)
        assert page['offset'] == 20 and page['rows'] == rows[20:40]
        assert 'of at least 41 result rows' in str(present(page, {}))
        full, _ = await execute(backend, ACTOR, plan, exporting=True)
        assert full['rows'] == rows and full['total_rows'] == 45
        assert full['offset'] == 0 and not full['has_more']
    asyncio.run(check())


def test_chat_followup_and_download_use_complete_specialized_results(backend):
    client, state = backend
    state['scopes'] = ['self', 'company']
    original = client.app.state.backend.call
    async def call(endpoint, token, body=None):
        if endpoint == 'manager-report':
            return {'rows': [{'employee_id': i, 'employee_name': f'Fictional {i}',
                'entries': [{'date': f'2026-09-{d:02d}', 'punch_in': '10:00:00', 'late_minutes': 45}
                            for d in range(1, 31)]} for i in range(1, 5)]}
        return await original(endpoint, token, body)
    client.app.state.backend.call = call
    plan = {'kind': 'late_punchin', 'scope': 'company', 'start_date': '2026-09-01', 'end_date': '2026-09-30'}
    model = enable(client, state, [
        {'action': 'specialized', 'specialized': plan},
        {'action': 'specialized', 'specialized': {**plan, 'page': 2}},
        {'action': 'specialized', 'specialized': {**plan, 'page': 2}, 'export': 'both'},
    ])
    conversation = None
    for message, count in [('Show company late arrivals in September 2026', 100),
                           ('Next page', 20), ('Export this company report to Excel and PDF', 20)]:
        response = client.post('/v1/chat', headers=headers(), json={
            'message': message, **({'conversation_id': conversation} if conversation else {})})
        assert response.status_code == 200, response.text
        answer = response.json()['data']
        assert len(answer['tables'][0]['rows']) == count
        conversation = answer['conversation_id']
    assert len(answer['artifacts']) == 2
    artifact = next(item for item in answer['artifacts'] if item['format'] == 'xlsx')
    response = client.get(artifact['download_url'], headers=headers())
    assert response.status_code == 200, response.text
    book = load_workbook(BytesIO(response.content))
    assert sum(str(cell.value).startswith('Fictional ') for row in book.active for cell in row) == 120
    assert model.await_count == 3


def test_incomplete_or_oversized_specialized_exports_are_rejected():
    async def check():
        plan = SpecializedPlan(kind='visits', scope='self', start_date='2026-09-01', end_date='2026-09-30')
        for data in [
            {'rows': [{'employee_name': 'Fictional'}] * 5000, 'has_more': True},
            {'rows': [{'employee_name': 'Fictional'}], 'details_truncated': True},
        ]:
            backend = SimpleNamespace(call=AsyncMock(return_value=data))
            with pytest.raises(HTTPException) as error:
                await execute(backend, ACTOR, plan, exporting=True)
            assert error.value.status_code == 413
    asyncio.run(check())
