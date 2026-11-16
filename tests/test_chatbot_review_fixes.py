"""Regressions for report access, normalized times and background recovery."""

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.query import worker
from app.query.models import QueryPlan
from tests.query_support import backend as backend_fixture
from tests.query_support import enable, headers, request

backend = backend_fixture


@pytest.mark.parametrize("field", ["punch_in", "punch_out"])
@pytest.mark.parametrize(
    "value,expected",
    [("9:00", "09:00:00"), ("11:00", "11:00:00"), ("9:00:59", "09:00:59")],
)
def test_punch_filter_normalization_matches_php_contract(field, value, expected):
    plan = QueryPlan(
        dataset="attendance",
        start_date="2026-09-14",
        end_date="2026-09-14",
        filters=[{"field": field, "operator": ">", "value": value}],
    )
    assert plan.filters[0].value == expected
    assert QueryPlan.model_validate(plan.model_dump()).filters[0].value == expected


def test_punch_in_filter_list_is_normalized():
    plan = QueryPlan(
        dataset="attendance",
        start_date="2026-09-14",
        end_date="2026-09-14",
        filters=[
            {"field": "punch_in", "operator": "in", "value": ["9:00", "11:00:01"]}
        ],
    )
    assert plan.filters[0].value == ["09:00:00", "11:00:01"]


@pytest.mark.parametrize(
    "value", ["24:00", "11:60", "9", "11:00:99", "11:00\n", 11, None]
)
def test_invalid_punch_time_is_rejected(value):
    with pytest.raises(ValidationError):
        QueryPlan(
            dataset="attendance",
            start_date="2026-09-14",
            end_date="2026-09-14",
            filters=[{"field": "punch_in", "operator": ">", "value": value}],
        )


@pytest.mark.parametrize("failure", ["claim", "finish", "failure_finalization"])
def test_export_worker_recovers_from_storage_errors(monkeypatch, failure):
    job = {"payload": {"data": {}, "charts": [], "formats": [], "receipt": {}}}
    claimed = (
        [RuntimeError("temporary storage failure"), job]
        if failure == "claim"
        else [job, job]
    )
    store = NS(
        next_job=Mock(side_effect=[*claimed, asyncio.CancelledError()]),
        finish_job=Mock(
            side_effect=[RuntimeError("temporary storage failure"), None]
            if failure != "claim"
            else None
        ),
    )
    generate = (
        Mock(side_effect=[RuntimeError("generation failure"), []])
        if failure == "failure_finalization"
        else Mock(return_value=[])
    )
    monkeypatch.setattr(worker, "generate", generate)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.consume(store, asyncio.Semaphore(1)))
    assert store.next_job.call_count == 3
    assert store.finish_job.call_args.args == (job, [])


def test_cleanup_recovers_and_still_accepts_cancellation(monkeypatch):
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)
    store = NS(
        prune=Mock(side_effect=[RuntimeError("temporary storage failure"), None])
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.maintain(store, 30))
    assert store.prune.call_count == 2
    assert all(call.args == (60,) for call in sleep.call_args_list)


