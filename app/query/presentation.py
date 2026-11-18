"""Render verified results without a second model call or numerical guessing."""

from app.reply import AssistantReply
from app.schemas import ReportChart
from app.query.formatting import display, hour_totals


def label(value):
    return value.replace("_", " ").capitalize()


def present(data, selection, chart_type=None):
    period = data.get("period", {})
    title = label(data["dataset"])
    if period.get("start"):
        title += f" | {period['start']} to {period['end']}"
    columns = data["columns"]
    rows = [
        {col["key"]: row.get(col["key"]) for col in columns} for row in data["rows"]
    ]
    lines = [title]
    if data.get("project"):
        lines.append("Project: " + data["project"]["project_name"])
    if not rows:
        lines.append(
            "No matching records were found for this selection within your access."
        )
    elif len(rows) == 1 and all(
        col["type"] in ("integer", "number") for col in columns
    ):
        lines.extend(
            f"{col['label']}: {display(rows[0].get(col['key']), col.get('unit', ''))}" for col in columns
        )
    lines.extend(f"{name}: {display(value, 'hours')}" for name, value in hour_totals(data))
    qualifier = "at least " if data.get("total_is_exact") is False else ""
    lines.append(f"Showing {len(rows)} of {qualifier}{data['total_rows']} result rows.")
    if data.get("has_more"):
        lines.append("Ask for the next page to view more records.")
    if data.get("notice"):
        lines.append(data["notice"])
    charts, chart_notice = charts_for(data, chart_type)
    if chart_notice:
        lines.append(chart_notice)
    table = {
        "title": title,
        "columns": columns,
        "rows": rows,
        "total_rows": data["total_rows"],
        "offset": data["offset"],
        "has_more": data.get("has_more", False),
    }
    return AssistantReply(
        "\n".join(lines), query_context=selection, charts=charts, tables=[table]
    )


def charts_for(data, kind):
    if not kind:
        return [], ""
    if data.get("has_more") or data.get("offset") or len(data["rows"]) > 50:
        return (
            [],
            "Narrow or group this report to at most 50 rows to chart the complete result.",
        )
    numeric = [
        column
        for column in data["columns"]
        if column["type"] in ("number", "integer") and not column["key"].endswith("_id")
    ]
    dimensions = [
        column
        for column in data["columns"]
        if column not in numeric and not column["key"].endswith("_id")
    ]
    if not numeric or not data["rows"]:
        return [], "A chart needs numeric measures and matching records."
    if kind == "pie" and len(numeric) != 1:
        return [], "Choose one numeric measure for a pie chart."
    if (
        kind == "pie"
        and numeric[0]["key"] == "employee_count"
        and data.get("dataset") != "employees"
    ):
        return (
            [],
            "Use a bar chart for distinct employee counts; employees may appear in multiple categories.",
        )
    measures = numeric[:1] if kind == "pie" else numeric[:5]
    if len({col.get("unit", "") for col in measures}) > 1:
        return [], "Choose measures with the same unit for a chart."
    series = [
        {"name": col["label"], "values": [row.get(col["key"]) for row in data["rows"]]}
        for col in measures
    ]
    if any(
        v is None or not isinstance(v, (int, float)) or v < 0
        for item in series
        for v in item["values"]
    ):
        return (
            [],
            "Charts require complete, nonnegative numeric values. The table retains the results.",
        )
    if kind == "pie" and (not dimensions or not any(series[0]["values"])):
        return [], "A pie chart needs categories and a positive total."
    labels = [
        " / ".join(display(row.get(col["key"])) for col in dimensions)[:250] or "Total"
        for row in data["rows"]
    ]
    if kind == "pie" and len(set(labels)) != len(labels):
        return (
            [],
            "Group the result into distinct categories before requesting a pie chart.",
        )
    spec = {
        "type": kind,
        "title": label(data["dataset"]),
        "subtitle": "",
        "unit": measures[0].get("unit") or "records",
        "labels": labels,
        "series": series,
        "note": "Complete authorized result for this selection.",
    }
    return [ReportChart.model_validate(spec).model_dump()], ""
