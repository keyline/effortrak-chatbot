"""Apply a typed report edit without asking the model to reconstruct its base."""

import re

from fastapi import HTTPException

from app.query.models import Interpretation, QueryPlan
from app.reply import AssistantReply


def choose_project(message, context):
    choices = context.get("project_choices")
    if not choices or not context.get("plan"):
        return None
    text = message.strip()
    identifier = re.fullmatch(r"(?:project\s+(?:id\s*)?|id\s*):?\s*(\d+)", text, re.IGNORECASE)
    option = re.fullmatch(r"(?:option\s+)?([0-9]+)", text, re.IGNORECASE)
    selected = None
    if identifier:
        selected = next((item for item in choices if item["project_id"] == int(identifier[1])), None)
    elif option:
        index = int(option[1]) - 1
        selected = choices[index] if 0 <= index < len(choices) else None
    else:
        matches = [item for item in choices if item["project_name"].strip().casefold() == text.casefold()]
        if len(matches) == 1:
            selected = matches[0]
        else:
            return None
    if selected is None:
        return AssistantReply(context["project_choice_message"], status="clarify", query_context=context)
    plan = QueryPlan.model_validate(context["plan"])
    # Preserve the scope, employee, period, measures and all unrelated filters.
    plan = plan.model_copy(update={"project_id": selected["project_id"], "project_name": None, "offset": 0})
    return Interpretation(action="query", plan=plan, chart=context.get("chart"), export=context.get("export"))


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
            having_group_by=None,
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
        if (key == "group_by" and plan["dataset"] == "attendance" and plan["having"]
            and plan.get("having_group_by") is None and set(change.columns) - set(existing)):
            # Employee-period thresholds must select employees before splitting their dates.
            plan["having_group_by"] = existing.copy()
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
