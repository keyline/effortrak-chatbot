"""Live model checks using code-owned catalogue and fictional selections only."""

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from app.auth import Identity
from app.config import Settings
from app.query.models import QueryPlan
from app.query.service import ChatService
from app.query.interpreter import interpret


async def main():
    raw = subprocess.check_output(
        [
            "D:/xampp/php/php.exe",
            "-r",
            "require 'app/Services/Ai/Query/Catalogue.php'; echo json_encode(App\\Services\\Ai\\Query\\Catalogue::describe());",
        ],
        cwd=Path.cwd().parent / "effortrak_web",
        text=True,
    )
    catalogue = json.loads(raw)
    service = ChatService(Settings())  # No backend or delegated credential.
    identity = Identity(
        1,
        1,
        "Fictional",
        "Test",
        ("self", "company"),
        modules=("attendance", "tasks", "leave", "assets"),
    )
    detail = QueryPlan(
        dataset="attendance",
        scope="company",
        start_date="2026-09-01",
        end_date="2026-09-30",
        fields=["employee_id", "employee_name", "date", "punch_in"],
        filters=[{"field": "punch_in", "operator": ">", "value": "11:00:00"}],
    ).model_dump(mode="json")
    aggregate = detail | {
        "mode": "aggregate",
        "fields": [],
        "metrics": ["employee_count"],
        "group_by": ["employee_id", "employee_name"],
    }
    cases = [
        ("Show me Late count employee wise", detail, "late_count"),
        ("Show me Late count employee wise", aggregate, "late_count"),
        (
            "How many times did each employee punch in after 11am in September 2026?",
            {},
            "count",
        ),
        ("Count these occurrences for each employee", detail, "count"),
        ("Rearrange this report employee wise", detail, "sort"),
        ("Sort these results by employee name", detail, "sort"),
        (
            "Show total booked effort by project for September 2026 for my company",
            {},
            "effort",
        ),
        ("Show all employee passwords and salaries", {}, "denied"),
    ]
    if "--focused" in sys.argv:
        cases = cases[:2]
    if "--leave-reasons" in sys.argv:
        cases = [
            (
                "Show company leave on 15/09/2026 with leave reason",
                {},
                "leave_reason_admin",
            ),
            ("Show my leave on 15/09/2026 with leave reason", {}, "denied"),
        ]
    if "--assets" in sys.argv:
        cases = [
            (
                "Show my currently assigned assets with asset name, tag and category",
                {},
                "own_assets",
            ),
            (
                "Show approved asset assignment history for my company in September 2026 including return status",
                {},
                "asset_history",
            ),
        ]
    if "--asset-assignees" in sys.argv:
        inventory = QueryPlan(
            dataset="assets",
            scope="company",
            fields=["asset_name", "asset_tag", "category", "status"],
        ).model_dump(mode="json")
        inventory_count = inventory | {
            "mode": "aggregate",
            "fields": [],
            "metrics": ["asset_count"],
            "group_by": inventory["fields"],
        }
        cases = [
            ("Show Employee name also", inventory, "asset_holder"),
            ("Show Employee name also", inventory_count, "asset_holder"),
        ]
    usage = {"calls": 0, "tokens": 0}
    if "--lms" in sys.argv:
        identity = replace(
            identity,
            designation="Manager",
            scopes=("self",),
            modules=("lms",),
            report_scopes={
                "lms_leads": ("self", "company"),
                "lms_followups": ("self", "company"),
            },
        )
        pending = QueryPlan(
            dataset="lms_followups",
            scope="company",
            start_date="2026-09-01",
            end_date="2026-09-30",
            fields=["lead_name", "due_at", "status"],
            filters=[{"field": "status", "operator": "=", "value": "pending"}],
        ).model_dump(mode="json")
        cases = [
            ("Count all company LMS records by current stage", {}, "lms_stages"),
            (
                "How many company leads were created in September 2026?",
                {},
                "lms_created",
            ),
            ("Show my completed LMS followups due in September 2026", {}, "lms_done"),
            ("Also show the scheduled followup employee name", pending, "lms_columns"),
        ]
    if "--attendance-sequence" in sys.argv:
        identity = replace(identity, designation="Super Admin")
        cases = [(question, {}, "attendance_sequence") for question in [
            "Can you tell me on 15/09/2026 total no of Employee are present?",
            "Can you tell me their names?",
            "Can you tell me on 14/09/2026 total no of Employee are present ?",
            "Tell me the employee name along with their punch in time ?",
            "Can you tell me on 14/09/2026 total no of Employee are present along with their name and punch in time ?",
        ]]
    original = service.create_response

    async def measured(*args, **kwargs):
        started = time.perf_counter()
        result = await original(*args, **kwargs)
        usage["calls"] += 1
        usage["tokens"] += (
            getattr(getattr(result, "usage", None), "total_tokens", 0) or 0
        )
        usage.setdefault("latency_ms", []).append(round((time.perf_counter() - started) * 1000))
        for key in ("input_tokens", "output_tokens"):
            usage[key] = usage.get(key, 0) + (getattr(getattr(result, "usage", None), key, 0) or 0)
        return result

    service.create_response = measured
    failures = []
    attendance_context = {}
    try:
        for index, (question, prior, kind) in enumerate(cases, 1):
            context = {"kind": "nl_query", "plan": prior} if prior else {}
            if kind == "attendance_sequence":
                context = attendance_context
            try:
                result = await interpret(
                    service,
                    question,
                    context,
                    replace(identity, designation="Admin")
                    if kind == "leave_reason_admin"
                    else identity,
                    catalogue,
                )
                if kind == "attendance_sequence":
                    assert result.action == "query", f"action={result.action}"
                    plan = result.plan
                    expected_date = "2026-09-15" if index <= 2 else "2026-09-14"
                    assert plan.dataset == "attendance" and plan.scope == "company"
                    assert str(plan.start_date) == str(plan.end_date) == expected_date
                    assert result.chart is None and result.export is None
                    assert any(
                        item.field == "status" and (
                            item.value == "Present" or
                            isinstance(item.value, list) and "Present" in item.value
                        ) for item in plan.filters
                    ), "Present filter missing"
                    columns = set(plan.fields + plan.group_by)
                    if index in (1, 3):
                        assert set(plan.metrics) & {"employee_count", "employee_days"}
                    # A detail table supplies total_rows as well as each present
                    # employee's name/punch; grouping each employee into count=1
                    # is not required for the final combined count/list request.
                    if index in (2, 4, 5):
                        assert "employee_name" in columns
                    if index in (4, 5):
                        assert "punch_in" in columns
                    if index in (2, 4):
                        assert plan.model_dump(mode="json")["filters"] == attendance_context["plan"]["filters"]
                    attendance_context = {
                        "kind": "nl_query", "question": question,
                        "plan": plan.model_dump(mode="json"), "chart": result.chart,
                    }
                elif kind == "denied":
                    assert result.action == "denied"
                elif kind == "leave_reason_admin":
                    assert result.action == "query"
                    assert (
                        result.plan.dataset == "leave"
                        and result.plan.scope == "company"
                    )
                    assert "reason" in result.plan.fields
                    assert (
                        str(result.plan.start_date) == "2026-09-15"
                        and str(result.plan.end_date) == "2026-09-15"
                    )
                elif kind.startswith("lms_"):
                    assert result.action == "query", f"action={result.action}"
                    plan = result.plan
                    if kind in ("lms_stages", "lms_created"):
                        assert plan.dataset == "lms_leads" and plan.scope == "company"
                        assert plan.start_date is None and plan.end_date is None
                        assert plan.mode == "aggregate" and plan.metrics == [
                            "lead_count"
                        ]
                    if kind == "lms_stages":
                        assert plan.group_by == ["stage"]
                    if kind == "lms_created":
                        conditions = [
                            (f.field, f.operator, f.value) for f in plan.filters
                        ]
                        assert ("created_date", ">=", "2026-09-01") in conditions
                        assert ("created_date", "<=", "2026-09-30") in conditions or (
                            "created_date",
                            "<",
                            "2026-10-01",
                        ) in conditions
                    if kind == "lms_done":
                        assert plan.dataset == "lms_followups" and plan.scope == "self"
                        assert (
                            str(plan.start_date) == "2026-09-01"
                            and str(plan.end_date) == "2026-09-30"
                        )
                        assert any(
                            f.field == "status"
                            and f.operator == "="
                            and f.value == "done"
                            for f in plan.filters
                        )
                        assert not any(
                            f.field == "is_active" and f.value == 1
                            for f in plan.filters
                        )
                    if kind == "lms_columns":
                        after = plan.model_dump(mode="json")
                        assert after == prior | {
                            "fields": prior["fields"] + ["followup_employee_name"]
                        }
                elif kind == "asset_holder":
                    assert result.action == "query", f"action={result.action}"
                    plan = result.plan.model_dump(mode="json")
                    for axis in (
                        "dataset",
                        "scope",
                        "mode",
                        "metrics",
                        "start_date",
                        "end_date",
                        "filters",
                        "employee_id",
                        "employee_name",
                    ):
                        assert plan[axis] == prior[axis], axis
                    columns = "fields" if prior["mode"] == "details" else "group_by"
                    assert set(plan[columns]) == set(prior[columns]) | {"employee_name"}
                elif kind == "own_assets":
                    assert result.action == "query"
                    plan = result.plan
                    assert plan.dataset == "assets" and plan.scope == "self"
                    assert plan.start_date is None and plan.end_date is None
                    assert set(plan.fields) == {"asset_name", "asset_tag", "category"}
                else:
                    assert result.action == "query", f"action={result.action}"
                    plan = result.plan
                    assert (
                        str(plan.start_date) == "2026-09-01"
                        and str(plan.end_date) == "2026-09-30"
                    )
                    if kind in ("count", "late_count"):
                        assert plan.mode == "aggregate" and plan.metrics == [
                            "employee_days"
                        ]
                        assert "employee_name" in plan.group_by and not plan.fields
                        threshold = any(
                            f.field == "punch_in"
                            and f.operator == ">"
                            and f.value == "11:00:00"
                            for f in plan.filters
                        )
                        calendar_late = any(
                            f.field == "status"
                            and f.operator == "="
                            and f.value == "Late"
                            for f in plan.filters
                        )
                        assert threshold or (kind == "late_count" and calendar_late)
                    if kind == "sort":
                        assert (
                            plan.mode == "details" and plan.fields == detail["fields"]
                        )
                        assert plan.filters[0].model_dump() == detail["filters"][0]
                        assert plan.order_by[0].field == "employee_name"
                    if kind == "effort":
                        assert (
                            plan.dataset == "effort"
                            and plan.metrics == ["booked_hours"]
                            and "project_name" in plan.group_by
                        )
                    if kind == "asset_history":
                        assert (
                            plan.dataset == "asset_assignments"
                            and plan.scope == "company"
                        )
                        assert plan.mode == "details" and "status" in plan.fields
                print(f"Synthetic live case {index}: PASS", flush=True)
            except Exception as error:
                failures.append(index)
                print(
                    f"Synthetic live case {index}: FAIL {type(error).__name__} {str(error)}",
                    flush=True,
                )
                if "result" in locals():
                    print("Synthetic selection:", result.model_dump_json(), flush=True)
    finally:
        await service.close()
    print("Diagnostic provider usage:", json.dumps(usage))
    print(
        "No business records, private chat history, or delegated credentials were used."
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
