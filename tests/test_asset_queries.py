"""Asset plans use the shared table, chart and private export pipeline."""

from io import BytesIO

import pytest
from openpyxl import load_workbook
from pydantic import ValidationError

from app.query.models import Interpretation, QueryPlan
from app.query.refinements import apply_refinement
from tests.query_support import backend, enable, headers, request  # noqa: F401


def test_inventory_is_current_and_custody_history_requires_a_period():
    assert QueryPlan(dataset="assets").start_date is None
    with pytest.raises(ValidationError):
        QueryPlan(dataset="assets", start_date="2026-09-01", end_date="2026-09-30")
    with pytest.raises(ValidationError):
        QueryPlan(dataset="asset_assignments")
    assert (
        QueryPlan(
            dataset="asset_assignments", start_date="2026-09-01", end_date="2026-09-30"
        ).end_date.isoformat()
        == "2026-09-30"
    )


@pytest.mark.parametrize("aggregate", [False, True])
def test_adding_employee_name_preserves_asset_report_mode_and_selection(aggregate):
    plan = QueryPlan(
        dataset="assets",
        scope="company",
        employee_id=123,
        fields=[] if aggregate else ["asset_name", "asset_tag"],
        mode="aggregate" if aggregate else "details",
        metrics=["asset_count"] if aggregate else [],
        group_by=["category"] if aggregate else [],
        filters=[{"field": "status", "operator": "=", "value": "assigned"}],
        offset=50,
    ).model_dump(mode="json")
    selection = Interpretation(
        action="refine",
        refinement={
            "operation": "columns",
            "columns": ["employee_name", "employee_name"],
        },
    )
    updated = apply_refinement(
        selection, {"kind": "nl_query", "plan": plan}
    ).plan.model_dump(mode="json")
    output = "group_by" if aggregate else "fields"
    expected = plan | {output: plan[output] + ["employee_name"], "offset": 0}
    assert updated == expected


def test_adding_column_resolves_default_fields_from_catalogue():
    selection = Interpretation(
        action="refine",
        refinement={"operation": "columns", "columns": ["employee_name"]},
    )
    result = apply_refinement(
        selection,
        {"kind": "nl_query", "plan": {"dataset": "assets"}},
        {"datasets": {"assets": {"default_fields": ["asset_name", "asset_tag"]}}},
    )
    assert result.plan.fields == ["asset_name", "asset_tag", "employee_name"]


def test_asset_report_uses_shared_charts_and_authorized_downloads(backend):
    client, state = backend
    state["modules"] = ["assets"]
    assert client.get("/v1/me", headers=headers()).status_code == 200
    plan = QueryPlan(
        dataset="assets",
        mode="aggregate",
        metrics=["asset_count"],
        group_by=["category"],
    ).model_dump(mode="json")
    state["result"] = {
        "status": "answered",
        "dataset": "assets",
        "scope": "self",
        "plan": plan,
        "period": {"start": None, "end": None},
        "columns": [
            {"key": "category", "label": "Category", "type": "string", "unit": ""},
            {
                "key": "asset_count",
                "label": "Asset Count",
                "type": "number",
                "unit": "records",
            },
        ],
        "rows": [{"category": "Computers", "asset_count": 2}],
        "total_rows": 1,
        "offset": 0,
        "limit": 50,
        "has_more": False,
        "notice": "Current approved custody only.",
    }
    enable(client, state, [request(plan=plan, chart="bar", export="both")])
    result = client.post(
        "/v1/chat",
        headers=headers(),
        json={
            "message": "Count my assigned assets by category with a bar chart and export Excel and PDF"
        },
    )
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["tables"][0]["rows"] == state["result"]["rows"]
    assert data["charts"][0]["type"] == "bar"
    assert {artifact["format"] for artifact in data["artifacts"]} == {"xlsx", "pdf"}
    for artifact in data["artifacts"]:
        url = artifact["download_url"]
        assert client.get(url, headers=headers(company=2, user=201)).status_code == 404
        download = client.get(url, headers=headers())
        assert download.status_code == 200
        if artifact["format"] == "xlsx":
            sheet = load_workbook(BytesIO(download.content)).active
            assert sheet["A2"].value == "Asset Inventory Report"
            assert sheet["A3"].value == "Reporting period: Current records"
            assert sheet["A8"].value == "Computers" and sheet["B8"].value == 2
        else:
            assert download.content.startswith(b"%PDF")
    state["policy"] = "b" * 64
    assert client.get(
        data["artifacts"][0]["download_url"], headers=headers()
    ).status_code in (403, 409)
