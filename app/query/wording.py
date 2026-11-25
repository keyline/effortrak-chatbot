"""Business wording for report display; field keys and record values stay intact."""

from app.query.formatting import display

FIELD_LABELS = {
    "is_present": "Present", "is_late": "Late", "is_active": "Active",
    "present_days": "Days present",
    "stage_won": "Won", "stage_lost": "Lost",
}
FIELD_VALUES = {
    "billing": {
        "billable": "Billable", "non_billable": "Non-billable",
        "unclassified": "Unclassified billing",
    },
}


def public_columns(columns):
    return [
        {**column, "label": FIELD_LABELS.get(column["key"], column["label"])}
        for column in columns
    ]


def report_view(data):
    """Build a display copy, keeping query fields and source records intact."""
    data = public_table(data)
    columns = data["columns"]
    if data.get("dataset") != "attendance":
        return data
    plan = data.get("plan") or {}
    filters = plan.get("filters") or []

    def selected(field, value):
        return any(item.get("field") == field and (
            item.get("operator") == "=" and item.get("value") == value
            or item.get("operator") == "in" and item.get("value") == [value]
            or value == 1 and item.get("operator") == "!=" and item.get("value") == 0
        ) for item in filters)

    cohort = (
        "late" if selected("is_late", 1) or selected("status", "Late") else
        "present" if selected("is_present", 1) or selected("status", "Present") else
        "absent" if selected("status", "Absent") else None
    )
    count_label = {"late":"Days late", "present":"Days present", "absent":"Days absent"}.get(cohort,
        "Days" if 'status' in plan.get('group_by', []) else "Days covered")
    columns = [{**col, "label":count_label} if col["key"] == "employee_days" else col for col in columns]
    headcount_label = {"late":"Employees late", "present":"Employees present", "absent":"Employees absent"}.get(cohort, "Employees")
    columns = [{**col, "label":headcount_label} if col["key"] == "employee_count" else col for col in columns]
    # Distinct headcount is redundant only when every displayed employee group is exactly one person.
    per_employee = plan.get("mode") == "aggregate" and bool(set(plan.get("group_by", [])) & {"employee_id", "employee_name"})
    if (per_employee and data.get("rows") and any(col["key"] == "employee_days" for col in columns)
        and all(row.get("employee_count") == 1 for row in data["rows"])
        and not any(item.get("field") == "employee_count" for item in plan.get("having", []) + plan.get("order_by", []))):
        columns = [col for col in columns if col["key"] != "employee_count"]
    title = {"late":"Late arrivals", "present":"Present employees", "absent":"Absent employees"}.get(cohort)
    return {**data, "columns":columns, **({"display_title":title} if title else {})}


def public_text(text):
    # Exact legacy note clauses also cover saved replies and queued exports.
    for old, new in {
        "Presence (is_present=1) means punch-in exists": "Employees are counted as present once they punch in",
        "Lateness (is_late=1) uses the assigned arrival cutoff independently of punch-out.": "Late arrivals are based on the assigned arrival cutoff, regardless of punch-out.",
        "filter status and is_active for open work": "ask for open follow-ups to see only active, pending work",
        "Status IDs are stored application statuses.": "Task statuses follow the records in Effortrak.",
    }.items():
        text = text.replace(old, new)
    return text


def public_rows(columns, rows):
    fields = {col["key"]: FIELD_VALUES[col["key"]] for col in columns if col["key"] in FIELD_VALUES}
    if not fields:
        return rows
    return [
        {**row, **{key: labels.get(row[key], row[key]) for key, labels in fields.items() if key in row}}
        for row in rows
    ]


def public_table(table):
    return {**table, "columns": public_columns(table["columns"]),
            "rows": public_rows(table["columns"], table["rows"])}


def public_chart(chart, tables=()):
    # Saved charts have no field keys; match their source table before relabeling values.
    for table in tables:
        dimensions = [col for col in table["columns"]
                      if col.get("type") not in ("number", "integer") and not col["key"].endswith("_id")]
        if not any(col["key"] in FIELD_VALUES for col in dimensions):
            continue

        def category_labels(rows):
            return [" / ".join(display(row.get(col["key"])) for col in dimensions)[:250] or "Total" for row in rows]

        if chart["labels"] == category_labels(table["rows"]):
            chart = {**chart, "labels": category_labels(public_rows(dimensions, table["rows"]))}
            break
    labels = {
        label: friendly
        for key, friendly in FIELD_LABELS.items()
        for label in (key, key.replace("_", " ").title(), key.replace("_", " ").capitalize())
    }
    return {
        **chart,
        "series": [{**series, "name": labels.get(series["name"], series["name"])} for series in chart["series"]],
        "note": public_text(chart.get("note", "")),
    }
