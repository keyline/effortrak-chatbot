"""Existing business readers, selected by meaning rather than question regexes."""


async def execute(backend, identity, plan):
    args = plan.model_dump(mode="json")
    common = {
        key: args[key] for key in ("scope", "employee_id", "start_date", "end_date")
    }
    if args["employee_name"] and plan.kind not in (
        "visits",
        "last_location",
        "client_locations",
        "lms_location",
    ):
        module = "leave_balance" if plan.kind == "leave_balance" else "attendance"
        found = await backend.call(
            "employee-search",
            identity.credential,
            {"scope": plan.scope, "name": plan.employee_name, "module": module},
        )
        if len(found["matches"]) != 1 or found["has_more"]:
            return {
                "status": "clarify",
                "message": "Please provide a unique employee name or ID within your access.",
            }, None
        common["employee_id"] = found["matches"][0]["employee_id"]
    if plan.kind == "leave_balance":
        body = {
            key: args[key]
            for key in (
                "scope",
                "financial_year",
                "leave_type",
                "limit",
                "balance_filter",
                "ranking",
            )
        }
        body["employee_id"] = common["employee_id"]
        endpoint = "leave-balance"
    elif plan.kind in ("late_punchin", "work_arrangements", "punch_locations"):
        body = {
            **common,
            "report": "locations" if plan.kind == "punch_locations" else plan.kind,
        }
        if plan.kind == "late_punchin":
            body.update(
                punch_basis=plan.punch_basis,
                punch_after=plan.punch_after,
                min_delay_minutes=plan.min_delay_minutes,
                threshold=0,
            )
        elif plan.kind == "work_arrangements":
            body.update(start_date=None, end_date=None)
        endpoint = "manager-report"
    elif plan.kind == "work_summary":
        body = {**common, "include_daily": False, "detailed_review": False}
        endpoint = "work-report"
    else:
        body = {
            **common,
            "employee_name": args["employee_name"],
            "client_name": args["client_name"],
            "action": {"last_location": "location", "client_locations": "visits"}.get(
                plan.kind, plan.kind
            ),
            "latest": plan.kind == "last_location",
            "page": plan.page,
        }
        if body["employee_id"] is not None:
            body["employee_name"] = None
        endpoint = "visit-location"
    data = await backend.call(endpoint, identity.credential, body)
    if data.get("status") == "clarify":
        return {
            "status": "clarify",
            "message": data.get("message", "Please narrow the reporting selection."),
        }, None
    rows = normalize(plan.kind, data)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    columns = [
        {
            "key": key,
            "label": key.replace("_", " ").capitalize(),
            "type": "number"
            if any(
                isinstance(row.get(key), (int, float))
                and not isinstance(row.get(key), bool)
                for row in rows
            )
            else "string",
            "unit": "hours"
            if key.endswith("_hours")
            else "days"
            if key.endswith("_days") or key == "days"
            else "percent"
            if key == "average_activity"
            else "records",
        }
        for key in fields
    ]
    total = data.get("total_count", data.get("matched_count", len(rows)))
    if not isinstance(total, int) or total < len(rows):
        total = len(rows)
    result = {
        "status": "answered",
        "dataset": plan.kind,
        "columns": columns,
        "rows": rows[:100],
        "total_rows": total,
        "offset": max(0, (plan.page - 1) * 50),
        "limit": 100,
        "has_more": data.get("has_more", False)
        or data.get("details_truncated", False)
        or len(rows) > 100,
        "period": {
            "start": data.get("start_date", common["start_date"]),
            "end": data.get("end_date", common["end_date"]),
        },
        "notice": data.get(
            "notice",
            "Recorded data within your current access. Missing tracking is not poor performance.",
        ),
    }
    return result, {"endpoint": endpoint, "arguments": body}


def normalize(kind, data):
    if kind == "leave_balance":
        result = []
        for group in data.get("groups", []):
            for row in group.get("rows", []):
                result.append(
                    {
                        key: row.get(key)
                        for key in (
                            "employee_id",
                            "employee_name",
                            "allocated_days",
                            "used_days",
                            "remaining_days",
                        )
                    }
                    | {"leave_type": group.get("leave_type")}
                )
        return result
    if kind == "work_summary":
        return [
            {
                "employee_id": row["employee_id"],
                "employee_name": row["employee_name"],
                **{
                    name: row["summary"].get(name)
                    for name in ("days", "average_activity", "screenshot_count")
                },
                **{
                    key: None
                    if row["summary"].get(source) is None
                    else round(row["summary"][source] / divisor, 4)
                    for key, source, divisor in [
                        ("attendance_hours", "attendance_minutes", 60),
                        ("productive_hours", "productive_seconds", 3600),
                        ("idle_hours", "idle_seconds", 3600),
                        ("tracked_hours", "tracked_seconds", 3600),
                    ]
                },
            }
            for row in data.get("rows", [])
        ]
    # Explicit allowlist: free-text task descriptions, visit notes, credentials,
    # reasons and arbitrary new backend fields can never enter the answer/export.
    allowed = {
        "employee_id",
        "employee_name",
        "date",
        "count",
        "arrangement",
        "client_name",
        "visit_date",
        "visit_time",
        "recorded_at",
        "latitude",
        "longitude",
        "address",
        "source",
        "punch_time",
        "punch_in_time",
        "punch_out_time",
        "punch_in_address",
        "punch_out_address",
        "delay_minutes",
        "accuracy_m",
        "is_mock",
    }
    rows = []
    for row in data.get("rows", []):
        out = {
            key: value
            for key, value in row.items()
            if key in allowed and isinstance(value, (str, int, float, type(None)))
        }
        if "dates" in row:
            out["dates"] = ", ".join(str(value) for value in row["dates"][:93])
        location = row.get("location")
        if isinstance(location, dict):
            for key in ("address", "latitude", "longitude"):
                value = location.get(key)
                if value is None or isinstance(value, (str, int, float)):
                    out[key] = value
            coordinates = location.get("coordinates")
            if isinstance(coordinates, (list, tuple)) and len(coordinates) == 2:
                out["latitude"], out["longitude"] = coordinates
            elif isinstance(coordinates, dict):
                for key in ("latitude", "longitude"):
                    if isinstance(coordinates.get(key), (int, float)):
                        out[key] = coordinates[key]
        if kind == "late_punchin" and row.get("entries"):
            for entry in row["entries"]:
                rows.append(
                    {key: row.get(key) for key in ("employee_id", "employee_name")}
                    | {
                        key: entry.get(key)
                        for key in ("date", "punch_in", "late_minutes")
                    }
                )
            continue
        if out:
            rows.append(out)
    return rows
