"""Metered interpretation with one validation retry; records never enter prompts."""

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from app.query.models import (
    Interpretation,
    InterpretationEnvelope,
    QueryPlan,
)
from app.query.prompt_schema import (
    interpretation_prompt_catalogue,
    interpretation_prompt_context,
    interpretation_prompt_schema,
)
from app.query.refinements import apply_refinement
from app.query.subjects import bind_subject

from app.query.instructions import INSTRUCTIONS


async def interpret(service, message, context, identity, catalogue):
    recency = attendance_recency(message)
    if recency:
        # A new recency request must not inherit a previous late cohort/date range.
        pending = context.get('pending_question')
        context = {'kind': 'nl_query', 'pending_question': pending, 'plan': None} if isinstance(pending, str) else {}
        if not re.search(r'\b(?:present|absent|late|punch(?:ed)?[\s-]+in|clock(?:ed)?[\s-]+in)\b', message, re.I):
            return Interpretation(action='clarify', clarification='attendance_recency')
    feedback = None
    for attempt in range(2):
        try:
            selection = await _interpret_once(
                service, message, context, identity, catalogue, feedback
            )
            if recency and selection.action in ('query', 'specialized'):
                plan = selection.plan
                if selection.action == 'specialized' or not (
                    plan.dataset == 'attendance' and plan.mode == 'aggregate'
                    and 'date' in plan.group_by and plan.limit == 1
                    and plan.order_by and plan.order_by[0].field == 'date'
                    and plan.order_by[0].direction == 'desc'
                ):
                    return Interpretation(action='clarify', clarification='attendance_recency')
                required = []
                if re.search(r'\b(?:present|punch(?:ed)?[\s-]+in|clock(?:ed)?[\s-]+in)\b', message, re.I):
                    required.append(('is_present', 1))
                if re.search(r'\babsent\b', message, re.I):
                    required.extend([('is_present', 0), ('status', 'Absent')])
                if re.search(r'\blate\b', message, re.I):
                    required.append(('is_late', 1))
                filters = [(item.field, item.value) for item in plan.filters if item.operator == '=']
                if any(condition not in filters for condition in required) or (
                    not re.search(r'\blate\b', message, re.I)
                    and any(item.field == 'is_late' or (item.field == 'status' and item.value == 'Late') for item in plan.filters)
                ):
                    return Interpretation(action='clarify', clarification='attendance_recency')
            return selection
        except ValidationError as error:
            # Log error locations/types only: no question, values or model output.
            issues = error.errors(
                include_input=False, include_context=False, include_url=False
            )
            logging.getLogger(__name__).warning(
                "interpretation_validation_failed attempt=%s issues=%s",
                attempt + 1,
                [
                    (issue["loc"], issue["type"], _validation_rule(issue))
                    for issue in issues
                ],
            )
            feedback = [
                {"path": list(issue["loc"]), "reason": issue["msg"]} for issue in issues
            ]
    period_issue = any(item['reason'].startswith('Value error, Specialized ') for item in (feedback or []))
    return Interpretation(action="clarify", clarification="period" if period_issue else "request")


def attendance_recency(message):
    """Distinguish latest matching events from relative calendar periods."""
    recent = re.search(
        r'\b(?:latest|most\s+recent|last(?!\s+(?:\d+\s+)?(?:days?|weeks?|months?|years?|quarters?|'
        r'monday|tuesday|wednesday|thursday|friday|saturday|sunday|night)\b))\b', message, re.I)
    return bool(recent and re.search(
        r'\b(?:attendance|present|absent|punch(?:ed)?[\s-]+in|clock(?:ed)?[\s-]+in)\b', message, re.I))


