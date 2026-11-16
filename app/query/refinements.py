"""Apply a typed report edit without asking the model to reconstruct its base."""

from fastapi import HTTPException

from app.query.models import QueryPlan


def apply_refinement(selection, context, catalogue=None):
    if selection.action != "refine":
        return selection
    prior = context.get("plan") if context.get("kind") == "nl_query" else None
    if not isinstance(prior, dict):
        return selection.model_copy(
            update={
                "action": "clarify",
                "clarification": "selection",
                "plan": None,
                "refinement": None,
            }
        )
    plan = QueryPlan.model_validate(prior).model_dump(mode="json")
    change = selection.refinement
    operation = change.operation
    plan["offset"] = 0
    if operation == "aggregate":
        plan.update(
            mode="aggregate",
            fields=[],
            metrics=change.metrics,
            group_by=change.group_by,
            having=[],
            order_by=[item.model_dump() for item in change.order_by],
        )
    elif operation == "sort":
        plan["order_by"] = [item.model_dump() for item in change.order_by]
    elif operation == "columns":
        # Adding dimensions to a summary must not change its measures; adding
        # detail columns must not silently turn the report into an aggregate.
        key = "fields" if plan["mode"] == "details" else "group_by"
        existing = plan[key]
        if key == "fields" and not existing:
            existing = (
                (catalogue or {})
                .get("datasets", {})
                .get(plan["dataset"], {})
                .get("default_fields", [])
            )
            if not existing:
                return selection.model_copy(
                    update={
                        "action": "clarify",
                        "clarification": "selection",
                        "plan": None,
                        "refinement": None,
                    }
                )
        plan[key] = list(dict.fromkeys(existing + change.columns))
    elif operation == "filter":
        plan["filters"] = [item.model_dump() for item in change.filters]
    elif operation == "period":
        plan.update(
            start_date=change.start_date.isoformat(),
            end_date=change.end_date.isoformat(),
        )
    elif operation == "page":
        previous = prior.get("offset", 0)
        plan["offset"] = (
            max(0, previous + plan["limit"] * (1 if change.direction == "next" else -1))
            if change.direction != "first"
            else 0
        )
        if plan["offset"] > 10000:
            raise HTTPException(
                422, "Narrow the reporting selection before requesting more pages."
            )
    return selection.model_copy(
        update={
            "action": "query",
            "plan": QueryPlan.model_validate(plan),
            "refinement": None,
            "specialized": None,
        }
    )
