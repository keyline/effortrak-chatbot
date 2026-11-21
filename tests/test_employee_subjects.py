"""Requested employees cannot be replaced by the authenticated user's records."""

import json

import pytest
from pydantic import ValidationError

from app.auth import Identity
from app.query.models import (
    EmployeeSubject,
    Interpretation,
    QueryPlan,
    SNAPSHOT_DATASETS,
    SpecializedPlan,
    compact_interpretation_schema,
    interpretation_schema,
)
from app.query.subjects import bind_subject
from tests.query_support import backend as backend_fixture
from tests.query_support import enable, headers, request

backend = backend_fixture

IDENTITY = Identity(1, 101, "Signed In User", "SUPER ADMIN", ("self", "company"))
NAMED = {"kind": "employee", "employee_name": "Example Employee", "employee_id": None}


@pytest.mark.parametrize("message", [
    "Can you tell me who are present on 07/11/2026",
    "Can you show me Employee who are present on 07/11/2026",
    "Give me the staff attendance report",
])
def test_recipient_me_cannot_ground_a_self_subject(message):
    result = bind_subject(Interpretation(action="query", plan={"dataset":"employees"}),
        EmployeeSubject(kind="self"), message, {}, IDENTITY)
    assert result.action == "clarify" and result.plan is None


def test_new_company_cohort_replaces_previous_personal_selector():
    result = bind_subject(Interpretation(action="query", plan={"dataset":"employees"}),
        EmployeeSubject(kind="company"), "Show employees who are present",
        {"kind":"nl_query", "plan":{"scope":"self", "employee_id":101}}, IDENTITY)
    assert result.plan.scope == "company" and result.plan.employee_id is None


@pytest.mark.parametrize("message", ["Show my attendance", "Which records are mine?", "Show records for myself", "Am I present?"])
def test_explicit_own_subject_remains_available(message):
    result = bind_subject(Interpretation(action="query", plan={"dataset":"employees"}),
        EmployeeSubject(kind="self"), message, {}, IDENTITY)
    assert result.plan.scope == "self"


def test_bad_self_interpretation_never_queries_personal_data_for_employee_cohort(backend):
    client, state = backend
    state["scopes"] = ["self", "company"]
    enable(client, state, [request()])
    response = client.post("/v1/chat", headers=headers(), json={
        "message":"Can you show me Employee who are present on 07/11/2026"})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "clarify"
    assert response.json()["data"]["tables"] == []
    assert state["query_bodies"] == []


def test_provider_must_extract_subject_before_selecting_a_plan():
    schema = compact_interpretation_schema({})
    assert list(schema["properties"]) == ["subject", "selection"]
    assert "subject" in schema["required"]
    for subject in schema["properties"]["subject"]["anyOf"]:
        assert subject["additionalProperties"] is False
        assert set(subject["required"]) == {"kind", "employee_name", "employee_id"}


@pytest.mark.parametrize(
    "make_schema", [compact_interpretation_schema, interpretation_schema]
)
@pytest.mark.parametrize(
    "kind", ["self", "company", "previous", "unspecified", "employee"]
)
def test_provider_subject_branches_enforce_runtime_combinations(make_schema, kind):
    branches = make_schema({})["properties"]["subject"]["anyOf"]
    assert len(branches) == 3
    matching = [
        item["properties"]
        for item in branches
        if kind in item["properties"]["kind"]["enum"]
    ]
    if kind == "employee":
        assert len(matching) == 2
        assert {
            (item["employee_name"]["type"], item["employee_id"]["type"])
            for item in matching
        } == {
            ("string", "null"),
            ("null", "integer"),
        }
    else:
        assert len(matching) == 1
        assert (
            matching[0]["employee_name"]
            == matching[0]["employee_id"]
            == {"type": "null"}
        )


@pytest.mark.parametrize(
    "kind", SpecializedPlan.model_fields["kind"].annotation.__args__
)
def test_specialized_reports_also_bind_a_dropped_employee(kind):
    selection = Interpretation(action="specialized", specialized={"kind": kind})
    result = bind_subject(
        selection,
        EmployeeSubject(**NAMED),
        "Show Example Employee report",
        {},
        IDENTITY,
    )
    assert result.specialized.scope == "company"
    assert result.specialized.employee_name == "Example Employee"
    assert result.specialized.employee_id is None


