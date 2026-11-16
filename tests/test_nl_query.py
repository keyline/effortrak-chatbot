import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook

from app.auth import Identity
from app.query.models import (
    Interpretation,
    QueryPlan,
    interpretation_schema,
    compact_interpretation_schema,
    strict_schema,
)
from app.query.presentation import charts_for
from app.query.store import Store
from app.reply import AssistantReply
from app.token_usage import TokenMeter
from tests.query_support import backend as backend_fixture
from tests.query_support import enable, headers, model_envelope, request

backend = backend_fixture


def send(client, message="How many employees?", **values):
    return client.post(
        "/v1/chat", headers=headers(), json={"message": message, **values}
    )


def test_single_interpretation_returns_backend_values_and_structured_table(backend):
    client, state = backend
    model = enable(client, state, [request()])
    response = send(client)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["tables"][0]["rows"] == [{"employee_count": 1}]
    assert data["status"] == "answered"
    assert model.await_count == 1
    payload = json.loads(model.call_args.kwargs["input"][0]["content"])
    assert model.call_args.kwargs["input"][0]["content"] == json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )
    assert model.call_args.kwargs["tools"][0]["parameters"] == compact_interpretation_schema(
        payload["catalogue"]
    )
    assert "Private employee name" not in json.dumps(model.call_args.kwargs)
    assert (
        "rows"
        not in json.loads(model.call_args.kwargs["input"][0]["content"])[
            "previous_selection"
        ]
    )


def test_invalid_catalogue_vocabulary_is_corrected_before_any_backend_query(backend):
    client, state = backend
    invalid = request(plan={
        "dataset": "employees", "mode": "aggregate", "scope": "self",
        "metrics": ["asset_count"],
    })
    model = enable(client, state, [invalid, request()])
    response = send(client)
    assert response.status_code == 200, response.text
    assert model.await_count == 2
    assert len(state["query_bodies"]) == 1
    assert state["query_bodies"][0]["plan"]["metrics"] == ["employee_count"]
    retry = json.loads(model.call_args.kwargs["input"][0]["content"])
    assert retry["validation_feedback"]


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "attacker"},
        {"company_id": "1"},
        {"purpose": "other"},
        {"sub": "0"},
        {"exp": int(time.time()) - 1},
    ],
)
def test_delegation_is_required_before_backend_access(backend, changes):
    client, state = backend
    assert client.get("/v1/me", headers=headers(**changes)).status_code == 401
    assert state["calls"] == []


@pytest.mark.parametrize("action", ["denied", "unsupported", "clarify"])
def test_unanswerable_requests_never_read_records(backend, action):
    client, state = backend
    enable(client, state, [request(action=action, plan=None, clarification="period")])
    response = send(client, "Show salary" if action == "denied" else "Show records")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == action
    assert "query" not in state["calls"]


def test_forbidden_scope_is_not_replaced_with_self(backend):
    client, state = backend
    plan = request()["plan"] | {"scope": "company"}
    enable(client, state, [request(plan=plan)])
    response = send(client, "Show company employees")
    assert response.json()["data"]["status"] == "denied"
    assert state["query_bodies"] == []


def test_backend_permission_denial_becomes_proper_reply(backend):
    client, state = backend
    state["query_denied"] = True
    enable(client, state, [request()])
    response = send(client)
    assert response.json()["data"]["status"] == "denied"
    assert "permission" in response.json()["data"]["reply"]


def test_cross_company_and_cross_user_history_are_private(backend):
    client, state = backend
    enable(client, state, [request()])
    identifier = send(client).json()["data"]["conversation_id"]
    for credentials in (headers(company=2, user=201), headers(user=102)):
        assert (
            client.get(
                "/v1/conversations/" + identifier, headers=credentials
            ).status_code
            == 404
        )
        assert (
            client.delete(
                "/v1/conversations/" + identifier, headers=credentials
            ).status_code
            == 404
        )


