import json
from copy import deepcopy
from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import select, update

from app.query.exports import fingerprint, pdf, workbook
from app.query.presentation import present
from app.query.wording import public_chart
from tests.query_support import backend as backend_fixture, enable, headers, request

backend = backend_fixture
LEGACY_NOTE = ('Presence (is_present=1) means punch-in exists, including late arrivals and pending punch-outs. '
    'Lateness (is_late=1) uses the assigned arrival cutoff independently of punch-out. '
    'Completed and cancelled records remain reportable; filter status and is_active for open work.')


def report():
    return {'dataset':'attendance','period':{},'total_rows':1,'offset':0,'has_more':False,
        'columns':[
            {'key':'employee_name','label':'Employee','type':'string'},
            {'key':'is_present','label':'is_present','type':'integer'},
            {'key':'is_late','label':'Is Late','type':'integer'},
        ],
        'rows':[{'employee_name':'Project_is_present','is_present':1,'is_late':1}],
        'notice':LEGACY_NOTE}


def test_notes_labels_and_charts_do_not_expose_backend_terms():
    data = report()
    before = deepcopy(data)
    reply = present(data, {})
    for keyword in ('is_present', 'is_late', 'is_active'):
        assert keyword not in str(reply)
        assert keyword not in ' '.join(column['label'] for column in reply.tables[0]['columns'])
    assert reply.tables[0]['rows'] == data['rows']
    assert reply.tables[0]['columns'][1]['key'] == 'is_present'
    chart = {'series':[{'name':'Is Late','values':[1]}], 'labels':['Project_is_present'], 'note':LEGACY_NOTE}
    friendly = public_chart(chart)
    assert friendly['series'][0]['name'] == 'Late'
    assert friendly['labels'] == chart['labels']
    assert data == before


def test_exports_clean_legacy_notes_without_altering_records_or_receipts(monkeypatch):
    import app.query.exports as exports
    data = report()
    before = fingerprint(data)
    book = load_workbook(BytesIO(workbook(data, [])))
    assert book['Report']['B7'].value == 'Present'
    assert book['Report']['C7'].value == 'Late'
    assert book['Report']['A8'].value == 'Project_is_present'
    assert 'is_present' not in book['Selection']['B6'].value
    text = []
    original = exports.SimpleDocTemplate.build

    def capture(document, story, **kwargs):
        for item in story:
            if hasattr(item, 'getPlainText'):
                text.append(item.getPlainText())
        return original(document, story, **kwargs)

    monkeypatch.setattr(exports.SimpleDocTemplate, 'build', capture)
    assert pdf(data, []).startswith(b'%PDF')
    assert all('is_present' not in paragraph and 'is_active' not in paragraph for paragraph in text)
    assert fingerprint(data) == before


def test_saved_replies_are_cleaned_without_rewriting_user_messages_or_storage(backend):
    client, state = backend
    enable(client, state, [request()])
    user_text = 'Count my employee record (is_present)'
    first = client.post('/v1/chat', headers=headers(), json={'message':user_text})
    assert first.status_code == 200
    identifier = first.json()['data']['conversation_id']
    store = client.app.state.store
    table = present(report(), {}).tables[0]
    table['columns'][1]['label'] = 'is_present'
    with store.engine.begin() as db:
        db.execute(update(store.messages).where(store.messages.c.conversation_id == identifier,
            store.messages.c.role == 'assistant').values(content=LEGACY_NOTE, tables=json.dumps([table])))
    saved = client.get('/v1/conversations/' + identifier, headers=headers())
    assert saved.status_code == 200
    messages = saved.json()['data']['messages']
    assert messages[0]['content'] == user_text
    assert 'is_present' not in messages[1]['content'] and 'is_late' not in messages[1]['content']
    assert messages[1]['tables'][0]['columns'][1]['label'] == 'Present'
    assert messages[1]['tables'][0]['rows'][0]['employee_name'] == 'Project_is_present'
    with store.engine.connect() as db:
        assert db.execute(select(store.messages.c.content).where(
            store.messages.c.conversation_id == identifier,
            store.messages.c.role == 'assistant')).scalar_one() == LEGACY_NOTE