@pytest.mark.parametrize(
    "dataset", QueryPlan.model_fields["dataset"].annotation.__args__
)
def test_dropped_or_wrong_plan_subject_is_bound_across_every_dataset(dataset):
    for wrong_selector in ({}, {"employee_id": 101}, {"employee_name": "Wrong Person"}):
        raw = {"dataset": dataset, "scope": "self", **wrong_selector}
        if dataset not in SNAPSHOT_DATASETS:
            raw.update(start_date="2026-11-07", end_date="2026-11-07")
        selection = Interpretation(action="query", plan=raw)
        result = bind_subject(
            selection,
            EmployeeSubject(**NAMED),
            "Can you tell me Example Employee report on 07/11/2026?",
            {},
            IDENTITY,
        )
        assert result.plan.scope == "company"
        assert result.plan.employee_name == "Example Employee"
        assert result.plan.employee_id is None
        assert result.plan.start_date == selection.plan.start_date
        assert selection.plan.scope == "self"


@pytest.mark.parametrize("kind", ["self", "company", "unspecified"])
def test_conflicting_or_ambiguous_subject_requires_clarification(kind):
    result = bind_subject(
        Interpretation(
            action="query",
            plan={"dataset": "employees", "employee_name": "Other Person"},
        ),
        EmployeeSubject(kind=kind),
        "Show my records",
        {},
        IDENTITY,
    )
    assert result.action == "clarify" and result.plan is None


@pytest.mark.parametrize(
    "name,message,expected",
    [
        ("Example Employee", "Show Example Employee's report", "query"),
        ("Example Employee", "Show EXAMPLE   EMPLOYEE report", "query"),
        ("Example Employee", "Show Example Employees report", "clarify"),
        ("Invented Person", "Show Example Employee report", "clarify"),
    ],
)
def test_name_is_grounded_in_current_question(name, message, expected):
    result = bind_subject(
        Interpretation(action="query", plan={"dataset": "employees"}),
        EmployeeSubject(kind="employee", employee_name=name),
        message,
        {},
        IDENTITY,
    )
    assert result.action == expected


@pytest.mark.parametrize(
    "employee_id,message,expected",
    [
        (102, "Show employee ID 102", "query"),
        (102, "Show employee ID 1020", "clarify"),
        (11, "Show attendance on 07/11/2026", "clarify"),
    ],
)
def test_explicit_id_is_grounded_and_not_taken_from_a_date(
    employee_id, message, expected
):
    result = bind_subject(
        Interpretation(action="query", plan={"dataset": "employees"}),
        EmployeeSubject(kind="employee", employee_id=employee_id),
        message,
        {},
        IDENTITY,
    )
    assert result.action == expected


def test_explicit_own_name_stays_self_and_new_self_question_resets_previous_person():
    prior = {"kind": "nl_query", "plan": {"scope": "company", "employee_id": 102}}
    selection = Interpretation(action="query", plan={"dataset": "employees"})
    named = bind_subject(
        selection,
        EmployeeSubject(kind="employee", employee_name=IDENTITY.name),
        "Show Signed In User report",
        prior,
        IDENTITY,
    )
    assert named.plan.scope == "self" and named.plan.employee_id == 101
    own = bind_subject(
        selection,
        EmployeeSubject(kind="self"),
        "Now show my own records",
        prior,
        IDENTITY,
    )
    assert own.plan.scope == "self" and own.plan.employee_id is None


@pytest.mark.parametrize("key", ["plan", "specialized"])
def test_followup_preserves_prior_person_across_report_types(key):
    context = {
        "kind": "nl_query",
        key: {"scope": "company", "employee_id": 102, "employee_name": None},
    }
    result = bind_subject(
        Interpretation(action="query", plan={"dataset": "employees"}),
        EmployeeSubject(kind="previous"),
        "Same report without location",
        context,
        IDENTITY,
    )
    assert result.plan.scope == "company" and result.plan.employee_id == 102


def test_previous_subject_without_context_clarifies_instead_of_returning_self():
    result = bind_subject(
        Interpretation(action="query", plan={"dataset": "employees"}),
        EmployeeSubject(kind="previous"),
        "Their report",
        {},
        IDENTITY,
    )
    assert result.action == "clarify"