def test_permission_change_invalidates_history(backend):
    client, state = backend
    enable(client, state, [request()])
    identifier = send(client).json()["data"]["conversation_id"]
    state["policy"] = "b" * 64
    assert (
        client.get("/v1/conversations/" + identifier, headers=headers()).status_code
        == 409
    )


def test_revalidate_before_saving_result(backend):
    client, state = backend
    state["revoke_during_read"] = True
    enable(client, state, [request()])
    assert send(client).status_code == 401
    state["revoked"] = False
    assert (
        client.get("/v1/conversations", headers=headers()).json()["data"][
            "conversations"
        ]
        == []
    )


def test_exports_are_private_expiring_and_reauthorized(backend):
    client, state = backend
    enable(client, state, [request(export="both")])
    response = send(client, "Count employees and export PDF and Excel")
    assert response.status_code == 200, response.text
    artifacts = response.json()["data"]["artifacts"]
    assert {a["format"] for a in artifacts} == {"pdf", "xlsx"}
    for artifact in artifacts:
        url = artifact["download_url"]
        assert client.get(url, headers=headers()).status_code == 200
        assert client.get(url, headers=headers(company=2, user=201)).status_code == 404
    state["export"] = False
    assert client.get(artifacts[0]["download_url"], headers=headers()).status_code in (
        403,
        409,
    )


def test_changed_report_data_requires_regeneration(backend):
    client, state = backend
    enable(client, state, [request(export="pdf")])
    artifact = send(client, "Export the count as PDF").json()["data"]["artifacts"][0]
    state["employee_count"] = 2
    assert client.get(artifact["download_url"], headers=headers()).status_code == 409


def test_export_denial_does_not_call_backend_report(backend):
    client, state = backend
    state["export"] = False
    enable(client, state, [request(export="xlsx")])
    assert send(client, "Export Excel").json()["data"]["status"] == "denied"
    assert state["query_bodies"] == []


def test_followups_send_selection_but_no_backend_values(backend):
    client, state = backend
    model = enable(client, state, [request(), request()])
    identifier = send(client).json()["data"]["conversation_id"]
    send(client, "Show that as a bar chart", conversation_id=identifier)
    context = json.loads(model.call_args.kwargs["input"][0]["content"])[
        "previous_selection"
    ]
    assert context["kind"] == "nl_query"
    assert context["question"] == "How many employees?"
    assert "rows" not in context and "tables" not in context


def test_strict_schema_closes_every_object():
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(strict_schema(Interpretation))


def test_model_schema_separates_detail_and_aggregate_for_every_catalogue_dataset():
    catalogue = {
        "datasets": {
            name: {
                "fields": {"employee_name": "string", "date": "date"},
                "metrics": {"approved_count": "records"},
            }
            for name in QueryPlan.model_fields["dataset"].annotation.__args__
        }
    }
    schema = interpretation_schema(catalogue)
    branches = schema["$defs"]["QueryPlan"]["anyOf"]
    assert len(branches) == 2 * len(catalogue["datasets"])
    for branch in branches:
        props = branch["properties"]
        assert branch["additionalProperties"] is False
        assert set(branch["required"]) == set(props)
        assert props["fields"]["items"]["enum"] == ["employee_name", "date"]
        assert props["metrics"]["items"]["enum"] == ["approved_count"]
        assert props["filters"]["items"]["properties"]["field"]["enum"] == [
            "employee_name",
            "date",
        ]
        assert props["having"]["items"]["properties"]["field"]["enum"] == [
            "approved_count"
        ]
        if props["mode"]["enum"] == ["aggregate"]:
            assert props["fields"]["maxItems"] == 0
            assert props["metrics"]["minItems"] == 1
        else:
            for key in ("metrics", "group_by", "having"):
                assert props[key]["maxItems"] == 0
        for key in ("start_date", "end_date"):
            assert props[key]["type"] == (
                "null"
                if props["dataset"]["enum"][0] in ("employees", "assets", "lms_leads")
                else "string"
            )


