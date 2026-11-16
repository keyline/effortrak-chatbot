"""LMS authority is dataset-specific; it cannot broaden employee reporting."""

from io import BytesIO

import pytest
from openpyxl import load_workbook
from pydantic import ValidationError

from app.query.models import QueryPlan
from tests.query_support import backend, enable, headers, request  # noqa: F401


@pytest.mark.parametrize("dataset", ["lms_leads", "lms_followups"])
def test_lms_company_reports_charts_and_exports_do_not_grant_employee_scope(
    backend, dataset
):
    client, state = backend
    state["modules"] = ["lms"]
    state["report_scopes"] = {dataset: ["self", "company"]}
    dates = (
        {}
        if dataset == "lms_leads"
        else {"start_date": "2026-09-01", "end_date": "2026-09-30"}
    )
    metric = "lead_count" if dataset == "lms_leads" else "followup_count"
    plan = QueryPlan(
        dataset=dataset,
        scope="company",
        mode="aggregate",
        metrics=[metric],
        group_by=["stage"],
        **dates,
    ).model_dump(mode="json")
    state["result"] = {
        "status": "answered",
        "dataset": dataset,
        "scope": "company",
        "plan": plan,
        "period": {"start": plan["start_date"], "end": plan["end_date"]},
        "columns": [
            {"key": "stage", "label": "Stage", "type": "string", "unit": ""},
            {"key": metric, "label": "Count", "type": "number", "unit": "records"},
        ],
        "rows": [{"stage": "Fresh", metric: 2}],
        "total_rows": 1,
        "offset": 0,
        "limit": 50,
        "has_more": False,
        "notice": "Approved LMS ownership only.",
    }
    model = enable(
        client,
        state,
        [
            request(plan=plan, chart="pie", export="both"),
            request(
                plan={
                    "dataset": "employees",
                    "scope": "company",
                    "mode": "aggregate",
                    "metrics": ["employee_count"],
                }
            ),
        ],
    )
    answer = client.post(
        "/v1/chat",
        headers=headers(),
        json={"message": "Show authorized company LMS counts with chart and reports"},
    )
    assert answer.status_code == 200, answer.text
    data = answer.json()["data"]
    assert data["status"] == "answered" and data["charts"][0]["type"] == "pie"
    assert len(data["artifacts"]) == 2
    for artifact in data["artifacts"]:
        url = artifact["download_url"]
        assert client.get(url, headers=headers(company=2, user=201)).status_code == 404
        download = client.get(url, headers=headers())
        assert download.status_code == 200
        if artifact["format"] == "xlsx":
            sheet = load_workbook(BytesIO(download.content)).active
            assert sheet["A2"].value == (
                "LMS Lead Report" if dataset == "lms_leads" else "LMS Follow-up Report"
            )
            assert sheet["B8"].value == 2
        else:
            assert download.content.startswith(b"%PDF")
    before = len(state["query_bodies"])
    denied = client.post(
        "/v1/chat", headers=headers(), json={"message": "Show company employees"}
    )
    assert denied.json()["data"]["status"] == "denied"
    assert len(state["query_bodies"]) == before
    assert "Fresh" not in str(
        model.call_args.kwargs
    )  # No returned rows in model prompts.
    # Identity policy also includes dataset scopes, even if a backend forgets
    # to change its own policy hash during revocation.
    state["report_scopes"] = {dataset: ["self"]}
    assert client.get(
        data["artifacts"][0]["download_url"], headers=headers()
    ).status_code in (403, 409)


@pytest.mark.parametrize(
    "scopes",
    [
        {"employees": ["self", "company"]},
        {"lms_leads": ["team"]},
        {"lms_leads": []},
    ],
)
def test_invalid_dataset_scope_metadata_is_rejected(backend, scopes):
    client, state = backend
    state["report_scopes"] = scopes
    assert client.get("/v1/me", headers=headers()).status_code == 502


def test_lms_snapshot_and_followup_period_are_distinct():
    assert QueryPlan(dataset="lms_leads").start_date is None
    with pytest.raises(ValidationError):
        QueryPlan(dataset="lms_leads", start_date="2026-09-01", end_date="2026-09-30")
    with pytest.raises(ValidationError):
        QueryPlan(dataset="lms_followups")
