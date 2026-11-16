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
        outputs = (
            resolved.fields
            if resolved.mode == "details"
            else resolved.group_by + resolved.metrics
        )
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
            != (data["offset"] + len(data["rows"]) < data["total_rows"])
            or len(data["rows"]) > (5000 if exporting else plan.limit)
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