def test_model_schema_requires_the_payload_for_its_action():
    schema = interpretation_schema({})
    branches = schema["properties"]["selection"]["anyOf"]
    assert schema["type"] == "object" and "anyOf" not in schema
    for branch in branches:
        props = branch["properties"]
        action = props["action"]["enum"][0]
        selected = {
            "query": "plan",
            "refine": "refinement",
            "specialized": "specialized",
        }.get(action)
        for key in ("plan", "refinement", "specialized"):
            if key != selected:
                assert props[key] == {"type": "null"}
            else:
                assert props[key].get("type") != "null"
                assert not any(
                    item.get("type") == "null" for item in props[key].get("anyOf", [])
                )


@pytest.mark.parametrize(
    "change",
    [
        {
            "operation": "aggregate",
            "metrics": ["employee_days"],
            "group_by": ["employee_id", "employee_name"],
        },
        {
            "operation": "sort",
            "order_by": [{"field": "employee_name", "direction": "asc"}],
        },
        {"operation": "period", "start_date": "2026-08-01", "end_date": "2026-08-31"},
        {"operation": "page", "direction": "next"},
    ],
)
def test_report_refinements_preserve_unmodified_filters_and_access_selections(change):
    from app.query.refinements import apply_refinement

    original = QueryPlan(
        dataset="attendance",
        scope="company",
        employee_id=123,
        start_date="2026-09-01",
        end_date="2026-09-30",
        fields=["employee_id", "employee_name", "date", "punch_in"],
        filters=[{"field": "punch_in", "operator": ">", "value": "11:00:00"}],
    ).model_dump(mode="json")
    selection = Interpretation(action="refine", refinement=change)
    result = apply_refinement(selection, {"kind": "nl_query", "plan": original})
    plan = result.plan.model_dump(mode="json")
    assert result.action == "query"
    assert plan["scope"] == original["scope"] and plan["employee_id"] == 123
    assert plan["filters"] == original["filters"]
    if change["operation"] != "period":
        assert (
            plan["start_date"] == original["start_date"]
            and plan["end_date"] == original["end_date"]
        )
    if change["operation"] == "aggregate":
        assert plan["fields"] == [] and plan["metrics"] == ["employee_days"]
    else:
        assert plan["fields"] == original["fields"]


def test_refinement_without_prior_report_requests_clarification():
    from app.query.refinements import apply_refinement

    result = apply_refinement(
        Interpretation(
            action="refine", refinement={"operation": "page", "direction": "next"}
        ),
        {},
    )
    assert result.action == "clarify" and result.plan is None


@pytest.mark.parametrize(
    "role,expected",
    [("Employee", "denied"), ("Admin", "query"), ("Super Admin", "query")],
)
def test_restricted_leave_reason_uses_authenticated_role_metadata(role, expected):
    from app.query.interpreter import interpret

    plan = QueryPlan(
        dataset="leave",
        scope="self",
        start_date="2026-09-15",
        end_date="2026-09-15",
        fields=["employee_name", "reason"],
    ).model_dump(mode="json")
    response = NS(
        status="completed",
        output=[
            NS(
                type="function_call",
                name="interpret_query",
                arguments=json.dumps(model_envelope({"action": "query", "plan": plan})),
            )
        ],
    )
    service = NS(
        settings=NS(openai_model="isolated-test-model"),
        create_response=AsyncMock(return_value=response),
    )
    catalogue = {
        "datasets": {
            "leave": {
                "fields": ["employee_name", "reason"],
                "metrics": ["request_count"],
                "restricted_fields": {"reason": ["ADMIN", "SUPER ADMIN"]},
            }
        }
    }
    result = asyncio.run(
        interpret(
            service,
            "Show leave with reason",
            {},
            NS(scopes=["self"], modules=["leave"], designation=role, report_scopes={}),
            catalogue,
        )
    )
    assert result.action == expected
    payload = json.loads(
        service.create_response.call_args.kwargs["input"][0]["content"]
    )
    assert payload["authenticated_role"] == role


