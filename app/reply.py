"""Presentation metadata stays local; only query selections reach the model."""

import json
from app.messages import friendly_message
from app.query.wording import public_text


class AssistantReply(str):
    def __new__(
        cls,
        content,
        *,
        model_content=None,
        query_context=None,
        charts=None,
        tables=None,
        status="answered",
        export_job=None,
    ):
        reply = super().__new__(cls, public_text(friendly_message(content)))
        reply.model_content = (
            model_content or "The backend handled this question locally."
        )
        reply.query_context = query_context
        reply.charts = charts or []
        reply.tables = tables or []
        reply.status = status
        reply.export_job = export_job
        return reply


def trusted_query(history):
    for row in reversed(history):
        if row["role"] == "assistant":
            try:
                value = row.get("query_context") or "{}"
                parsed = json.loads(value) if isinstance(value, str) else value
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
    return {}
