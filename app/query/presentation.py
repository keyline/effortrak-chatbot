"""Render verified results without a second model call or numerical guessing."""

from datetime import date

from app.reply import AssistantReply
from app.schemas import ReportChart
from app.query.formatting import display, hour_totals
from app.query.wording import public_columns, report_view

REPORT_TITLES = {
    "employees": "Employee details", "attendance": "Attendance", "effort": "Booked effort",
    "tasks": "Tasks", "leave": "Leave", "assets": "Assets", "asset_assignments": "Asset assignments",
    "lms_leads": "Leads", "lms_followups": "Lead follow-ups", "leave_balance": "Leave balance",
    "work_summary": "Work summary", "work_report": "Work summary", "late_punchin": "Late arrivals",
    "punch_locations": "Attendance locations", "visits": "Client visits", "location": "Last recorded location",
    "lms_location": "Lead locations", "last_location": "Last recorded location",
    "client_locations": "Client locations", "work_arrangements": "Work arrangements",
}


def label(value):
    return REPORT_TITLES.get(value, value.replace("_", " ").capitalize())


def period_text(period):
    try:
        start, end = (date.fromisoformat(period[key]) for key in ("start", "end"))
    except (ValueError, TypeError, KeyError):
        return ""
    def readable(day):
        return f"{day.day} {day:%B %Y}"
    if start == end:
        return " on " + readable(start)
    if (start.year, start.month) == (end.year, end.month):
        return f" from {start.day} to {end.day} {end:%B %Y}"
    return f" from {readable(start)} to {readable(end)}"


def present(data, selection, chart_type=None):
    data = report_view(data)
    period = data.get("period", {})
    title = data.get("display_title") or label(data["dataset"])
    columns = public_columns(data["columns"])
    rows = [
        {col["key"]: row.get(col["key"]) for col in columns} for row in data["rows"]
    ]
    single_summary = len(rows) == 1 and all(col["type"] in ("integer", "number") for col in columns)
    scope = data.get("scope") or (selection.get("plan") or selection.get("specialized") or {}).get("scope")
    project = (data.get("project") or {}).get("project_name")
    context = (" for " + project if project else "") + period_text(period)
    heading = "Your " + title.lower() if scope == "self" else title
    lines = [f"{heading}{context}."]
    order = (data.get("plan") or {}).get("order_by") or []
    if (rows and data.get("display_title") == "Late arrivals" and order
        and order[0] == {"field":"employee_days", "direction":"desc"}
        and "date" not in (data.get("plan") or {}).get("group_by", [])
        and any(col["key"] == "employee_name" for col in columns)
        and any(col["key"] == "employee_days" for col in columns)):
        lines.append("Employees are ranked by the number of days they arrived late.")
    employee_page = data.get("employee_pagination")
    if rows and employee_page:
        count = employee_page['count']
        summary = f"{count} {'employee' if count == 1 else 'employees'}"
        if data.get('has_more') or data.get('offset'):
            summary = f"Employees {employee_page['offset'] + 1}–{employee_page['offset'] + count} of {employee_page['total']}"
        lines.append(f"{summary} · {len(rows)} records" + (". Ask for the next page to see more employees." if data.get('has_more') else "."))
    if not rows:
        lines = [f"I couldn't find any matching {title.lower()} records{context}. Please try a different date range or selection."]
    elif single_summary:
        lines.extend(
            f"{col['label']}: {display(rows[0].get(col['key']), col.get('unit', ''))}" for col in columns
        )
    if not single_summary and not employee_page:
        lines.extend(f"{name.capitalize()}: {display(value, 'hours')}" for name, value in hour_totals(data))
    if rows and not employee_page and (data.get("has_more") or data.get("offset")):
        qualifier = "at least " if data.get("total_is_exact") is False else ""
        lines.append(f"Showing results {data['offset'] + 1} to {data['offset'] + len(rows)} of {qualifier}{data['total_rows']}." + (" Ask for the next page to see more." if data.get("has_more") else ""))
    if any(row.get(col["key"]) is None for row in rows for col in columns if col["type"] in ("integer", "number")):
        lines.append("Some values aren't available, so a complete total may not be possible.")
    if rows and data["dataset"] == "attendance" and any(col["key"] == "attendance_hours" for col in columns):
        lines.append("Hours may be incomplete when a punch-out is missing.")
    if rows and data["dataset"] == "leave" and any(col["key"] in ("days", "total_days", "leave_days") for col in columns):
        lines.append("Leave-day totals cover each full leave request.")
    charts, chart_notice = charts_for(data, chart_type) if rows else ([], "")
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
    identifiers = data.get("row_employee_ids")
    if identifiers and len(identifiers) == len(rows) and len(columns) > 1 and all(type(value) is int and value > 0 for value in identifiers):
        groups = {}
        for index, (identifier, row) in enumerate(zip(identifiers, rows)):
            if not isinstance(row.get("employee_name"), str) or not row["employee_name"].strip():
                groups = {}
                break
            group = groups.setdefault(identifier, {"employee_id": identifier, "name": row["employee_name"], "row_indices": []})
            group["row_indices"].append(index)
        if any(len(group["row_indices"]) > 1 for group in groups.values()):
            table["employee_groups"] = list(groups.values())
    if employee_page:
        table["employee_pagination"] = employee_page
    if table.get("employee_groups"):
        note = "Hours may be incomplete when a punch-out is missing."
        if note in lines:
            lines.remove(note)
            if employee_page:
                totals = [f"{name.capitalize()}: {display(value, 'hours')}" for name, value in hour_totals(data)]
                table["summary"] = "\n".join(totals + [note])
            else:
                table["note"] = note
    return AssistantReply(
        "\n".join(lines), query_context=selection, charts=charts, tables=[table] if rows else []
    )