@pytest.mark.parametrize(
    "dataset", ["employees", "attendance", "effort", "tasks", "leave"]
)
@pytest.mark.parametrize("policy", [[], {}])
def test_empty_catalogue_restrictions_do_not_break_normal_reports(dataset, policy):
    from app.query.interpreter import interpret

    plan = QueryPlan(
        dataset=dataset,
        fields=["employee_name"],
        **(
            {}
            if dataset == "employees"
            else {"start_date": "2026-11-13", "end_date": "2026-11-13"}
        ),
    )
    service = NS(
        settings=NS(openai_model="isolated-test-model"),
        create_response=AsyncMock(
            return_value=NS(
                status="completed",
                output=[
                    NS(
                        type="function_call",
                        name="interpret_query",
                        arguments=json.dumps(
                            model_envelope({"action": "query", "plan": plan.model_dump(mode="json")})
                        ),
                    )
                ],
            )
        ),
    )
    catalogue = {
        "datasets": {
            dataset: {
                "fields": ["employee_name"],
                "metrics": ["employee_count"],
                "restricted_fields": policy,
            }
        }
    }
    result = asyncio.run(
        interpret(
            service,
            "Show my records",
            {},
            NS(
                scopes=["self"],
                report_scopes={},
                modules=["attendance", "tasks", "leave"],
                designation="Employee",
            ),
            catalogue,
        )
    )
    assert result.action == "query" and result.plan == plan


@pytest.mark.parametrize("dataset", ["attendance", "effort", "tasks", "leave"])
def test_aggregate_contract_rejects_leftover_detail_fields_across_modules(dataset):
    with pytest.raises(ValueError, match="Aggregate queries require metrics"):
        QueryPlan(
            dataset=dataset,
            mode="aggregate",
            start_date="2026-09-01",
            end_date="2026-09-30",
            fields=["employee_name"],
            metrics=["employee_count"],
        )


def test_invalid_employee_wise_followup_gets_one_metered_correction_without_records(
    caplog,
):
    from app.query.interpreter import interpret

    prior = QueryPlan(
        dataset="attendance",
        scope="company",
        start_date="2026-09-01",
        end_date="2026-09-30",
        fields=["employee_id", "employee_name", "date", "punch_in"],
        filters=[{"field": "punch_in", "operator": ">", "value": "11:00:00"}],
    ).model_dump(mode="json")
    ordered = prior | {
        "order_by": [
            {"field": "employee_name", "direction": "asc"},
            {"field": "date", "direction": "asc"},
        ]
    }
    invalid = ordered | {"group_by": ["employee_name"]}

    def response(plan):
        return NS(
            status="completed",
            output=[
                NS(
                    type="function_call",
                    name="interpret_query",
                    arguments=json.dumps(model_envelope({"action": "query", "plan": plan})),
                )
            ],
        )

    service = NS(
        settings=NS(openai_model="isolated-test-model"),
        create_response=AsyncMock(side_effect=[response(invalid), response(ordered)]),
    )
    context = {"kind": "nl_query", "plan": prior}
    identity = NS(
        scopes=["self", "company"],
        modules=["attendance"],
        designation="Employee",
        report_scopes={},
    )
    selection = asyncio.run(
            interpret(
                service, "Rearrange this report employee wise", context, identity,
                {"datasets": {"attendance": {
                    "fields": ["employee_id", "employee_name", "date", "punch_in"],
                    "metrics": ["employee_days", "employee_count"],
                }}},
            )
    )
    assert selection.plan.model_dump(mode="json") == ordered
    assert service.create_response.await_count == 2
    for call in service.create_response.call_args_list:
        assert call.args[1] == "nl_sql_interpretation"
        payload = json.loads(call.kwargs["input"][0]["content"])
        from app.query.prompt_schema import interpretation_prompt_context
        assert payload["previous_selection"] == interpretation_prompt_context(context)
        assert "rows" not in payload
    feedback = json.loads(
        service.create_response.call_args.kwargs["input"][0]["content"]
    )["validation_feedback"]
    assert "Detail queries cannot contain aggregate selections" in feedback[0]["reason"]
    assert "11:00:00" not in caplog.text
    assert "Rearrange this report" not in caplog.text