async def _interpret_once(service, message, context, identity, catalogue, feedback):
    # These non-reasoning model families support sampling control. Keep other
    # models' native parameters rather than sending an unsupported temperature.
    sampling = (
        {"temperature": 0}
        if service.settings.openai_model.startswith(("gpt-4.1", "gpt-4o"))
        or (service.settings.openai_model == 'gpt-6-luna'
            and getattr(service.settings, 'openai_reasoning_effort', None) == 'none')
        else {}
    )
    result = await service.create_response(
        identity,
        "nl_sql_interpretation",
        model=service.settings.openai_model,
        instructions=INSTRUCTIONS,
        input=[
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "current_date": datetime.now(ZoneInfo("Asia/Kolkata"))
                        .date()
                        .isoformat(),
                        "scopes": list(identity.scopes),
                        "report_scopes": identity.report_scopes,
                        "modules": list(identity.modules),
                        "authenticated_role": identity.designation,
                        "field_access": _field_access(catalogue, identity.designation),
                        "catalogue": interpretation_prompt_catalogue(catalogue),
                        "previous_selection": interpretation_prompt_context(context),
                        "validation_feedback": feedback,
                        "question": message,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        ],
        tools=[
            {
                "type": "function",
                "name": "interpret_query",
                "description": "Propose a governed reporting selection.",
                "parameters": interpretation_prompt_schema(catalogue),
                "strict": True,
            }
        ],
        tool_choice={"type": "function", "name": "interpret_query"},
        parallel_tool_calls=False,
        max_output_tokens=3500,
        store=False,
        **sampling,
    )
    calls = [item for item in result.output if item.type == "function_call"]
    if getattr(result, "status", "completed") != "completed":
        raise HTTPException(502, "AI interpretation is unavailable or timed out. Please retry.")
    if len(calls) != 1 or calls[0].name != "interpret_query":
        return Interpretation(action="clarify", clarification="request")
    value = TypeAdapter(dict[str, object]).validate_json(calls[0].arguments)
    envelope = InterpretationEnvelope.model_validate(value)
    selection = envelope.selection
    selection = apply_refinement(selection, context, catalogue)
    pending = context.get('pending_question') if context.get('kind') == 'nl_query' else None
    binding_message = pending + '\n' + message if isinstance(pending, str) and not context.get('plan') else message
    selection = bind_subject(selection, envelope.subject, binding_message, context, identity)
    if selection.action == "query":
        plan = QueryPlan.model_validate(
            selection.plan.model_dump(mode="json"), context={"catalogue": catalogue}
        )
        restricted = (
            catalogue.get("datasets", {})
            .get(plan.dataset, {})
            .get("restricted_fields", {})
        )
        # Older PHP catalogue responses encode an empty array as [] rather than {}.
        # Both mean no field restrictions; reject other malformed policy metadata.
        if restricted == []:
            restricted = {}
        if not isinstance(restricted, dict) or any(
            not isinstance(field, str)
            or not isinstance(roles, list)
            or not roles
            or any(not isinstance(role, str) for role in roles)
            for field, roles in restricted.items()
        ):
            raise HTTPException(
                502, "The reporting catalogue contains invalid permission metadata."
            )
        selected = set(plan.fields + plan.group_by + plan.metrics)
        selected.update(
            item.field for item in plan.filters + plan.having + plan.order_by
        )
        role = identity.designation.strip().upper()
        if any(
            field in selected and role not in roles
            for field, roles in restricted.items()
        ):
            # This early denial saves a read. PHP independently checks the live role.
            return selection.model_copy(
                update={"action": "denied", "plan": None, "refinement": None}
            )
    return employee_headcount(selection, message, catalogue)


def employee_headcount(selection, message, catalogue):
    """Keep employee headcounts distinct from lists and attendance-day totals."""
    if selection.action != 'query':
        return selection
    count = re.search(
        r'\b(?:how\s+(?:many|much)|(?:total\s+)?(?:number|count)\s+of|count|total)\s+'
        r'(?:(?:distinct|unique|total|matching|the|all|these|those|present|late|absent)\s+)*'
        r'(?:employees?|staff(?:\s+members?)?|people|workers?|users?)\b|'
        r'\b(?:employee|staff|worker|user)\s+(?:head)?count\b', message, re.I)
    # Combined requests keep their measures; explicit breakdowns keep their dimensions.
    combined = re.search(r'\b(?:names?|list|details?|and)\b|'
                         r'\balong\s+with\b|\bas\s+well\s+as\b', message, re.I)
    if not count or combined:
        return selection
    plan = selection.plan
    metrics = catalogue.get('datasets', {}).get(plan.dataset, {}).get('metrics', {})
    if 'employee_count' not in metrics:
        return selection
    if any(metric not in ('employee_count', 'employee_days', 'present_days') for metric in plan.metrics):
        return selection
    if plan.having or plan.having_group_by:
        # Counting a threshold-qualified cohort needs an audited two-stage aggregate.
        return Interpretation(action='clarify', clarification='request')
    values = plan.model_dump(mode='json')
    if plan.dataset == 'attendance' and plan.metrics == ['present_days']:
        if not any(item.field == 'is_present' for item in plan.filters):
            values['filters'].append({'field': 'is_present', 'operator': '=', 'value': 1})
    breakdown = re.search(r'\b(?:by|per|each|wise|breakdown)\b', message, re.I)
    values.update(mode='aggregate', fields=[], metrics=['employee_count'], group_by=plan.group_by if breakdown else [],
                  order_by=[], offset=0)
    return selection.model_copy(update={'plan': QueryPlan.model_validate(values, context={'catalogue': catalogue})})


def _validation_rule(issue):
    # Only application-authored static rule codes are logged, never model values.
    reasons = {
        "Value error, Detail queries cannot contain aggregate selections.": "mixed_detail_aggregate",
        "Value error, Aggregate queries require metrics and no detail fields.": "invalid_aggregate_columns",
        "Value error, Select both dates, up to 366 inclusive days.": "invalid_reporting_period",
        "Value error, Attendance supports up to 93 days.": "attendance_period_limit",
        "Value error, Snapshot reports do not accept a reporting period; use approved date filters.": "historical_snapshot",
        "Value error, Select at most ten output columns.": "column_limit",
    }
    return reasons.get(issue["msg"], "schema_constraint")


def _field_access(catalogue, designation):
    """Make existing role restrictions explicit; PHP rechecks the live role."""
    role = designation.strip().upper()
    access = {}
    for dataset, metadata in catalogue.get("datasets", {}).items():
        restricted = metadata.get("restricted_fields", {})
        if restricted == []:
            restricted = {}
        if not isinstance(restricted, dict) or any(
            not isinstance(field, str) or not isinstance(roles, list) or not roles
            or any(not isinstance(item, str) for item in roles)
            for field, roles in restricted.items()
        ):
            raise HTTPException(502, "The reporting catalogue contains invalid permission metadata.")
        if restricted:
            access[dataset] = {field: role in roles for field, roles in restricted.items()}
    return access
