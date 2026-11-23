from copy import deepcopy
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.query.exports import fingerprint, pdf, workbook
from app.query.presentation import charts_for, present
from tests.query_support import backend as backend_fixture, enable, headers
from tests.test_attendance_presence_reports import FIELDS, detail

backend = backend_fixture


@pytest.mark.parametrize('kind', ['pie', 'bar'])
def test_daily_attendance_charts_count_statuses_instead_of_hours_or_punch_labels(kind):
    _, data = detail()
    data['rows'].append({**data['rows'][0], 'attendance_hours':0, 'punch_out':None})
    data['total_rows'] = 7
    original = deepcopy(data)
    receipt = fingerprint(data)
    reply = present(data, {}, kind)
    chart = reply.charts[0]
    assert chart['labels'] == ['Present', 'Late', 'Absent', 'Paid Leave', 'Holiday', 'Week Off']
    assert chart['series'] == [{'name':'Attendance days', 'values':[2,1,1,1,1,1]}]
    assert chart['unit'] == 'employee-days'
    assert chart['title'] == 'Attendance by status'
    assert reply.tables[0]['rows'] == data['rows']
    assert data == original and fingerprint(data) == receipt


def test_complete_large_detail_report_can_chart_a_small_status_breakdown():
    _, data = detail()
    data['rows'] *= 20
    data['total_rows'] = 120
    charts, notice = charts_for(data, 'pie')
    assert not notice and charts[0]['series'][0]['values'] == [20] * 6
    for partial in [{'has_more':True}, {'offset':50}]:
        charts, notice = charts_for({**data, **partial}, 'pie')
        assert charts == [] and 'complete result' in notice


def test_unknown_status_stays_separate_without_guessing_absence():
    _, data = detail()
    data['rows'][0]['status'] = None
    chart = present(data, {}, 'pie').charts[0]
    assert chart['labels'][0] == 'Unclassified'
    assert chart['series'][0]['values'] == [1] * 6


def test_explicit_aggregate_hours_chart_keeps_its_measure():
    _, data = detail()
    data['plan'].update(mode='aggregate', group_by=['date','status'], metrics=['attendance_hours'], fields=[])
    data['rows'] = [dict(date='2026-11-17', status='Present', attendance_hours=9)]
    data['columns'] = [col for col in data['columns'] if col['key'] in ['date','status','attendance_hours']]
    data['total_rows'] = 1
    chart = present(data, {}, 'pie').charts[0]
    assert chart['unit'] == 'hours' and chart['series'][0]['values'] == [9]


def test_status_counts_reach_spreadsheet_and_pdf_charts(monkeypatch):
    import app.query.exports as exports
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics.charts.piecharts import Pie

    _, data = detail()
    charts = present(data, {}, 'pie').charts
    book = load_workbook(BytesIO(workbook(data, charts)))
    assert list(book['Chart 1'].values) == [
        ('Category','Attendance days'), ('Present',1), ('Late',1), ('Absent',1),
        ('Paid Leave',1), ('Holiday',1), ('Week Off',1)]
    assert book['Report']['C10'].value == 'Absent'
    drawings = []
    original = exports.SimpleDocTemplate.build

    def capture(document, story, **kwargs):
        drawings.extend(item for item in story if isinstance(item, Drawing))
        return original(document, story, **kwargs)

    monkeypatch.setattr(exports.SimpleDocTemplate, 'build', capture)
    assert pdf(data, charts).startswith(b'%PDF')
    pie = next(item for drawing in drawings for item in drawing.contents if isinstance(item, Pie))
    assert list(pie.data) == [1] * 6


@pytest.mark.parametrize('platform', ['web', 'android', 'ios'])
def test_chat_endpoint_returns_the_same_status_breakdown_on_all_clients(backend, platform):
    client, state = backend
    plan, data = detail()
    state.update(scopes=['self','company'], result=data)
    client.app.state.chat.catalogue = {'datasets':{'attendance':{
        'fields':FIELDS, 'metrics':['employee_days','attendance_hours'], 'default_fields':FIELDS}}}
    model = enable(client, state, [{'action':'query', 'plan':plan.model_dump(mode='json'), 'chart':'pie'}])
    response = client.post('/v1/chat', headers=headers(), json={
        'message':'Show attendance as a pie chart', 'client_platform':platform})
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['charts'][0]['labels'] == ['Present', 'Late', 'Absent', 'Paid Leave', 'Holiday', 'Week Off']
    assert result['charts'][0]['series'][0]['values'] == [1] * 6
    assert result['tables'][0]['rows'] == data['rows']
    assert model.await_count == 1
