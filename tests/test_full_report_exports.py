import asyncio
import copy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook

from app import report_export as exports
from app.task_report import AssistantReply
from tests.test_report_export import actor


class Pages:
    def __init__(self, total=7, size=3):
        self.total, self.size = total, size
        self.calls = []
        self.changed = False
        self.denied = False
        self.revalidate = AsyncMock()

    async def call(self, endpoint, credential, arguments):
        self.calls.append((endpoint, credential, copy.deepcopy(arguments)))
        page = arguments.get('page', 1)
        if self.denied and page == 2:
            raise HTTPException(403, 'Scope revoked')
        start = (page - 1) * self.size
        data = dict(scope='self', employee_ids=[101], task_count=self.total,
            page=page, page_size=self.size, has_more=start + self.size < self.total,
            rows=[dict(task_id=index + 1, employee_id=101, employee_name='Employee',
                       description='=unsafe' if index == 0 else f'Record {index + 1}',
                       status_name='Changed' if self.changed and index == 4 else 'Open')
                  for index in range(start, min(start + self.size, self.total))])
        exports.capture(endpoint, arguments, data)
        return data


def arguments(**overrides):
    return dict(scope='self', employee_id=101, start_date='2026-09-01',
                end_date='2026-09-30', page=2, perpage=3, **overrides)


def saved(endpoint, args, data):
    return exports.make_snapshot(AssistantReply('Chat page only'),
        [dict(endpoint=endpoint, arguments=args, data=data)], actor())


@pytest.mark.parametrize('endpoint', ['daily-tasks', 'project-tasks', 'visit-location'])
@pytest.mark.parametrize('format', ['pdf', 'xlsx'])
def test_full_selection_export_and_later_page_reauthorization(endpoint, format):
    backend = Pages()
    args = arguments()
    original = copy.deepcopy(args)

    async def run():
        data = await exports.collect_report_data(endpoint, args, actor(), backend)
        assert [row['task_id'] for row in data['rows']] == list(range(1, 8))
        assert data['has_more'] is False
        snapshot = saved(endpoint, args, data)
        _, _, files = await exports.export_snapshot(snapshot, format, actor(), backend, {})
        assert files[0]['payload'].startswith(b'%PDF-' if format == 'pdf' else b'PK')
        if format == 'xlsx':
            book = load_workbook(BytesIO(files[0]['payload']))
            cells = [cell for sheet in book for row in sheet for cell in row]
            assert any(cell.value == 'Record 7' for cell in cells)
            assert any(cell.value == "'=unsafe" for cell in cells)
            assert not any(cell.data_type == 'f' for cell in cells)
        await exports.authorize(files[0]['export_snapshot'], actor(), backend)
        backend.changed = True
        with pytest.raises(HTTPException) as error:
            await exports.authorize(files[0]['export_snapshot'], actor(), backend)
        assert error.value.status_code == 409
        with pytest.raises(HTTPException):
            await exports.export_snapshot(snapshot, format, actor(), backend, {})

    asyncio.run(run())
    assert args == original
    assert [call[2]['page'] for call in backend.calls[:3]] == [1, 2, 3]
    assert all(call[2]['perpage'] == 3 and call[2]['employee_id'] == 101 for call in backend.calls)


@pytest.mark.parametrize('endpoint', ['daily-tasks', 'project-tasks'])
def test_latest_limit_is_total_selection_even_across_smaller_pages(endpoint):
    backend = Pages(total=100)
    args = arguments(latest_limit=10)
    args.update(start_date=None, end_date=None)
    data = asyncio.run(exports.collect_report_data(endpoint, args, actor(), backend))
    assert len(data['rows']) == 10
    assert [row['task_id'] for row in data['rows']] == list(range(4, 14))
    assert len(backend.calls) == 4
    assert data['has_more'] is False
    asyncio.run(exports.authorize(saved(endpoint, args, data), actor(), backend))
    assert len(backend.calls) == 8


