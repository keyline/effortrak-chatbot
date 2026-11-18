"""Prepare shared reporting contracts once per business catalogue."""

import json
from copy import deepcopy
from functools import lru_cache

from app.query.models import compact_interpretation_schema


def interpretation_prompt_schema(catalogue):
    """Cache metadata only; return an independent shared strict schema.

    All datasets remain available. No identity, question, conversation or
    business records enter this bounded cache.
    """
    key = json.dumps(catalogue, ensure_ascii=False, separators=(",", ":"))
    return deepcopy(_cached_schema(key))


@lru_cache(maxsize=8)
def _cached_schema(catalogue_json):
    return compact_interpretation_schema(json.loads(catalogue_json))


def interpretation_prompt_context(context):
    """Remove empty plan placeholders, keeping the actual cohort easy to see.

    The complete trusted context is still used to apply typed refinements.
    No selected value, filter, date, paging value or chart is removed here.
    """
    if context.get("kind") != "nl_query":
        return {}
    result = deepcopy(context)
    # Authorized choice names stay in the chat, never in a provider prompt.
    result.pop("project_choices", None)
    result.pop("project_choice_message", None)
    plan = result.get("plan")
    if isinstance(plan, dict):
        result["plan"] = {
            key: value for key, value in plan.items()
            if value is not None and value != []
        }
    return result
