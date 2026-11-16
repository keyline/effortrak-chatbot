"""Isolated delegated backend fixtures for the replacement reporting API."""

import json
import asyncio
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SECRET = "isolated-test-secret-not-for-deployment-1234"


def headers(company=1, user=101, **changes):
    now = int(time.time())
    claims = dict(
        iss="effortrak",
        aud="effortrak-ai",
        sub=str(user),
        company_id=company,
        purpose="attendance-chat",
        iat=now,
        exp=now + 120,
        jti="a" * 32,
    )
    claims.update(changes)
    return {
        "Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256"),
        "X-AI-Service-Key": SECRET,
    }


def request(**changes):
    return {
        "action": "query",
        "plan": {
            "dataset": "employees",
            "mode": "aggregate",
            "scope": "self",
            "metrics": ["employee_count"],
        },
        **changes,
    }


def model_envelope(value):
    """Supply the provider's required subject contract in legacy plan fixtures."""
    if "selection" in value:
        return value
    plan = value.get("plan") or value.get("specialized") or {}
    subject = {"kind": "self", "employee_name": None, "employee_id": None}
    if value.get("action") == "refine":
        subject["kind"] = "previous"
    elif plan.get("employee_name") or plan.get("employee_id"):
        subject.update(kind="employee", employee_name=plan.get("employee_name"))
        if not subject["employee_name"]:
            subject["employee_id"] = plan["employee_id"]
    elif plan.get("scope") in ("company", "team"):
        subject["kind"] = "company"
    return {"subject": subject, "selection": value}


def enable(client, state, selections):
    service = client.app.state.chat
    service.settings.chat_mode = "openai"
    service.settings.openai_model = "isolated-test-model"
    service.client = NS(
        responses=NS(
            create=AsyncMock(
                side_effect=[
                    NS(
                        status="completed",
                        output=[
                            NS(
                                type="function_call",
                                name="interpret_query",
                                arguments=json.dumps(model_envelope(value)),
                            )
                        ],
                    )
                    for value in selections
                ]
            )
        ),
        close=AsyncMock(),
    )
    return service.client.responses.create


@pytest.fixture
def backend(tmp_path):
    state = {
        "policy": "a" * 64,
        "export": True,
        "revoked": False,
        "calls": [],
        "query_bodies": [],
    }

    def respond(req):
        endpoint = req.url.path.rsplit("/", 1)[-1]
        state["calls"].append(endpoint)
        if endpoint == "token-settle":
            state.setdefault("settlements", []).append(json.loads(req.content))
            return httpx.Response(200, json={"success": True, "data": {}})
        claims = jwt.decode(
            req.headers["Authorization"][7:],
            SECRET,
            algorithms=["HS256"],
            audience="effortrak-ai",
        )
        company, user = claims["company_id"], int(claims["sub"])
        if state["revoked"]:
            return httpx.Response(401, json={"success": False})
        if endpoint == "context":
            data = dict(
                company_id=company,
                user_id=user,
                name="Private employee name",
                designation=state.get(
                    "designation",
                    "ADMIN"
                    if "company" in state.get("scopes", [])
                    or any(
                        "company" in scopes
                        for scopes in state.get("report_scopes", {}).values()
                    )
                    else "Employee",
                ),
                scopes=state.get("scopes", ["self"]),
                modules=state.get("modules", ["attendance", "tasks", "leave"]),
                report_scopes=state.get("report_scopes", {}),
                can_export=state["export"],
                policy_version=state["policy"],
                data_source="effortrak_backend",
            )
        elif endpoint == "query-catalogue":
            data = {
                "version": 1,
                "datasets": {
                    "employees": {
                        "fields": ["employee_id", "employee_name"],
                        "metrics": ["employee_count"],
                        "restricted_fields": [],
                    },
                    "assets": {
                        "fields": [
                            "asset_name",
                            "asset_tag",
                            "employee_name",
                            "category",
                        ],
                        "metrics": ["asset_count"],
                    },
                    "lms_leads": {
                        "fields": ["stage", "employee_name"],
                        "metrics": ["lead_count"],
                    },
                    "lms_followups": {
                        "fields": ["stage", "employee_name"],
                        "metrics": ["followup_count"],
                    },
                },
            }
        elif endpoint == "query":
            body = json.loads(req.content)
            state["query_bodies"].append(body)
            if state.get("query_denied"):
                return httpx.Response(
                    403,
                    json={
                        "success": False,
                        "message": "Module read permission is not granted for this scope.",
                    },
                )
            plan = body["plan"]
            if plan["scope"] not in state.get("report_scopes", {}).get(
                plan["dataset"], state.get("scopes", ["self"])
            ):
                return httpx.Response(403, json={"success": False})
            if state.get("revoke_during_read"):
                state["revoked"] = True
            data = state.get("result") or {
                "status": "answered",
                "dataset": plan["dataset"],
                "scope": plan["scope"],
                "plan": plan,
                "period": {
                    "start": plan.get("start_date"),
                    "end": plan.get("end_date"),
                },
                "columns": [
                    {
                        "key": "employee_count",
                        "label": "Employee count",
                        "type": "number",
                        "unit": "employees",
                    }
                ],
                "rows": [{"employee_count": state.get("employee_count", 1)}],
                "total_rows": 1,
                "offset": plan.get("offset", 0),
                "limit": plan.get("limit", 50),
                "has_more": False,
                "notice": "Active authorized employees only.",
            }
        elif endpoint == "token-reserve":
            body = json.loads(req.content)
            state.setdefault("reservations", []).append(body)
            data = {"id": body["id"]}
        elif endpoint == "token-status":
            data = {"remaining_tokens": 1000}
        elif endpoint == "help":
            data = {
                "topic_id": None,
                "status": "clarify",
                "reply": "Please specify the feature you need help with.",
            }
        else:
            raise AssertionError(endpoint)
        return httpx.Response(200, json={"success": True, "data": data})

    config = Settings(
        _env_file=None,
        data_mode="backend",
        chat_mode="demo",
        backend_url="http://127.0.0.1/backend",
        ai_service_secret=SECRET,
        database_path=str(tmp_path / "service.sqlite3"),
        application_log_path=str(tmp_path / "application.log"),
    )
    with TestClient(create_app(config)) as client:
        awaitable_client = client.app.state.backend.client
        asyncio.run(awaitable_client.aclose())
        # The previous unconnected transport owns no sockets yet.
        client.app.state.backend.client = httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        )
        yield client, state