def test_capture_replays_do_not_poison_sources_and_snapshot_uses_all_pages():
    backend = Pages()

    class Service:
        async def _reply(self, *args, **kwargs):
            await backend.call('daily-tasks', actor().credential, arguments())
            return AssistantReply('Displayed page two'), ['daily_tasks'], []

    service = Service()
    service.backend = backend
    reply, _, _ = asyncio.run(exports.reply_with_exports(service, 'show my tasks', [], actor(), 'web'))
    snapshot = reply.export_snapshot
    assert len(snapshot['sources']) == 1
    assert len(snapshot['sources'][0]['data']['rows']) == 3
    assert snapshot['sources'][0]['pending_pages'] is True
    assert snapshot['sources'][0]['arguments']['page'] == 2
    assert snapshot['charts'] == []
    captures = []
    token = exports.capture_sources.set(captures)
    try:
        asyncio.run(exports.authorize(snapshot, actor(), backend))
        assert len(snapshot['sources'][0]['data']['rows']) == 7
        assert 'pending_pages' not in snapshot['sources'][0]
        assert captures == []
    finally:
        exports.capture_sources.reset(token)


def test_revoked_later_page_never_creates_a_partial_export():
    backend = Pages()
    args = arguments()
    data = asyncio.run(exports.collect_report_data('daily-tasks', args, actor(), backend))
    backend.denied = True
    with pytest.raises(HTTPException) as error:
        asyncio.run(exports.export_snapshot(saved('daily-tasks', args, data), 'xlsx', actor(), backend, {}))
    assert error.value.status_code == 403


def test_last_chat_page_never_becomes_a_partial_export_chart():
    backend = Pages()
    args = arguments()
    args['page'] = 3

    class Service:
        async def _reply(self, *unused, **kwargs):
            await backend.call('daily-tasks', actor().credential, args)
            return AssistantReply('Last displayed page'), [], []

    service = Service()
    service.backend = backend
    reply, _, _ = asyncio.run(exports.reply_with_exports(service, 'show tasks', [], actor(), 'web'))
    assert reply.export_snapshot['charts'] == []
    _, _, files = asyncio.run(exports.export_snapshot(reply.export_snapshot, 'xlsx', actor(), backend, {}))
    exported = files[0]['export_snapshot']
    assert len(exported['sources'][0]['data']['rows']) == 7
    assert exported['charts'][0]['series'][0]['values'] == [7]


@pytest.mark.parametrize('failure', ['stalled', 'empty', 'wrong_page', 'scope', 'truncated', 'missing_page'])
def test_invalid_or_incomplete_backend_pages_are_rejected(failure):
    backend = Pages()
    original = backend.call

    async def call(endpoint, credential, args):
        data = await original(endpoint, credential, args)
        if failure == 'stalled':
            data['rows'] = [{'task_id': 1}]
        elif failure == 'empty':
            data['rows'] = []
        elif failure == 'wrong_page':
            data['page'] = 99
        elif failure == 'scope' and args['page'] == 2:
            data['scope'] = 'company'
        elif failure == 'truncated':
            data['details_truncated'] = True
        elif failure == 'missing_page':
            del data['page']
        return data

    backend.call = call
    with pytest.raises(HTTPException):
        asyncio.run(exports.collect_report_data('daily-tasks', arguments(), actor(), backend))


@pytest.mark.parametrize('resource,value', [('MAX_PAGES', 2), ('MAX_ROWS', 4), ('MAX_SOURCE_BYTES', 1000)])
def test_collection_resource_limits_fail_without_partial_success(monkeypatch, resource, value):
    monkeypatch.setattr(exports, resource, value)
    with pytest.raises(HTTPException) as error:
        asyncio.run(exports.collect_report_data('daily-tasks', arguments(), actor(), Pages(total=12)))
    assert error.value.status_code == 422
    assert 'limit' in error.value.detail


def test_nested_daily_rows_share_tables_and_preserve_parent_identity():
    data = dict(rows=[dict(employee_id=index, employee_name=f'Employee {index}',
                          daily=[dict(date='2026-09-01', minutes=60), dict(date='2026-09-02', minutes=90)])
                      for index in range(1, 201)])
    snapshot = saved('work-report', dict(scope='company'), data)
    tables = exports.data_tables(snapshot)
    assert len(tables) == 2
    _, headers, rows = next(table for table in tables if table[0].endswith('/ daily'))
    assert len(rows) == 400
    assert rows[-1][headers.index('employee_id')] == 200
    assert rows[-1][headers.index('minutes')] == 90
    assert exports.pdf_bytes(snapshot, tables).startswith(b'%PDF-')
    assert exports.workbook_bytes(snapshot, tables).startswith(b'PK')


