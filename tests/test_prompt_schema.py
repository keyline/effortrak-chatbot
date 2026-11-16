"""Token optimization must retain the complete original model context."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.query.models import QueryPlan, compact_interpretation_schema
from app.query.prompt_schema import interpretation_prompt_context, interpretation_prompt_schema


@pytest.mark.parametrize("dataset", QueryPlan.model_fields["dataset"].annotation.__args__)
def test_prompt_preserves_full_schema_and_all_business_rules(dataset):
    catalogue = {"datasets": {dataset: {
        "description": "Keep every business rule and generated label.",
        "fields": ["employee_id", "employee_name", "date", "status", "reason"],
        "metrics": ["record_count", "total_hours"],
        "restricted_fields": {"reason": ["ADMIN", "SUPER ADMIN"]},
    }}}
    before = deepcopy(catalogue)
    assert interpretation_prompt_schema(catalogue) == compact_interpretation_schema(catalogue)
    assert catalogue == before


def test_empty_catalogue_preserves_specialized_and_refinement_shapes():
    assert interpretation_prompt_schema({}) == compact_interpretation_schema({})


def test_schema_cache_tracks_catalogue_changes_and_returns_independent_copies():
    catalogue = {"datasets": {"assets": {
        "fields": ["asset_name", "employee_name"], "metrics": ["asset_count"],
    }}}
    expected = interpretation_prompt_schema(catalogue)
    changed_request = interpretation_prompt_schema(catalogue)
    changed_request["properties"].clear()
    changed_request["$defs"].clear()
    assert interpretation_prompt_schema(catalogue) == expected
    catalogue["datasets"]["employees"] = {
        "fields": ["employee_name"], "metrics": ["employee_count"],
    }
    assert interpretation_prompt_schema(catalogue) != expected


def test_compact_schema_retains_mode_and_snapshot_period_constraints():
    catalogue = {"datasets": {
        name: {"fields": ["employee_name"], "metrics": ["record_count"]}
        for name in QueryPlan.model_fields["dataset"].annotation.__args__
    }}
    branches = interpretation_prompt_schema(catalogue)["$defs"]["QueryPlan"]["anyOf"]
    assert len(branches) == 4
    for branch in branches:
        props = branch["properties"]
        snapshots = set(props["dataset"]["enum"]) <= {"employees", "assets", "lms_leads"}
        assert props["start_date"]["type"] == ("null" if snapshots else "string")
        assert props["end_date"]["type"] == props["start_date"]["type"]
        if props["mode"]["enum"] == ["details"]:
            assert all(props[key]["maxItems"] == 0 for key in ("metrics", "group_by", "having"))
        else:
            assert props["fields"]["maxItems"] == 0
            assert props["metrics"]["minItems"] == 1


@pytest.mark.parametrize("change", [
    {"fields": ["salary"]},
    {"group_by": ["salary"]},
    {"metrics": ["salary"]},
    {"filters": [{"field": "salary", "operator": ">", "value": 0}]},
    {"having": [{"field": "salary", "operator": ">", "value": 0}]},
    {"order_by": [{"field": "salary", "direction": "desc"}]},
    {"metrics": ["asset_count"]},
])
def test_runtime_catalogue_guard_rejects_unknown_or_other_dataset_vocabulary(change):
    catalogue = {"datasets": {"employees": {
        "fields": ["employee_name"], "metrics": ["employee_count"],
    }}}
    aggregate = any(key in change for key in ("metrics", "group_by", "having"))
    plan = {"dataset": "employees", "mode": "aggregate" if aggregate else "details"}
    if aggregate:
        plan["metrics"] = ["employee_count"]
    with pytest.raises(ValidationError, match="catalogue fields and metrics"):
        QueryPlan.model_validate(plan | change, context={"catalogue": catalogue})


def test_runtime_catalogue_guard_rejects_unavailable_dataset():
    with pytest.raises(ValidationError, match="available catalogue dataset"):
        QueryPlan.model_validate({"dataset": "assets"}, context={"catalogue": {"datasets": {}}})


def test_runtime_catalogue_guard_accepts_every_valid_query_position():
    catalogue = {"datasets": {"employees": {
        "fields": ["employee_id", "employee_name"], "metrics": ["employee_count"],
    }}}
    plan = QueryPlan.model_validate({
        "dataset": "employees", "mode": "aggregate", "group_by": ["employee_name"],
        "metrics": ["employee_count"],
        "filters": [{"field": "employee_id", "operator": ">", "value": 0}],
        "having": [{"field": "employee_count", "operator": ">", "value": 0}],
        "order_by": [{"field": "employee_count", "direction": "desc"}],
    }, context={"catalogue": catalogue})
    assert plan.metrics == ["employee_count"]


@pytest.mark.parametrize("role,allowed", [("Admin", True), ("Super Admin", True), ("Employee", False)])
def test_prompt_field_access_uses_authenticated_role_metadata(role, allowed):
    from app.query.interpreter import _field_access

    catalogue = {"datasets": {"leave": {
        "restricted_fields": {"reason": ["ADMIN", "SUPER ADMIN"]},
    }}}
    assert _field_access(catalogue, role) == {"leave": {"reason": allowed}}


def test_compact_previous_selection_preserves_cohort_and_does_not_mutate_context():
    plan = QueryPlan(
        dataset="attendance", scope="company", start_date="2026-09-14",
        end_date="2026-09-14", mode="aggregate", metrics=["employee_count"],
        filters=[{"field": "status", "operator": "=", "value": "Present"}],
        limit=20, offset=40,
    ).model_dump(mode="json")
    context = {"kind": "nl_query", "question": "Previous count", "plan": plan, "chart": None}
    before = deepcopy(context)
    prompt = interpretation_prompt_context(context)
    assert context == before
    assert QueryPlan.model_validate(prompt["plan"]).model_dump(mode="json") == plan
    assert prompt["question"] == context["question"] and prompt["chart"] is None
    assert "employee_name" not in prompt["plan"]
    prompt["plan"]["filters"].clear()
    assert context["plan"]["filters"] == before["plan"]["filters"]
