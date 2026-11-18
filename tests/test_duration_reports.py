from copy import deepcopy
from datetime import timedelta
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.query.exports import fingerprint, pdf, workbook
from app.query.formatting import display, hour_totals
from app.query.presentation import present


def report():
    return {
        "dataset": "effort", "period": {}, "offset": 0, "has_more": False,
        "total_rows": 2,
        "columns": [
            {"key": "employee_name", "label": "Employee", "type": "string"},
            {"key": "booked_hours", "label": "Booked Hours", "type": "number", "unit": "hours"},
        ],
        "rows": [{"employee_name": "Alice", "booked_hours": 3.5},
                 {"employee_name": "Bob", "booked_hours": 2.25}],
    }


@pytest.mark.parametrize("value, expected", [(2.25, "2h 15m"), (3.5, "3h 30m"),
    (0, "0h 00m"), (1.9999, "2h 00m"), (27.25, "27h 15m"),
    (-2.25, "-2h 15m"), (None, "Unavailable")])
def test_duration_minutes(value, expected):
    assert display(value, "hours") == expected


def test_totals_and_charts_keep_verified_numbers():
    data = report()
    before = deepcopy(data)
    reply = present(data, {}, "bar")
    assert "Total Booked Hours: 5h 45m" in reply
    assert reply.tables[0]["rows"] == data["rows"]
    assert reply.charts[0]["series"][0]["values"] == [3.5, 2.25]
    assert data == before
    assert display(2.25, "records") == "2.25"
    assert display("09:30:00", "hours") == "09:30:00"
    data["has_more"] = True
    assert "Total Booked Hours (this page): 5h 45m" in present(data, {})
    data["rows"][1]["booked_hours"] = None
    assert hour_totals(data) == [("Total Booked Hours (this page)", None)]
    data["has_more"] = False
    data["offset"] = 100
    assert hour_totals(data) == [("Total Booked Hours (this page)", None)]
    data["rows"] = []
    assert hour_totals(data) == []


def test_exports_show_durations_and_totals_without_changing_receipt(monkeypatch):
    data = report()
    data['project'] = {'project_id': 7, 'project_name': 'Fictional Project <A> & B'}
    before = fingerprint(data)
    sheet = load_workbook(BytesIO(workbook(data, []))).active
    assert sheet["B8"].value == timedelta(hours=3.5)
    assert sheet["B9"].value == timedelta(hours=2.25)
    assert sheet["B8"].number_format == '[h]"h "mm"m"'
    assert sheet["A10"].value == "Total Booked Hours: 5h 45m"
    assert sheet["A6"].value == 'Project: Fictional Project <A> & B'
    assert sheet.auto_filter.ref == "A7:B9"
    # Inspect the actual PDF story while still rendering its charts and pages.
    import app.query.exports as exports
    original = exports.SimpleDocTemplate.build
    text = []

    def capture(document, story, **kwargs):
        for item in story:
            if hasattr(item, "getPlainText"):
                text.append(item.getPlainText())
            if hasattr(item, "_cellvalues"):
                text.extend(cell.getPlainText() for row in item._cellvalues for cell in row)
        return original(document, story, **kwargs)

    monkeypatch.setattr(exports.SimpleDocTemplate, "build", capture)
    charts = present(data, {}, "bar").charts
    chart_sheet = load_workbook(BytesIO(workbook(data, charts)))["Chart 1"]
    assert chart_sheet["B2"].value == timedelta(hours=3.5)
    assert chart_sheet["B2"].number_format == '[h]"h "mm"m"'
    assert pdf(data, charts).startswith(b"%PDF")
    assert "2h 15m" in text and "3h 30m" in text
    assert "Total Booked Hours: 5h 45m" in text
    assert 'Project: Fictional Project <A> & B' in text
    assert fingerprint(data) == before
    data['project']['project_name'] = 'Renamed project'
    assert fingerprint(data) != before


@pytest.mark.parametrize("kind", ["pie", "bar"])
def test_pdf_charts_keep_shape_and_smaller_values_visible(monkeypatch, kind):
    import app.query.exports as exports
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.piecharts import Pie
    from reportlab.graphics import renderSVG

    original = exports.SimpleDocTemplate.build
    drawings = []

    def capture(document, story, **kwargs):
        drawings.extend(item for item in story if isinstance(item, Drawing))
        return original(document, story, **kwargs)

    monkeypatch.setattr(exports.SimpleDocTemplate, "build", capture)
    data = report()
    payload = pdf(data, present(data, {}, kind).charts)
    assert payload.startswith(b"%PDF")
    chart = next(item for item in drawings[0].contents if isinstance(item, (Pie, VerticalBarChart)))
    if kind == "pie":
        assert chart.width == chart.height
        assert chart.slices[0].fillColor != chart.slices[1].fillColor
    else:
        assert chart.valueAxis.valueMin == 0
        assert chart.bars[(0, 0)].fillColor != chart.bars[(0, 1)].fillColor
        assert chart.data == [[3.5, 2.25]]
    # Force the graphics renderer to build the actual paths, not just serialize settings.
    svg = renderSVG.drawToString(drawings[0])
    assert "fill: rgb(8%,51%,41%)" in svg
    assert "fill: rgb(25%,41%,83%)" in svg
