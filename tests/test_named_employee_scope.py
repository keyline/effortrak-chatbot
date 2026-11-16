"""Employee selectors do not inherit a model's default personal scope."""

import pytest

from app.query.models import QueryPlan
from tests.query_support import backend as backend_fixture
from tests.query_support import enable, headers, request

backend = backend_fixture


@pytest.mark.parametrize(
    "scopes,role,selector,expected",
    [
        (
            ["self", "company"],
            "SUPER ADMIN",
            {"employee_name": "Example Employee"},
            "company",
        ),
        (["self", "company"], "ADMIN", {"employee_id": 102}, "company"),
        (["self", "team"], "Team Lead", {"employee_name": "Example Employee"}, None),
        (["self", "company"], "Manager", {"employee_name": "Example Employee"}, None),
        (["self", "company"], "HR", {"employee_id": 102}, None),
        (["self"], "Employee", {"employee_name": "Example Employee"}, None),
        (["self"], "SUPER ADMIN", {"employee_id": 102}, None),
        (["self", "company"], "ADMIN", {}, "self"),
        (
            ["self", "company"],
            "ADMIN",
            {"employee_name": "Private employee name"},
            "self",
        ),
    ],
)
def test_named_punch_queries_use_only_authenticated_scopes(
    backend, scopes, role, selector, expected
):
    client, state = backend
    state["scopes"] = scopes
    state["designation"] = role
    raw = {
        "dataset": "attendance",
        "scope": "self",
        "fields": ["employee_name", "punch_in"],
        "start_date": "2026-11-07",
        "end_date": "2026-11-07",
        **selector,
    }
    client.app.state.chat.catalogue = {
        "datasets": {
            "attendance": {"fields": ["employee_name", "punch_in"], "metrics": []}
        }
    }
    if expected:
        resolved = QueryPlan.model_validate(
            {
                **raw,
                "scope": expected,
                "employee_name": None,
                "employee_id": (101 if selector else None)
                if expected == "self"
                else 102,
            }
        ).model_dump(mode="json")
        state["result"] = {
            "status": "answered",
            "dataset": "attendance",
            "scope": expected,
            "plan": resolved,
            "period": {"start": "2026-11-07", "end": "2026-11-07"},
            "columns": [
                {
                    "key": "employee_name",
                    "label": "Employee name",
                    "type": "string",
                    "unit": "",
                },
                {"key": "punch_in", "label": "Punch in", "type": "string", "unit": ""},
            ],
            "rows": [{"employee_name": "Example Employee", "punch_in": "09:30:00"}],
            "total_rows": 1,
            "offset": 0,
            "limit": 50,
            "has_more": False,
        }
    model = enable(client, state, [request(plan=raw)])
    response = client.post(
        "/v1/chat",
        headers=headers(),
        json={"message": f"Can you tell punch in of {selector.get('employee_name') or selector.get('employee_id') or 'myself'} on 07/11/2026"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == ("answered" if expected else "denied")
    assert len(state["query_bodies"]) == (1 if expected else 0)
    if expected:
        assert state["query_bodies"][0]["plan"]["scope"] == expected
        assert response.json()["data"]["tables"][0]["rows"][0]["punch_in"] == "09:30:00"
    model.assert_awaited_once()


def test_lms_company_scope_does_not_allow_other_employees_attendance(backend):
    client, state = backend
    state["report_scopes"] = {"lms_leads": ["self", "company"]}
    state["modules"] = ["attendance", "lms"]
    enable(
        client,
        state,
        [
            request(
                plan={
                    "dataset": "employees",
                    "scope": "self",
                    "mode": "aggregate",
                    "metrics": ["employee_count"],
                    "employee_name": "Example Employee",
                }
            )
        ],
    )
    response = client.post(
        "/v1/chat", headers=headers(), json={"message": "Show Example Employee"}
    )
    assert response.json()["data"]["status"] == "denied"
    assert not state["query_bodies"]


def test_named_query_still_obeys_backend_denial(backend):
    client, state = backend
    state.update(scopes=["self", "company"], query_denied=True)
    model = enable(
        client,
        state,
        [
            request(
                plan={
                    "dataset": "employees",
                    "scope": "self",
                    "mode": "aggregate",
                    "metrics": ["employee_count"],
                    "employee_name": "Example Employee",
                }
            )
        ],
    )
    response = client.post(
        "/v1/chat", headers=headers(), json={"message": "Show Example Employee"}
    )
    assert response.json()["data"]["status"] == "denied"
    assert state["query_bodies"][0]["plan"]["scope"] == "company"
    model.assert_awaited_once()


@pytest.mark.parametrize("allowed", [True, False])
def test_named_punch_locations_reuse_permission_checked_reader(backend, allowed):
    from fastapi import HTTPException

    client, state = backend
    state["scopes"] = ["self", "company"]
    original = client.app.state.backend.call
    calls = []

    async def delegated(endpoint, token, body=None):
        if endpoint == "employee-search":
            assert body["scope"] == "company"
            return {
                "matches": [{"employee_id": 102, "name": "Example Employee"}],
                "has_more": False,
            }
        if endpoint == "manager-report":
            calls.append(body)
            assert body["scope"] == "company" and body["employee_id"] == 102
            assert body["report"] == "locations"
            if not allowed or state.get("locations_revoked"):
                raise HTTPException(403, "Attendance location permission is required.")
            return {
                "rows": [
                    {
                        "employee_id": 102,
                        "employee_name": "Example Employee",
                        "date": "2026-11-07",
                        "punch_in_time": "09:30:00",
                        "punch_out_time": "18:30:00",
                        "punch_in_address": "Example Office",
                        "punch_out_address": "Example Office",
                        "private_note": "Never display",
                    }
                ],
                "matched_count": 1,
            }
        return await original(endpoint, token, body)

    client.app.state.backend.call = delegated
    model = enable(
        client,
        state,
        [
            {
                "action": "specialized",
                "specialized": {
                    "kind": "punch_locations",
                    "scope": "self",
                    "employee_name": "Example Employee",
                    "start_date": "2026-11-07",
                    "end_date": "2026-11-07",
                },
                "export": "both" if allowed else None,
            }
        ],
    )
    response = client.post(
        "/v1/chat",
        headers=headers(),
        json={
            "message": "Show Example Employee punch in on 07/11/2026 along with location name"
        },
    )
    assert response.status_code == 200, response.text
    answer = response.json()["data"]
    assert answer["status"] == ("answered" if allowed else "denied")
    if allowed:
        row = answer["tables"][0]["rows"][0]
        assert (
            row["punch_in_time"] == "09:30:00"
            and row["punch_in_address"] == "Example Office"
        )
        assert "Never display" not in str(answer)
        assert len(answer["artifacts"]) == 2
        for artifact in answer["artifacts"]:
            assert (
                client.get(
                    artifact["download_url"], headers=headers(company=2, user=201)
                ).status_code
                == 404
            )
            assert (
                client.get(artifact["download_url"], headers=headers()).status_code
                == 200
            )
        state["locations_revoked"] = True
        assert (
            client.get(
                answer["artifacts"][0]["download_url"], headers=headers()
            ).status_code
            == 403
        )
    else:
        assert not answer["tables"] and not answer["artifacts"]
    model.assert_awaited_once()
