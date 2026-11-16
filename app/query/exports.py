"""Bounded exports of the exact authorized result, with private download receipts."""

import hashlib
import json
import time
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

from fastapi import HTTPException
from openpyxl import Workbook
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.drawing.image import Image as SpreadsheetImage
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

REPORT_LOGO = Path(__file__).resolve().parents[1] / "assets" / "effortrak-logo.png"


def fingerprint(data):
    stable = {key: data[key] for key in ("dataset", "columns", "rows", "total_rows")}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def snapshot(identity, source, data, ttl):
    return {
        "version": 2,
        "company_id": identity.company_id,
        "user_id": identity.user_id,
        "policy_key": identity.policy_key,
        "source": source,
        "fingerprint": fingerprint(data),
        "expires_at": int(time.time()) + ttl,
    }


async def authorize(receipt, identity, backend):
    if (
        not isinstance(receipt, dict)
        or receipt.get("version") != 2
        or not identity.can_export
        or receipt.get("company_id") != identity.company_id
        or receipt.get("user_id") != identity.user_id
        or receipt.get("policy_key") != identity.policy_key
    ):
        raise HTTPException(
            403, "You no longer have permission to download this report."
        )
    if receipt.get("expires_at", 0) <= time.time():
        raise HTTPException(410, "This report has expired. Generate it again.")
    source = receipt.get("source", {})
    if source.get("endpoint") not in ("query", "specialized") or not isinstance(
        source.get("arguments"), dict
    ):
        raise HTTPException(
            409, "Generate this report again using the current chatbot."
        )
    await backend.revalidate(identity)
    if source["endpoint"] == "query":
        data = await backend.call("query", identity.credential, source["arguments"])
    else:
        from app.query.models import SpecializedPlan
        from app.query.specialized import execute

        data, _ = await execute(
            backend, identity, SpecializedPlan.model_validate(source["arguments"])
        )
    if data.get("status") != "answered" or fingerprint(data) != receipt.get(
        "fingerprint"
    ):
        raise HTTPException(
            409, "The report selection or data has changed. Generate the report again."
        )


def generate(data, charts, formats, receipt):
    if len(data["rows"]) > 5000 or data.get("has_more") or data.get("offset"):
        raise HTTPException(
            413,
            "Narrow this report before exporting; partial reports are not exported.",
        )
    reports = []
    for format in formats:
        identifier = str(uuid4())
        payload = workbook(data, charts) if format == "xlsx" else pdf(data, charts)
        if len(payload) > 12 * 1024 * 1024:
            raise HTTPException(
                413, "This file is too large. Narrow the report and retry."
            )
        reports.append(
            {
                "report_id": identifier,
                "filename": f"effortrak-{data['dataset']}-{identifier[:8]}.{format}",
                "format": format,
                "payload": payload,
                "employee_ids": [],
                "download_url": f"/v1/reports/{identifier}/download",
                "export_snapshot": receipt,
            }
        )
    return reports


def report_heading(data):
    """Human-readable selection metadata shared by both export formats."""
    titles = {
        "employees": "Employee Report",
        "attendance": "Attendance Report",
        "effort": "Booked Effort Report",
        "tasks": "Task Report",
        "leave": "Leave Report",
        "assets": "Asset Inventory Report",
        "asset_assignments": "Asset Assignment Report",
        "lms_leads": "LMS Lead Report",
        "lms_followups": "LMS Follow-up Report",
    }
    title = titles.get(
        data["dataset"], data["dataset"].replace("_", " ").title() + " Report"
    )
    period = data.get("period") or {}

    def readable(value):
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
        except ValueError:
            return None

    start, end = readable(period.get("start")), readable(period.get("end"))
    period_label = (
        (start if start == end else f"{start} - {end}")
        if start and end
        else (
            "From " + start
            if start
            else "Through " + end
            if end
            else "Current records"
            if data["dataset"] in ("employees", "assets", "lms_leads")
            else "Not specified"
        )
    )
    scope = {
        "self": "My records",
        "team": "Team records",
        "company": "Company records",
    }.get(data.get("scope"), "Authorized records")
    generated = datetime.now(ZoneInfo("Asia/Kolkata")).strftime(
        "%d %b %Y, %I:%M %p IST"
    )
    return {
        "title": title,
        "period": period_label,
        "scope": scope,
        "rows": f"{data['total_rows']:,}",
        "generated": generated,
    }