def charts_for(data, kind):
    if not kind:
        return [], ""
    data = report_view(data)
    if data.get("has_more") or data.get("offset") or len(data["rows"]) > 50:
        return (
            [],
            "To chart the complete result, please group the report or select up to 50 rows.",
        )
    numeric = [
        column
        for column in public_columns(data["columns"])
        if column["type"] in ("number", "integer") and not column["key"].endswith("_id")
    ]
    dimensions = [
        column
        for column in public_columns(data["columns"])
        if column not in numeric and not column["key"].endswith("_id")
    ]
    if not numeric or not data["rows"]:
        return [], "Please choose a report with numeric values and matching records to create a chart."
    if kind == "pie" and len(numeric) != 1:
        return [], "Please choose one value, such as booked hours, for the pie chart."
    if (
        kind == "pie"
        and numeric[0]["key"] == "employee_count"
        and data.get("dataset") != "employees"
    ):
        return (
            [],
            "An employee may appear in more than one category. Please use a bar chart for this employee count.",
        )
    measures = numeric[:1] if kind == "pie" else numeric[:5]
    if len({col.get("unit", "") for col in measures}) > 1:
        return [], "Please choose values with the same unit for this chart, such as hours only."
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
            "Some values are missing or can't be charted. You can still view the results in the table.",
        )
    if kind == "pie" and (not dimensions or not any(series[0]["values"])):
        return [], "Please choose a report with categories and a total greater than zero for the pie chart."
    labels = [
        " / ".join(display(row.get(col["key"])) for col in dimensions)[:250] or "Total"
        for row in data["rows"]
    ]
    if kind == "pie" and len(set(labels)) != len(labels):
        return (
            [],
            "Please group the results by category before requesting a pie chart.",
        )
    spec = {
        "type": kind,
        "title": data.get("display_title") or label(data["dataset"]),
        "subtitle": "",
        "unit": measures[0].get("unit") or "records",
        "labels": labels,
        "series": series,
        "note": "",
    }
    return [ReportChart.model_validate(spec).model_dump()], ""