@pytest.mark.parametrize(
    "dataset", ["lms_leads", "lms_followups", "assets", "asset_assignments"]
)
@pytest.mark.parametrize(
    "change", ["reassigned", "permission_denied", "unchanged", "backend_unavailable"]
)
def test_history_rechecks_ownership_without_model_calls(backend, dataset, change):
    client, state = backend
    metric = {
        "lms_leads": "lead_count",
        "lms_followups": "followup_count",
        "assets": "asset_count",
        "asset_assignments": "assignment_count",
    }[dataset]
    dimension = "stage" if dataset.startswith("lms_") else "category"
    # Fixtures expose only business metadata; report records never enter the model.
    service = client.app.state.chat
    service.catalogue = {
        "datasets": {dataset: {"fields": [dimension], "metrics": [metric]}}
    }
    plan = QueryPlan(
        dataset=dataset,
        mode="aggregate",
        scope="self",
        group_by=[dimension],
        metrics=[metric],
        **(
            {}
            if dataset in ("lms_leads", "assets")
            else {"start_date": "2026-09-01", "end_date": "2026-09-30"}
        ),
    ).model_dump(mode="json")
    state["result"] = {
        "status": "answered",
        "dataset": dataset,
        "scope": "self",
        "plan": plan,
        "period": {"start": plan["start_date"], "end": plan["end_date"]},
        "columns": [
            {"key": dimension, "label": dimension.title(), "type": "string", "unit": ""},
            {"key": metric, "label": "Count", "type": "number", "unit": "records"},
        ],
        "rows": [{dimension: "Previously visible record", metric: 1}],
        "total_rows": 1,
        "offset": 0,
        "limit": 50,
        "has_more": False,
    }
    model = enable(
        client,
        state,
        [request(plan=plan, chart="pie"), request(plan=plan, chart="bar")],
    )
    answer = client.post(
        "/v1/chat", headers=headers(), json={"message": "Show my authorized records"}
    )
    assert answer.status_code == 200, answer.text
    identifier = answer.json()["data"]["conversation_id"]
    answer = client.post(
        "/v1/chat",
        headers=headers(),
        json={"message": "Bar chart", "conversation_id": identifier},
    )
    assert answer.status_code == 200, answer.text
    if change == "reassigned":
        state["result"] = {**state["result"], "rows": [], "total_rows": 0}
    elif change == "permission_denied":
        state["query_denied"] = True
    elif change == "backend_unavailable":
        original = client.app.state.backend.call

        async def unavailable(endpoint, *args, **kwargs):
            if endpoint == "query":
                raise HTTPException(503, "Temporary backend outage")
            return await original(endpoint, *args, **kwargs)

        client.app.state.backend.call = unavailable
    before = len(state["query_bodies"])
    history = client.get("/v1/conversations/" + identifier, headers=headers())
    if change == "backend_unavailable":
        assert history.status_code == 503
        assert "Previously visible record" not in history.text
        assert model.await_count == 2
        return
    assert history.status_code == 200, history.text
    assert (
        len(state["query_bodies"]) == before + 1
    )  # Same selection checked once per history request.
    assert model.await_count == 2
    messages = [
        row for row in history.json()["data"]["messages"] if row["role"] == "assistant"
    ]
    assert len(messages) == 2
    for row in messages:
        if change == "unchanged":
            assert row["tables"] and row["charts"]
            assert "Previously visible record" in str(row)
        else:
            assert row["tables"] == row["charts"] == []
            assert "data or access has changed" in row["content"]
            assert "Previously visible record" not in str(row)


def test_short_time_from_model_reaches_backend_as_normalized_time(backend):
    client, state = backend
    client.app.state.chat.catalogue = {
        "datasets": {"attendance": {"fields": ["punch_in"], "metrics": []}}
    }
    raw = {
        "dataset": "attendance",
        "scope": "self",
        "fields": ["punch_in"],
        "start_date": "2026-09-14",
        "end_date": "2026-09-14",
        "filters": [{"field": "punch_in", "operator": ">", "value": "9:00"}],
    }
    plan = QueryPlan.model_validate(raw).model_dump(mode="json")
    state["result"] = {
        "status": "answered",
        "dataset": "attendance",
        "scope": "self",
        "plan": plan,
        "period": {"start": "2026-09-14", "end": "2026-09-14"},
        "columns": [
            {"key": "punch_in", "label": "Punch in", "type": "string", "unit": ""}
        ],
        "rows": [{"punch_in": "10:00:00"}],
        "total_rows": 1,
        "offset": 0,
        "limit": 50,
        "has_more": False,
    }
    model = enable(client, state, [request(plan=raw)])
    response = client.post(
        "/v1/chat",
        headers=headers(),
        json={"message": "Show my punch in after 9 AM on 14 September 2026"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "answered"
    assert state["query_bodies"][0]["plan"]["filters"][0]["value"] == "09:00:00"
    model.assert_awaited_once()


@pytest.mark.parametrize("change", ["reassigned", "denied", "unchanged"])
def test_saved_lms_locations_recheck_current_access(monkeypatch, change):
    from app.auth import Identity
    from app.query import history
    from app.query.presentation import present

    data = {
        "status": "answered",
        "dataset": "lms_location",
        "period": {},
        "columns": [{"key": "client_name", "label": "Client", "type": "string"}],
        "rows": [{"client_name": "Previously visible location"}],
        "total_rows": 1,
        "offset": 0,
        "has_more": False,
    }
    context = {"specialized": {"kind": "lms_location", "scope": "self"}}
    saved = {
        "role": "assistant",
        "query_context": json.dumps(context),
        "tables": json.dumps(present(data, context).tables),
        "content": "Previously visible location",
        "charts": "[]",
    }
    fresh = {**data, "rows": [], "total_rows": 0} if change == "reassigned" else data
    execute = (
        AsyncMock(side_effect=HTTPException(403, "Not permitted"))
        if change == "denied"
        else AsyncMock(return_value=(fresh, None))
    )
    monkeypatch.setattr(history, "execute", execute)
    rows = asyncio.run(
        history.refresh_report_history(
            [saved], Identity(1, 101, "Example", "Employee", ("self",)), None
        )
    )
    assert execute.await_count == 1
    if change == "unchanged":
        assert rows == [saved]
    else:
        assert rows[0]["tables"] == rows[0]["charts"] == "[]"
        assert "Previously visible location" not in str(rows)