def workbook(data, charts):
    heading = report_heading(data)
    book = Workbook()
    book.properties.title = heading["title"]
    book.properties.creator = "Effortrak"
    sheet = book.active
    sheet.title = "Report"
    width = max(1, len(data["columns"]))
    sheet.append(["" if REPORT_LOGO.is_file() else "EFFORTRAK"])
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
    sheet.row_dimensions[1].height = 40
    if REPORT_LOGO.is_file():
        logo = SpreadsheetImage(str(REPORT_LOGO))
        logo.width, logo.height = 150, 150 * logo.height / logo.width
        sheet.add_image(logo, "A1")
    else:
        sheet["A1"].font = Font(name="Calibri", bold=True, size=18, color="12364A")
    for row in [
        [heading["title"]],
        ["Reporting period: " + heading["period"]],
        [heading["scope"] + "  |  Result rows: " + heading["rows"]],
        ["Generated: " + heading["generated"]],
        [],
    ]:
        sheet.append(row)
    for number in range(2, 6):
        sheet.merge_cells(
            start_row=number, start_column=1, end_row=number, end_column=width
        )
        sheet.cell(number, 1).font = Font(
            name="Calibri",
            size=18 if number == 2 else 11,
            bold=number == 2,
            color="FFFFFF" if number == 2 else "334155",
        )
        sheet.cell(number, 1).alignment = Alignment(vertical="center")
    for cell in sheet[2]:
        cell.fill = PatternFill("solid", fgColor="12364A")
    sheet.row_dimensions[2].height = 34
    for number in (3, 4, 5):
        sheet.row_dimensions[number].height = 22
    sheet.row_dimensions[6].height = 8
    sheet.append([col["label"] for col in data["columns"]])
    for index, col in enumerate(data["columns"], 1):
        cell = sheet.cell(7, index)
        cell.font = Font(name="Calibri", bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="12364A")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = (
            48 if col["key"] == "reason" or width == 1 else 24
        )
    sheet.row_dimensions[7].height = 28
    for row in data["rows"]:
        sheet.append([row.get(col["key"]) for col in data["columns"]])
    # Explicit string types prevent spreadsheet formula execution, including names
    # beginning with =, +, -, @, tabs and carriage returns.
    for cells in sheet:
        for cell in cells:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    sheet.freeze_panes = "A8"
    sheet.auto_filter.ref = f"A7:{get_column_letter(width)}{max(7, sheet.max_row)}"
    sheet.print_title_rows = "1:7"
    sheet.sheet_view.showGridLines = False
    metadata = book.create_sheet("Selection")
    for key, value in [
        ("Report", heading["title"]),
        ("Reporting period", heading["period"]),
        ("Scope", heading["scope"]),
        ("Generated", heading["generated"]),
        ("Rows", data["total_rows"]),
        ("Notice", data.get("notice", "")),
    ]:
        metadata.append([key, value])
    metadata.column_dimensions["A"].width = 22
    metadata.column_dimensions["B"].width = 100
    for row in metadata:
        row[0].font = Font(bold=True, color="12364A")
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    for cells in metadata:
        for cell in cells:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    for index, spec in enumerate(charts):
        chart_data = book.create_sheet(f"Chart {index + 1}")
        chart_data.append(["Category"] + [series["name"] for series in spec["series"]])
        for i, name in enumerate(spec["labels"]):
            chart_data.append(
                [name] + [series["values"][i] for series in spec["series"]]
            )
        for cells in chart_data:
            for cell in cells:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        chart = PieChart() if spec["type"] == "pie" else BarChart()
        chart.title = spec["title"]
        chart.add_data(
            Reference(
                chart_data,
                min_col=2,
                max_col=1 + len(spec["series"]),
                min_row=1,
                max_row=1 + len(spec["labels"]),
            ),
            titles_from_data=True,
        )
        chart.set_categories(
            Reference(chart_data, min_col=1, min_row=2, max_row=1 + len(spec["labels"]))
        )
        chart_data.add_chart(chart, "H2")
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def pdf(data, charts):
    output = BytesIO()
    heading = report_heading(data)
    page_width, page_height = landscape(A4)
    available_width = page_width - 92
    logo = ImageReader(str(REPORT_LOGO)) if REPORT_LOGO.is_file() else None
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ReportTitle",
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=27,
            textColor=colors.HexColor("#12364a"),
            spaceAfter=5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReportPeriod",
            fontSize=11,
            leading=15,
            textColor=colors.HexColor("#334155"),
            spaceAfter=12,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReportMeta",
            fontSize=9,
            leading=13,
            textColor=colors.HexColor("#334155"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReportNote",
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#526579"),
        )
    )
    summary = Table(
        [
            [
                Paragraph(escape(heading["scope"]), styles["ReportMeta"]),
                Paragraph("Result rows: " + heading["rows"], styles["ReportMeta"]),
            ]
        ],
        colWidths=[available_width * 0.7, available_width * 0.3],
    )
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef4f7")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story = [
        Paragraph(escape(heading["title"]), styles["ReportTitle"]),
        Paragraph(
            "Reporting period: " + escape(heading["period"]), styles["ReportPeriod"]
        ),
        summary,
        Spacer(1, 16),
    ]
    for spec in charts:
        drawing = Drawing(720, 230)
        chart = Pie() if spec["type"] == "pie" else VerticalBarChart()
        chart.x, chart.y, chart.width, chart.height = 70, 55, 580, 160
        if spec["type"] == "pie":
            chart.data = spec["series"][0]["values"]
            chart.labels = spec["labels"]
        else:
            chart.data = [series["values"] for series in spec["series"]]
            chart.categoryAxis.categoryNames = spec["labels"]
            chart.categoryAxis.labels.angle = 35
            chart.categoryAxis.labels.fontSize = 7
        drawing.add(chart)
        story.extend([Paragraph(escape(spec["title"]), styles["Heading2"]), drawing])
    cells = [
        [Paragraph(escape(col["label"]), styles["Normal"]) for col in data["columns"]]
    ]
    for row in data["rows"]:
        cells.append(
            [
                Paragraph(
                    escape(
                        str(
                            row.get(col["key"])
                            if row.get(col["key"]) is not None
                            else "Unavailable"
                        )
                    ),
                    styles["Normal"],
                )
                for col in data["columns"]
            ]
        )
    if data["columns"]:
        table = LongTable(
            cells,
            repeatRows=1,
            colWidths=[available_width / len(data["columns"])] * len(data["columns"]),
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eff5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ]
            )
        )
        story.append(table)
    if data.get("notice"):
        story.extend(
            [
                Spacer(1, 12),
                Paragraph("Report notes", styles["Heading3"]),
                Paragraph(escape(data["notice"]), styles["ReportNote"]),
            ]
        )

    def page_header(canvas, document):
        canvas.saveState()
        canvas.setFillColor(colors.HexColor("#12364a"))
        if logo:
            logo_width, logo_height = logo.getSize()
            canvas.drawImage(
                logo,
                46,
                page_height - 46,
                width=108,
                height=108 * logo_height / logo_width,
                mask="auto",
            )
        else:
            canvas.setFont("Helvetica-Bold", 10)
            canvas.drawString(46, page_height - 37, "EFFORTRAK")
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#526579"))
        canvas.drawRightString(
            page_width - 46, page_height - 37, "Generated: " + heading["generated"]
        )
        canvas.setStrokeColor(colors.HexColor("#dce3e9"))
        canvas.line(46, page_height - 54, page_width - 46, page_height - 54)
        canvas.line(46, 35, page_width - 46, 35)
        canvas.drawString(46, 22, heading["title"])
        canvas.drawRightString(page_width - 46, 22, f"Page {document.page}")
        canvas.restoreState()

    SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=40,
        rightMargin=40,
        topMargin=66,
        bottomMargin=48,
        title=heading["title"],
        author="Effortrak",
    ).build(story, onFirstPage=page_header, onLaterPages=page_header)
    return output.getvalue()
