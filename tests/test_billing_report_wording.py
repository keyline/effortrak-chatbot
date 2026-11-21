import json
from copy import deepcopy
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import select, update

from app.query.exports import fingerprint, pdf, workbook
from app.query.presentation import present
from app.query.wording import public_chart, public_table, report_view
from tests.query_support import backend as backend_fixture, enable, headers, request

backend = backend_fixture


def report():
    return {
        'dataset': 'effort', 'period': {}, 'offset': 0, 'total_rows': 3, 'has_more': False,
        'columns': [
            {'key': 'billing', 'label': 'Billing', 'type': 'string'},
            {'key': 'booked_hours', 'label': 'Booked Hours', 'type': 'number', 'unit': 'hours'},
        ],
        'rows': [{'billing': value, 'booked_hours': hours} for value, hours in
                 [('billable', 2.25), ('non_billable', 3.5), ('unclassified', 1)]],
        'plan': {'filters': [{'field': 'billing', 'operator': '=', 'value': 'non_billable'}]},
    }


LABELS = ['Billable', 'Non-billable', 'Unclassified billing']


@pytest.mark.parametrize('kind', ['bar', 'pie', 'table'])
def test_fresh_reports_and_charts_use_labels_without_changing_source_or_selection(kind):
    data = report()
    before, receipt = deepcopy(data), fingerprint(data)
    reply = present(data, {'plan': data['plan']}, kind)
    assert [row['billing'] for row in reply.tables[0]['rows']] == LABELS
    assert reply.charts[0]['labels'] == LABELS
    assert reply.charts[0]['series'][0]['values'] == [2.25, 3.5, 1]
    assert reply.query_context['plan']['filters'][0]['value'] == 'non_billable'
    assert report_view(report_view(data)) == report_view(data)
    assert data == before and fingerprint(data) == receipt


def test_only_billing_values_are_formatted_and_missing_or_unknown_values_are_preserved():
    data = report()
    data['columns'].insert(0, {'key': 'project_name', 'label': 'Project', 'type': 'string'})
    for row in data['rows']:
        row['project_name'] = 'non_billable'
    data['rows'][1]['billing'] = None
    data['rows'][2]['billing'] = 'future_status'
    table = public_table(data)
    assert [row['project_name'] for row in table['rows']] == ['non_billable'] * 3
    assert [row['billing'] for row in table['rows']] == ['Billable', None, 'future_status']
    chart = present(data, {}, 'bar').charts[0]
    assert chart['labels'] == ['non_billable / Billable', 'non_billable / Unavailable', 'non_billable / future_status']
    unrelated = {**chart, 'labels': ['non_billable', 'billable', 'unclassified']}
    assert public_chart(unrelated, [data])['labels'] == unrelated['labels']


def legacy_chart():
    chart = present(report(), {}, 'bar').charts[0]
    return {**chart, 'labels': ['billable', 'non_billable', 'unclassified']}


def test_exports_format_table_and_legacy_chart_without_changing_receipt(monkeypatch):
    import app.query.exports as exports
    data, charts = report(), [legacy_chart()]
    receipt = fingerprint(data)
    book = load_workbook(BytesIO(workbook(data, charts)))
    assert [book['Report'].cell(row, 1).value for row in range(8, 11)] == LABELS
    assert [book['Chart 1'].cell(row, 1).value for row in range(2, 5)] == LABELS
    text, chart_labels = [], []
    original = exports.SimpleDocTemplate.build

    def capture(document, story, **kwargs):
        for item in story:
            if hasattr(item, '_cellvalues'):
                text.extend(cell.getPlainText() for row in item._cellvalues for cell in row)
            if isinstance(item, exports.Drawing):
                chart_labels.extend(item.contents[0].categoryAxis.categoryNames)
        return original(document, story, **kwargs)

    monkeypatch.setattr(exports.SimpleDocTemplate, 'build', capture)
    assert pdf(data, charts).startswith(b'%PDF')
    assert all(label in text for label in LABELS)
    assert chart_labels == LABELS
    assert fingerprint(data) == receipt and charts[0]['labels'][1] == 'non_billable'


@pytest.mark.parametrize('platform', ['web', 'android', 'ios'])
def test_saved_report_api_formats_billing_for_all_clients_without_rewriting_storage(backend, platform):
    client, state = backend
    enable(client, state, [request()])
    first = client.post('/v1/chat', headers=headers(), json={
        'message': 'Count my employee record', 'client_platform': platform})
    assert first.status_code == 200
    identifier = first.json()['data']['conversation_id']
    store = client.app.state.store
    raw_table, chart = report(), legacy_chart()
    with store.engine.begin() as db:
        db.execute(update(store.messages).where(store.messages.c.conversation_id == identifier,
            store.messages.c.role == 'assistant').values(tables=json.dumps([raw_table]), charts=json.dumps([chart])))
    saved = client.get('/v1/conversations/' + identifier, headers=headers())
    assert saved.status_code == 200
    message = saved.json()['data']['messages'][1]
    assert [row['billing'] for row in message['tables'][0]['rows']] == LABELS
    assert message['charts'][0]['labels'] == LABELS
    with store.engine.connect() as db:
        stored = db.execute(select(store.messages.c.tables).where(
            store.messages.c.conversation_id == identifier, store.messages.c.role == 'assistant')).scalar_one()
    assert json.loads(stored) == [raw_table]