@pytest.mark.parametrize("resolved", [False, True])
def test_model_repeating_previous_name_keeps_the_same_person(resolved):
    context = {
        "kind": "nl_query",
        "plan": {
            "scope": "company",
            "employee_name": None if resolved else "Example Employee",
            "employee_id": 102 if resolved else None,
        },
        "employee_selector": {"employee_name": "Example Employee", "employee_id": None},
    }
    result = bind_subject(
        Interpretation(action="query", plan={"dataset": "employees"}),
        EmployeeSubject(**NAMED),
        "Show the same employee without location",
        context,
        IDENTITY,
    )
    assert result.action == "query"
    assert result.plan.scope == "company"
    assert result.plan.employee_id == (102 if resolved else None)
    assert result.plan.employee_name == (None if resolved else "Example Employee")


def test_history_name_requires_an_exact_prior_selector_not_just_question_text():
    result = bind_subject(
        Interpretation(action="query", plan={"dataset": "employees"}),
        EmployeeSubject(**NAMED),
        "Show their attendance",
        {
            "kind": "nl_query",
            "question": "Example Employee",
            "plan": {"scope": "self", "employee_id": 101},
        },
        IDENTITY,
    )
    assert result.action == "clarify" and result.plan is None


@pytest.mark.parametrize("employee_id,expected", [(102, "query"), (103, "clarify")])
def test_continuation_can_reuse_only_the_exact_previous_employee_id(
    employee_id, expected
):
    result = bind_subject(
        Interpretation(action="query", plan={"dataset": "employees"}),
        EmployeeSubject(kind="employee", employee_id=employee_id),
        "Show their report",
        {
            "kind": "nl_query",
            "plan": {"scope": "company", "employee_id": 102},
        },
        IDENTITY,
    )
    assert result.action == expected
    if expected == "query":
        assert result.plan.scope == "company" and result.plan.employee_id == 102


@pytest.mark.parametrize(
    "subject",
    [
        {"kind": "employee"},
        {"kind": "employee", "employee_name": "Example Employee", "employee_id": 102},
        {"kind": "self", "employee_name": "Example Employee"},
        {"kind": "company", "employee_id": 102},
    ],
)
def test_invalid_subject_contract_is_rejected(subject):
    with pytest.raises(ValidationError):
        EmployeeSubject(**subject)


