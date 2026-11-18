"""Bind the requested person before executing any generated reporting plan."""

import re

from app.query.models import Interpretation


def bind_subject(selection, subject, message, context, identity):
    if selection.action not in ("query", "specialized"):
        return selection
    key = "plan" if selection.action == "query" else "specialized"
    plan = getattr(selection, key)
    prior = context.get("plan") or context.get("specialized")
    previous = (
        {field: prior.get(field) for field in ("scope", "employee_name", "employee_id")}
        if context.get("kind") == "nl_query"
        and isinstance(prior, dict)
        and prior.get("scope") in ("self", "company")
        else None
    )
    if subject.kind == "unspecified":
        return Interpretation(action="clarify", clarification="subject")
    if subject.kind == "self" and not re.search(r"\b(?:my|mine|myself|i)\b", message, re.IGNORECASE):
        # Recipient-only "me" does not authorize replacing a requested cohort with self.
        return Interpretation(action="clarify", clarification="subject")
    if subject.kind == "previous":
        if previous is None:
            return Interpretation(action="clarify", clarification="selection")
        values = previous
    elif subject.kind == "employee":
        name, employee_id = subject.employee_name, subject.employee_id
        # Names must come from this question or the exact previously selected person.
        if name is not None:
            words = r"\s+".join(re.escape(word) for word in name.strip().split())
            if not words or not re.search(
                r"(?<!\w)" + words + r"(?!\w)", message, re.IGNORECASE
            ):
                remembered = context.get("employee_selector", {})
                prior_name = (previous or {}).get("employee_name") or (
                    remembered.get("employee_name")
                    if isinstance(remembered, dict)
                    else None
                )
                if (
                    previous is None
                    or not prior_name
                    or name.strip().casefold() != prior_name.strip().casefold()
                ):
                    return Interpretation(action="clarify", clarification="selection")
                return selection.model_copy(
                    update={key: plan.model_copy(update=previous)}
                )
        elif not re.search(
            r"(?<![\w/.-])" + str(employee_id) + r"(?![\w/.-])", message
        ):
            if previous is None or employee_id != previous["employee_id"]:
                return Interpretation(action="clarify", clarification="selection")
            return selection.model_copy(update={key: plan.model_copy(update=previous)})
        own = employee_id == identity.user_id or (
            name is not None
            and name.strip().casefold() == identity.name.strip().casefold()
        )
        values = {
            "scope": "self" if own else "company",
            "employee_name": None if own else name,
            "employee_id": identity.user_id if own else employee_id,
        }
    else:
        # Conflicting subject/selector declarations must never fall back to self.
        if plan.employee_name is not None or (
            plan.employee_id is not None
            and (subject.kind != "self" or plan.employee_id != identity.user_id)
        ):
            return Interpretation(action="clarify", clarification="selection")
        values = {"scope": subject.kind, "employee_name": None, "employee_id": None}
    return selection.model_copy(update={key: plan.model_copy(update=values)})
