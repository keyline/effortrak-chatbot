"""Recheck saved results whose row ownership can change without a role change."""

import json

from fastapi import HTTPException

from app.query.models import SpecializedPlan
from app.query.presentation import present
from app.query.specialized import execute


async def refresh_report_history(rows, identity, backend):
    checked = {}
    messages = []
    for row in rows:
        context = json.loads(row.get("query_context") or "null") or {}
        plan = context.get("plan") or {}
        specialized = context.get("specialized") or {}
        ownership_dependent = (
            plan.get("scope") == "self"
            and plan.get("dataset")
            in ("assets", "asset_assignments", "lms_leads", "lms_followups")
        ) or specialized.get("kind") == "lms_location"
        tables = json.loads(row.get("tables") or "[]")
        if row["role"] == "assistant" and tables and ownership_dependent:
            key = json.dumps(plan or specialized, sort_keys=True)
            if key not in checked:
                try:
                    if plan:
                        data = await backend.call(
                            "query", identity.credential, {"plan": plan}
                        )
                    else:
                        data, _ = await execute(
                            backend,
                            identity,
                            SpecializedPlan.model_validate(specialized),
                        )
                    checked[key] = (
                        present(data, context).tables
                        if data.get("status") == "answered"
                        else None
                    )
                except HTTPException as exc:
                    if exc.status_code not in (403, 404, 409, 410, 413, 422):
                        raise
                    checked[key] = None
            if checked[key] != tables:
                # Remove summaries and charts too; both can contain old record values.
                row = {
                    **row,
                    "content": "This saved report's data or access has changed. Ask for the report again to view your current authorized records.",
                    "tables": "[]",
                    "charts": "[]",
                    "status": "clarify",
                }
        messages.append(row)
    return messages
