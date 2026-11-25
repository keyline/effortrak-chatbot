"""Prepare shared reporting contracts once per business catalogue."""

import json
from collections import Counter
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
    return _shared_schema(compact_interpretation_schema(json.loads(catalogue_json)))


def _shared_schema(schema):
    """Replace repeated schemas with local refs without changing constraints."""
    counts = Counter()

    def key(node):
        if isinstance(node, dict) and (isinstance(node.get('type'), str) or 'anyOf' in node):
            value = json.dumps(node, sort_keys=True, separators=(',', ':'))
            return value if len(value) >= 60 else None

    def prepare(node):
        if isinstance(node, dict):
            if isinstance(node.get('type'), str) or 'anyOf' in node or '$ref' in node:
                node.pop('title', None)
            for value in node.values():
                prepare(value)
            if identity := key(node):
                counts[identity] += 1
        elif isinstance(node, list):
            for value in node:
                prepare(value)

    prepare(schema)
    names, definitions = {}, {}

    def share(node, defining=False):
        identity = key(node)
        if not defining and identity and counts[identity] > 1:
            if identity not in names:
                name = names[identity] = f'Shared{len(names) + 1}'
                definitions[name] = share(node, defining=True)
            return {'$ref': '#/$defs/' + names[identity]}
        if isinstance(node, dict):
            return {name: share(value) for name, value in node.items()}
        if isinstance(node, list):
            return [share(value) for value in node]
        return node

    result = share(schema)
    result['$defs'].update(definitions)
    return result


def interpretation_prompt_catalogue(catalogue):
    """Keep every dataset, typed field, unit and rule without repeated labels."""
    result = deepcopy(catalogue)
    for metadata in result.get('datasets', {}).values():
        if metadata.get('notice') == metadata.get('description'):
            metadata.pop('notice', None)
        for source, target in [('fields', 'fields_by_type'), ('metrics', 'metrics_by_unit')]:
            values = metadata.get(source)
            if isinstance(values, dict) and all(isinstance(value, str) for value in values.values()):
                grouped = {}
                for name, value in values.items():
                    grouped.setdefault(value, []).append(name)
                metadata[target] = grouped
                del metadata[source]
    return result


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
            if value is not None and (value != [] or key == "having_group_by")
        }
    return result