def test_cell_and_field_bounds_apply_before_rendering(monkeypatch):
    snapshot = saved('work-report', {}, dict(rows=[dict(description='x' * 30001)]))
    with pytest.raises(HTTPException) as error:
        exports.data_tables(snapshot)
    assert 'field' in error.value.detail
    snapshot = saved('work-report', {}, dict(rows=[dict(employee_id=1, daily=[dict(minutes=1)] * 20)]))
    monkeypatch.setattr(exports, 'MAX_CELLS', 10)
    with pytest.raises(HTTPException) as error:
        exports.data_tables(snapshot)
    assert error.value.status_code == 422


@pytest.mark.parametrize('endpoint', ['manager-report', 'leave-history', 'employee-count'])
def test_complete_unpaginated_sources_are_replayed_without_invented_pagination(endpoint):
    data = {'employee_count': 12} if endpoint == 'employee-count' else dict(rows=[{'employee_id': 101}], has_more=False)
    backend = SimpleNamespace(call=AsyncMock(return_value=data), revalidate=AsyncMock())
    args = {'scope': 'self'}
    result = asyncio.run(exports.collect_report_data(endpoint, args, actor(), backend))
    assert result == data
    backend.call.assert_awaited_once_with(endpoint, actor().credential, args)
    asyncio.run(exports.authorize(saved(endpoint, args, result), actor(), backend))


def test_old_page_only_snapshot_cannot_generate_a_partial_file():
    backend = Pages()
    args = arguments()
    partial = asyncio.run(backend.call('daily-tasks', actor().credential, args))
    with pytest.raises(HTTPException) as error:
        asyncio.run(exports.export_snapshot(saved('daily-tasks', args, partial), 'pdf', actor(), backend, {}))
    assert error.value.status_code == 409


def test_direct_export_and_latest_single_page_request():
    backend = Pages(total=100, size=10)

    class Service:
        async def _reply(self, *args, **kwargs):
            selection = arguments(latest_limit=10)
            selection.update(page=1, start_date=None, end_date=None)
            await backend.call('daily-tasks', actor().credential, selection)
            return AssistantReply('Latest ten tasks'), ['daily_tasks'], []

    service = Service()
    service.backend = backend
    reply, _, files = asyncio.run(exports.reply_with_exports(service, 'Export my last 10 tasks as Excel', [], actor(), 'web'))
    assert len(files) == 1
    assert len(reply.export_snapshot['sources'][0]['data']['rows']) == 10
    assert all(call[2]['page'] == 1 for call in backend.calls)


def test_clock_fields_ignored_on_every_replayed_page():
    backend = Pages()
    original = backend.call
    clock = [0]

    async def call(endpoint, credential, args):
        data = await original(endpoint, credential, args)
        clock[0] += 1
        data['generated_at'] = str(clock[0])
        return data

    backend.call = call
    args = arguments()
    data = asyncio.run(exports.collect_report_data('daily-tasks', args, actor(), backend))
    asyncio.run(exports.authorize(saved('daily-tasks', args, data), actor(), backend))


def test_file_byte_limit_rejects_the_entire_artifact(monkeypatch):
    backend = Pages()
    args = arguments()
    data = asyncio.run(exports.collect_report_data('daily-tasks', args, actor(), backend))
    monkeypatch.setattr(exports, 'workbook_bytes', lambda *args: b'PK' + b'x' * (8 * 1024 * 1024))
    with pytest.raises(HTTPException) as error:
        asyncio.run(exports.export_snapshot(saved('daily-tasks', args, data), 'xlsx', actor(), backend, {}))
    assert error.value.status_code == 422
    assert 'file is too large' in error.value.detail


def test_repeated_final_page_cannot_masquerade_as_a_complete_report():
    backend = Pages(total=6)
    original = backend.call

    async def call(endpoint, credential, args):
        data = await original(endpoint, credential, args)
        data['rows'] = [{'task_id': 1}]
        return data

    backend.call = call
    with pytest.raises(HTTPException) as error:
        asyncio.run(exports.collect_report_data('daily-tasks', arguments(), actor(), backend))
    assert 'no progress' in error.value.detail