def test_repeated_invalid_interpretation_stops_after_one_correction(backend):
    client, state = backend
    bad_plan = request()["plan"] | {"fields": ["employee_name"]}
    model = enable(client, state, [request(plan=bad_plan), request(plan=bad_plan)])
    response = send(client)
    assert response.status_code == 502
    assert model.await_count == 2
    assert state["query_bodies"] == []


def test_usage_settles_before_invalid_model_output_is_rejected(backend, tmp_path):
    client, state = backend
    model = enable(client, state, [])
    service = client.app.state.chat
    model.side_effect = None
    model.return_value = NS(
        id="resp_bad",
        usage=NS(
            input_tokens=30,
            output_tokens=20,
            total_tokens=50,
            input_tokens_details=NS(cached_tokens=10),
        ),
        output=[
            NS(type="function_call", name="interpret_query", arguments="invalid json")
        ],
    )
    service.client.responses.input_tokens = NS(
        count=AsyncMock(return_value=NS(input_tokens=40))
    )
    service.client.with_options = Mock(return_value=service.client)
    service.token_meter = TokenMeter(tmp_path / "usage.sqlite3", service.backend)
    assert send(client).status_code == 502
    assert state["reservations"][0]["operation"] == "nl_sql_interpretation"
    assert state["settlements"][0]["state"] == "settled"
    assert state["settlements"][0]["total_tokens"] == 50


def test_same_user_concurrent_turn_gets_busy_without_resetting_conversation(backend):
    client, state = backend
    enable(client, state, [request(), request()])
    identifier = send(client).json()["data"]["conversation_id"]
    store = client.app.state.store
    lease = store.claim("conversation:" + identifier)
    try:
        assert send(client, conversation_id=identifier).status_code == 429
    finally:
        store.release(lease)


def test_independent_users_can_query_concurrently(backend):
    client, state = backend
    enable(client, state, [request(), request()])
    original = client.app.state.backend.call
    both_started = asyncio.Event()
    active = 0

    async def slow_read(endpoint, token, body=None):
        nonlocal active
        if endpoint == "query":
            active += 1
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=3)
        return await original(endpoint, token, body)

    client.app.state.backend.call = slow_read

    def question(user):
        return client.post(
            "/v1/chat", headers=headers(user=user), json={"message": "Count employees"}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(question, [101, 102]))
    assert [response.status_code for response in responses] == [200, 200]
    assert active == 2


def test_concurrent_load_is_bounded_per_company_without_server_errors(backend):
    client, state = backend
    enable(client, state, [request() for _ in range(32)])
    original = client.app.state.backend.call
    active, peak = 0, 0

    async def bounded_read(endpoint, token, body=None):
        nonlocal active, peak
        if endpoint != "query":
            return await original(endpoint, token, body)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.2)
            return await original(endpoint, token, body)
        finally:
            active -= 1

    client.app.state.backend.call = bounded_read

    def question(user):
        return client.post(
            "/v1/chat", headers=headers(user=user), json={"message": "Count employees"}
        ).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        statuses = list(pool.map(question, range(200, 232)))
    assert set(statuses).issubset({200, 429})
    assert 200 in statuses and 429 in statuses
    assert peak <= client.app.state.config.max_company_concurrent_chats


