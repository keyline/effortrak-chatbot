"""Reject mismatched or unexpectedly expanded backend report responses."""

import math

from fastapi import HTTPException

from app.query.models import QueryPlan


def validate_result(plan, data, catalogue, *, exporting=False):
    try:
        resolved = QueryPlan.model_validate(data["plan"])
        before, after = plan.model_dump(), resolved.model_dump()
        for key in (
            "employee_name",
            "project_name",
            "employee_id",
            "project_id",
            "fields",
        ):
            before.pop(key)
            after.pop(key)
        if before != after:
            raise ValueError("Changed query")
        for subject in ("employee", "project"):
            identifier, name = subject + "_id", subject + "_name"
            if getattr(plan, identifier) is not None and getattr(
                resolved, identifier
            ) != getattr(plan, identifier):
                raise ValueError("Changed explicit subject")
            if getattr(plan, name) is None:
                if (
                    getattr(resolved, identifier) != getattr(plan, identifier)
                    or getattr(resolved, name) is not None
                ):
                    raise ValueError("Changed subject")
            elif (
                getattr(resolved, identifier) is None
                or getattr(resolved, name) is not None
            ):
                raise ValueError("Unresolved subject")
        if resolved.mode == "details":
            expected = (
                plan.fields or catalogue["datasets"][plan.dataset]["default_fields"]
            )
            if resolved.fields != expected:
                raise ValueError("Changed fields")
        project = data.get("project")
        if project is not None and (
            not isinstance(project, dict)
            or set(project) != {"project_id", "project_name"}
            or type(project["project_id"]) is not int
            or project["project_id"] != resolved.project_id
            or not isinstance(project["project_name"], str)
            or not 1 <= len(project["project_name"]) <= 500
        ):
            raise ValueError("Invalid project heading")
        outputs = (
            resolved.fields
            if resolved.mode == "details"
            else resolved.group_by + resolved.metrics
        )
        row_employees = data.get("row_employee_ids")
        if row_employees is not None and (
            resolved.mode != "details" or "employee_name" not in outputs
            or not isinstance(row_employees, list) or len(row_employees) != len(data["rows"])
            or any(value is not None and (type(value) is not int or value <= 0) for value in row_employees)
        ):
            raise ValueError("Invalid employee grouping")
        employee_page = data.get("employee_pagination")
        if employee_page is not None:
            days = (plan.end_date - plan.start_date).days + 1 if plan.start_date and plan.end_date else 0
            if (exporting or plan.dataset != "attendance" or resolved.mode != "details" or days < 2
                or len(outputs) < 2 or row_employees is None
                or any(type(value) is not int for value in row_employees)
                or not isinstance(employee_page, dict)
                or set(employee_page) != {"total", "count", "offset", "limit"}
                or any(type(value) is not int or value < 0 for value in employee_page.values())
                or employee_page["offset"] != plan.offset or employee_page["limit"] != plan.limit
                or employee_page["count"] != len(set(row_employees)) or employee_page["count"] > plan.limit
                or employee_page["total"] < employee_page["count"] or employee_page["total"] > data["total_rows"]
                or employee_page["count"] != min(plan.limit, max(0, employee_page["total"] - plan.offset))
                or len(data["rows"]) > min(1550, plan.limit * days)
                or any(row_employees.count(value) > days for value in set(row_employees))):
                raise ValueError("Invalid employee pagination")
        page_count = employee_page["count"] if employee_page is not None else len(data["rows"])
        page_total = employee_page["total"] if employee_page is not None else data["total_rows"]
        row_limit = plan.limit * days if employee_page is not None else (5000 if exporting else plan.limit)
        if (
            data["dataset"] != plan.dataset
            or data["scope"] != plan.scope
            or [column["key"] for column in data["columns"]] != outputs
            or any(set(row) - set(outputs) for row in data["rows"])
        ):
            raise ValueError("Unexpected fields or scope")
        if (
            type(data["total_rows"]) is not int
            or data["total_rows"] < len(data["rows"])
            or type(data["offset"]) is not int
            or data["offset"] != (0 if exporting else plan.offset)
            or type(data["has_more"]) is not bool
            or data["has_more"]
            != (data["offset"] + page_count < page_total)
            or len(data["rows"]) > row_limit
        ):
            raise ValueError("Invalid pagination")
        for row in data["rows"]:
            for column in data["columns"]:
                value = row.get(column["key"])
                if value is None:
                    continue
                if column["type"] in ("number", "integer"):
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise ValueError("Invalid numeric value")
                elif not isinstance(value, str):
                    raise ValueError("Invalid text value")
        period = data["period"]
        if period != {
            "start": plan.start_date.isoformat() if plan.start_date else None,
            "end": plan.end_date.isoformat() if plan.end_date else None,
        }:
            raise ValueError("Changed period")
        return resolved
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(
            502, "The backend returned an invalid or different reporting selection."
        ) from None