@pytest.mark.parametrize(
    "role,expected",
    [
        ("ADMIN", "answered"),
        ("SUPER ADMIN", "answered"),
        ("Manager", "denied"),
        ("Employee", "denied"),
    ],
)
def test_dropped_employee_is_never_executed_as_self(backend, role, expected):
    client, state = backend
    state.update(scopes=["self", "company"], designation=role)
    raw = {
        "dataset": "attendance",
        "scope": "self",
        "fields": ["employee_name", "punch_in"],
        "start_date": "2026-11-07",
        "end_date": "2026-11-07",
    }
    client.app.state.chat.catalogue = {
        "datasets": {"attendance": {"fields": raw["fields"], "metrics": []}}
    }
    state["result"] = {
        "status": "answered",
        "dataset": "attendance",
        "scope": "company",
        "plan": QueryPlan(
            **(raw | {"scope": "company", "employee_id": 102})
        ).model_dump(mode="json"),
        "period": {"start": "2026-11-07", "end": "2026-11-07"},
        "columns": [
            {"key": key, "label": key, "type": "string", "unit": ""}
            for key in raw["fields"]
        ],
        "rows": [{"employee_name": "Example Employee", "punch_in": "09:30:00"}],
        "total_rows": 1,
        "limit": 50,
        "offset": 0,
        "has_more": False,
    }
    model = enable(client, state, [{"subject": NAMED, "selection": request(plan=raw)}])
    response = client.post(
        "/v1/chat",
        headers=headers(),
        json={
            "message": "Can you tell me Example Employee Attendance Report on 07/11/2026",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == expected
    assert len(state["query_bodies"]) == (1 if expected == "answered" else 0)
    if expected == "answered":
        sent = state["query_bodies"][0]["plan"]
        assert (
            sent["scope"] == "company" and sent["employee_name"] == "Example Employee"
        )
        assert data["tables"][0]["rows"][0]["employee_name"] == "Example Employee"
    model.assert_awaited_once()


def test_missing_subject_cannot_bypass_guard_with_old_flat_output(backend):
    client, state = backend
    model = enable(client, state, [request(), request()])
    responses = list(model.side_effect)
    for response in responses:
        response.output[0].arguments = json.dumps(request())
    model.side_effect = responses
    response = client.post(
        "/v1/chat", headers=headers(), json={"message": "Show my records"}
    )
    assert response.status_code == 502
    assert not state["query_bodies"]
    assert model.await_count == 2


def test_denied_report_retains_intent_and_rechecks_permissions_on_followup(backend):
    client, state = backend
    state.update(scopes=["self", "company"], query_denied=True)
    model = enable(
        client,
        state,
        [
            {"subject": NAMED, "selection": request()},
            {"subject": {"kind": "previous"}, "selection": request()},
        ],
    )
    first = client.post(
        "/v1/chat", headers=headers(), json={"message": "Show Example Employee report"}
    )
    assert first.json()["data"]["status"] == "denied"
    second = client.post(
        "/v1/chat",
        headers=headers(),
        json={
            "message": "Show the same report",
            "conversation_id": first.json()["data"]["conversation_id"],
        },
    )
    assert second.json()["data"]["status"] == "denied"
    assert len(state["query_bodies"]) == 2
    for body in state["query_bodies"]:
        assert body["plan"]["scope"] == "company"
        assert body["plan"]["employee_name"] == "Example Employee"
    payload = json.loads(model.call_args.kwargs["input"][0]["content"])
    assert payload["previous_selection"]["plan"]["employee_name"] == "Example Employee"
    assert "rows" not in payload["previous_selection"]


@pytest.mark.parametrize("allowed", [True, False])
def test_attendance_to_punch_place_followup_keeps_selector_and_permission(
    backend, allowed
):
    from fastapi import HTTPException

    client, state = backend
    state["scopes"] = ["self", "company"]
    raw = {
        "dataset": "attendance",
        "scope": "company",
        "fields": ["employee_name", "punch_in"],
        "start_date": "2026-11-07",
        "end_date": "2026-11-07",
    }
    client.app.state.chat.catalogue = {
        "datasets": {"attendance": {"fields": raw["fields"], "metrics": []}}
    }
    state["result"] = {
        "status": "answered",
        "dataset": "attendance",
        "scope": "company",
        "plan": QueryPlan(**(raw | {"employee_id": 102})).model_dump(mode="json"),
        "period": {"start": "2026-11-07", "end": "2026-11-07"},
        "columns": [
            {"key": key, "label": key, "type": "string", "unit": ""}
            for key in raw["fields"]
        ],
        "rows": [{"employee_name": "Example Employee", "punch_in": "09:30:00"}],
        "total_rows": 1,
        "limit": 50,
        "offset": 0,
        "has_more": False,
    }
    original = client.app.state.backend.call
    locations = []

    async def delegated(endpoint, token, body=None):
        if endpoint != "manager-report":
            return await original(endpoint, token, body)
        locations.append(body)
        assert body == {
            "scope": "company",
            "employee_id": 102,
            "start_date": "2026-11-07",
            "end_date": "2026-11-07",
            "report": "locations",
        }
        if not allowed:
            raise HTTPException(403, "Attendance location permission is required.")
        return {
            "rows": [
                {
                    "employee_id": 102,
                    "employee_name": "Example Employee",
                    "date": "2026-11-07",
                    "punch_in_time": "09:30:00",
                    "punch_in_address": "Example Office",
                }
            ],
            "matched_count": 1,
        }

    client.app.state.backend.call = delegated
    model = enable(
        client,
        state,
        [
            {"subject": NAMED, "selection": request(plan=raw)},
            {
                "subject": {
                    "kind": "previous",
                    "employee_name": None,
                    "employee_id": None,
                },
                "selection": {
                    "action": "specialized",
                    "specialized": {
                        "kind": "punch_locations",
                        "scope": "self",
                        "start_date": "2026-11-07",
                        "end_date": "2026-11-07",
                    },
                },
            },
        ],
    )
    first = client.post(
        "/v1/chat",
        headers=headers(),
        json={
            "message": "Can you tell me Example Employee Attendance report for 07/11/2026",
        },
    )
    assert first.status_code == 200 and first.json()["data"]["status"] == "answered"
    second = client.post(
        "/v1/chat",
        headers=headers(),
        json={
            "message": "Can you tell me from where he punch in (Place name)",
            "conversation_id": first.json()["data"]["conversation_id"],
        },
    )
    assert second.status_code == 200, second.text
    data = second.json()["data"]
    assert data["status"] == ("answered" if allowed else "denied")
    assert len(locations) == 1 and model.await_count == 2
    if allowed:
        assert data["tables"][0]["rows"][0]["punch_in_address"] == "Example Office"
    else:
        assert not data["tables"] and not data["artifacts"]
        assert "You don't have access to attendance locations" in data["reply"]
    assert "invalid reporting selection" not in second.text