def test_shared_store_leases_coordinate_independent_instances(tmp_path):
    path = tmp_path / "shared.sqlite3"
    first, second = Store(path), Store(path)
    one = first.claim("conversation:test")
    with pytest.raises(HTTPException):
        second.claim("conversation:test")
    # Independent conversations can proceed concurrently.
    two = second.claim("conversation:other")
    second.release(two)
    first.release(one)
    second.release(second.claim("conversation:test"))
    first.close()
    second.close()


def test_storage_tenant_policy_and_expiry(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    actor = Identity(1, 101, "Alice", "Employee", ("self",))
    identifier = store.create(actor)
    with pytest.raises(HTTPException):
        store.history(identifier, replace(actor, company_id=2))
    with pytest.raises(HTTPException):
        store.history(identifier, replace(actor, policy_version="changed"))
    store.close()


def test_spreadsheet_cells_never_execute_formulas():
    from app.query.exports import workbook

    data = {
        "dataset": "employees",
        "columns": [{"key": "employee_name", "label": "Name"}],
        "rows": [{"employee_name": '=HYPERLINK("https://attacker")'}],
        "total_rows": 1,
    }
    sheet = load_workbook(BytesIO(workbook(data, []))).active
    assert sheet["A8"].value == '=HYPERLINK("https://attacker")'
    assert sheet["A8"].data_type == "s"
    assert sheet.freeze_panes == "A8"
    assert sheet.auto_filter.ref == "A7:A8"


def test_charts_do_not_present_a_partial_result_as_complete():
    data = {
        "rows": [{"count": 1}],
        "columns": [
            {"key": "count", "label": "Count", "type": "number", "unit": "records"}
        ],
        "has_more": True,
        "offset": 0,
    }
    charts, notice = charts_for(data, "pie")
    assert charts == [] and "complete result" in notice


def test_durable_export_job_is_owned_and_finishes_after_enqueue(backend):
    client, state = backend
    client.app.state.config.background_export_rows = 0
    enable(client, state, [request(export="xlsx")])
    response = send(client, "Export the employee count")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["artifacts"] == [] and data["jobs"][0]["status"] == "queued"
    url = "/v1/jobs/" + data["jobs"][0]["job_id"]
    assert client.get(url, headers=headers(company=2, user=201)).status_code == 404
    assert client.get(url, headers=headers(user=102)).status_code == 404
    for _ in range(50):
        status = client.get(url, headers=headers()).json()["data"]
        if status["status"] == "ready":
            break
        time.sleep(0.1)
    assert status["status"] == "ready"
    assert (
        client.get(
            status["artifacts"][0]["download_url"], headers=headers()
        ).status_code
        == 200
    )
    assert client.app.state.chat.client.responses.create.await_count == 1


def test_expired_job_lease_can_be_recovered_after_worker_restart(tmp_path):
    from sqlalchemy import update

    store = Store(tmp_path / "queue.sqlite3")
    actor = Identity(1, 101, "Alice", "Employee", ("self",))
    identifier = store.create(actor)
    lease = store.claim("conversation:" + identifier)
    job = {
        "job_id": "33333333-3333-4333-8333-333333333333",
        "receipt": {"expires_at": int(time.time()) + 300},
    }
    reply = AssistantReply("Preparing report", export_job=job)
    store.save_turn(identifier, actor, "Export", reply, [], lease)
    original = store.next_job()
    with store.engine.begin() as db:
        db.execute(
            update(store.jobs)
            .where(store.jobs.c.id == job["job_id"])
            .values(lease_until=0)
        )
    recovered = store.next_job()
    assert recovered["id"] == original["id"] and recovered["token"] != original["token"]
    store.finish_job(original, [])
    assert store.job(job["job_id"], actor)["status"] == "working"
    store.finish_job(recovered, [])
    assert store.job(job["job_id"], actor)["status"] == "ready"
    store.close()


def test_backend_cannot_return_extra_sensitive_fields(backend):
    client, state = backend
    plan = QueryPlan.model_validate(request()["plan"]).model_dump(mode="json")
    state["result"] = {
        "status": "answered",
        "dataset": "employees",
        "plan": plan,
        "columns": [{"key": "employee_count"}],
        "rows": [{"employee_count": 1, "salary": 99999}],
    }
    enable(client, state, [request()])
    response = send(client)
    assert response.status_code == 502
    assert "99999" not in response.text


@pytest.mark.parametrize("cleanup", ["deleted_conversation", "exhausted_worker"])
def test_abandoned_export_releases_company_queue_capacity(tmp_path, cleanup):
    from sqlalchemy import update

    store = Store(tmp_path / "queue-cleanup.sqlite3")
    actor = Identity(1, 101, "Alice", "Employee", ("self",))
    identifier = store.create(actor)
    lease = store.claim("conversation:" + identifier)
    job = {
        "job_id": "33333333-3333-4333-8333-333333333333",
        "receipt": {"expires_at": int(time.time()) + 300},
    }
    reply = AssistantReply("Preparing report", export_job=job)
    store.save_turn(identifier, actor, "Export", reply, [], lease)
    resource = job["queue_lease"][0]
    if cleanup == "deleted_conversation":
        store.delete(identifier, actor)
    else:
        with store.engine.begin() as db:
            db.execute(
                update(store.jobs)
                .where(store.jobs.c.id == job["job_id"])
                .values(state="working", attempts=3, lease_until=0)
            )
        store.prune(30)
        assert store.job(job["job_id"], actor)["status"] == "failed"
    store.release(store.claim(resource))
    store.close()


def test_attendance_clock_is_pinned_without_weakening_exports(backend):
    from copy import deepcopy

    client, state = backend
    service = client.app.state.chat
    raw = QueryPlan(dataset="attendance", scope="self", start_date="2026-11-16",
                    end_date="2026-11-16", fields=["attendance_hours"]).model_dump(mode="json")
    service.catalogue = {"datasets": {"attendance": {"fields": ["attendance_hours"], "metrics": []}}}
    stamp = "2026-11-16T11:30:00+05:30"
    state["result"] = {
        "status": "answered", "dataset": "attendance", "scope": "self", "plan": raw,
        "period": {"start": "2026-11-16", "end": "2026-11-16"},
        "columns": [{"key": "attendance_hours", "label": "Attendance hours", "type": "number", "unit": "hours"}],
        "rows": [{"attendance_hours": 1.5}], "total_rows": 1, "offset": 0, "limit": 50, "has_more": False,
    }
    original = service.backend.call
    state["clock_advanced"] = False

    async def clock_read(endpoint, token, body=None):
        data = await original(endpoint, token, body)
        if endpoint == "query":
            data = deepcopy(data)
            data["as_of"] = stamp
            data["rows"][0]["attendance_hours"] = (
                2.0 if state["clock_advanced"] and not body.get("as_of") else 1.5
            )
            if state.get("corrected_punch"):
                data["rows"][0]["attendance_hours"] = 3.0
        return data

    service.backend.call = clock_read
    model = enable(client, state, [request(plan=raw, export="both")])
    response = send(client, "Export my attendance today in PDF and Excel")
    assert response.status_code == 200, response.text
    artifacts = response.json()["data"]["artifacts"]
    assert len(artifacts) == 2
    assert "as_of" not in state["query_bodies"][0]
    assert state["query_bodies"][1]["as_of"] == stamp
    state["clock_advanced"] = True
    for artifact in artifacts:
        assert client.get(artifact["download_url"], headers=headers()).status_code == 200
        assert state["query_bodies"][-1]["as_of"] == stamp
        assert client.get(artifact["download_url"], headers=headers(company=2, user=201)).status_code == 404
    state["corrected_punch"] = True
    assert client.get(artifacts[0]["download_url"], headers=headers()).status_code == 409
    state["export"] = False
    assert client.get(artifacts[0]["download_url"], headers=headers()).status_code in (403, 409)
    model.assert_awaited_once()
