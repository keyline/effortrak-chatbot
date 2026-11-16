"""NL-to-SQL coordinator. Authorization and numerical answers stay in PHP."""

import asyncio
import json
import ssl
from uuid import uuid4

import truststore
from fastapi import HTTPException
from openai import AsyncOpenAI, DefaultAsyncHttpxClient, OpenAIError

from app.product_help import product_help, product_reply
from app.query import exports
from app.query.contracts import validate_result
from app.query.interpreter import interpret
from app.query.presentation import present
from app.query.specialized import execute as specialized_query
from app.reply import AssistantReply, trusted_query
from app.token_usage import TokenMeter
from app.website_help import website_help

CLARIFICATIONS = {
    "period": "Which date or reporting period should I use?",
    "subject": "Do you mean your own records, a specific employee, or your company?",
    "metric": "Do you mean attendance hours, booked timesheet effort, planned task hours, or tracked productive/idle time?",
    "report": "Which records or measures would you like to see?",
    "selection": "Please specify the employee, project, or reporting selection more precisely.",
}


class ChatService:
    def __init__(self, settings, backend=None):
        self.settings, self.backend = settings, backend
        self.client = None
        self.token_meter = (
            TokenMeter(settings.database_file, backend)
            if settings.ai_token_metering_enabled and backend
            else None
        )
        self.export_slots = asyncio.Semaphore(settings.max_export_workers)
        self.catalogue = None
        self.catalogue_lock = asyncio.Lock()
        if (
            settings.chat_mode == "openai"
            and settings.openai_api_key.get_secret_value()
            and settings.openai_model
        ):
            tls = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.client = AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                timeout=30,
                max_retries=0,
                http_client=DefaultAsyncHttpxClient(verify=tls),
            )

    async def close(self):
        if self.token_meter:
            await self.token_meter.close()
        if self.client:
            await self.client.close()

    async def create_response(self, identity, operation, **kwargs):
        if self.token_meter:
            return await self.token_meter.create(
                self.client, identity, operation, **kwargs
            )
        return await self.client.responses.create(**kwargs)

    async def get_catalogue(self, identity):
        # Business metadata only: permissions and records are never cached here.
        async with self.catalogue_lock:
            if self.catalogue is None:
                self.catalogue = await self.backend.call(
                    "query-catalogue", identity.credential
                )
            return self.catalogue

    async def reply(self, message, history, identity, client_platform="web"):
        context = trusted_query(history)
        overview = product_help(message, context)
        if overview is not None:
            return overview
        if self.backend:
            help_reply = await website_help(
                message, context, identity, self.backend, client_platform, self
            )
            if help_reply is not None:
                return help_reply
        if not self.backend:
            return (
                AssistantReply(
                    "Connect the authenticated Effortrak backend to query real records. Sample mode does not answer company-data questions.",
                    status="unsupported",
                ),
                [],
                [],
            )
        if self.settings.chat_mode != "openai" or self.client is None:
            raise HTTPException(
                503,
                "Natural-language reporting requires the configured AI interpreter and authenticated backend.",
            )
        try:
            selection = await asyncio.wait_for(
                interpret(
                    self, message, context, identity, await self.get_catalogue(identity)
                ),
                timeout=50,
            )
        except (OpenAIError, TimeoutError):
            raise HTTPException(
                502, "AI interpretation is unavailable or timed out. Please retry."
            ) from None
        if selection.action == "denied":
            return (
                AssistantReply(
                    "You do not have permission to view the requested information. Salary, passwords, credentials and private financial identifiers are not available through chat. Leave reasons require Admin or Super Admin and existing leave permission.",
                    status="denied",
                    query_context={},
                ),
                [],
                [],
            )
        if selection.action == "product_overview":
            return product_reply()
        if selection.action == "unsupported":
            return (
                AssistantReply(
                    "This request is not supported by the approved reporting datasets. You can ask about authorized records; changes to records are not available through chat.",
                    status="unsupported",
                    query_context={},
                ),
                [],
                [],
            )
        if selection.action == "help":
            data = await self.backend.call(
                "help",
                identity.credential,
                {"message": message, "client_platform": client_platform},
            )
            return (
                AssistantReply(
                    data.get("reply", "Please specify the feature you need help with."),
                    query_context={},
                ),
                ["website_help"],
                [],
            )
        if selection.action == "clarify":
            pending = {
                "kind": "nl_query",
                "question": message,
                "plan": selection.plan.model_dump(mode="json")
                if selection.plan
                else None,
            }
            return (
                AssistantReply(
                    CLARIFICATIONS.get(
                        selection.clarification, CLARIFICATIONS["selection"]
                    ),
                    status="clarify",
                    query_context=pending,
                ),
                [],
                [],
            )
        plan = selection.plan if selection.action == "query" else selection.specialized
        scopes = (
            identity.report_scopes.get(plan.dataset, identity.scopes)
            if selection.action == "query"
            else identity.scopes
        )
        if (
            plan.scope == "self"
            and plan.employee_name is not None
            and (
                plan.employee_name.strip().casefold()
                == identity.name.strip().casefold()
                and plan.employee_id in (None, identity.user_id)
            )
        ):
            plan = plan.model_copy(
                update={"employee_name": None, "employee_id": identity.user_id}
            )
        if plan.scope == "self" and (
            plan.employee_name is not None
            or plan.employee_id not in (None, identity.user_id)
        ):
            # An explicit employee selector is resolved by PHP within an authenticated scope.
            broader = "company" if "company" in scopes else None
            if broader:
                plan = plan.model_copy(update={"scope": broader})
        if plan.scope not in scopes:
            return (
                AssistantReply(
                    "You do not have permission to view the requested employee scope. Please contact your administrator if access is required.",
                    status="denied",
                    query_context={},
                ),
                [],
                [],
            )
        if plan.scope == "self" and (
            plan.employee_name is not None
            or plan.employee_id not in (None, identity.user_id)
        ):
            return (
                AssistantReply(
                    "That employee selection is not available within your personal scope.",
                    status="denied",
                    query_context={},
                ),
                [],
                [],
            )
        if selection.export and not identity.can_export:
            return (
                AssistantReply(
                    "You do not have permission to export reports.",
                    status="denied",
                    query_context={},
                ),
                [],
                [],
            )
        # Retain the literal selector after PHP resolves a name to its tenant-scoped ID.
        employee_selector = {
            "employee_name": plan.employee_name,
            "employee_id": plan.employee_id,
        }
        try:
            if selection.action == "query":
                result = await self.backend.call(
                    "query", identity.credential, {"plan": plan.model_dump(mode="json")}
                )
                if result.get("status") == "answered":
                    plan = validate_result(plan, result, self.catalogue)
                source = {
                    "endpoint": "query",
                    "arguments": {"plan": plan.model_dump(mode="json"), "export": True},
                }
                if plan.dataset == "attendance" and result.get("as_of"):
                    # Freeze the clock, never authorization, across preview/export/download.
                    source["arguments"]["as_of"] = result["as_of"]
            else:
                result, _ = await specialized_query(self.backend, identity, plan)
                source = {
                    "endpoint": "specialized",
                    "arguments": plan.model_dump(mode="json"),
                }
            if result.get("status") == "clarify":
                pending = {
                    "kind": "nl_query",
                    "question": message,
                    "employee_selector": employee_selector,
                    "plan"
                    if selection.action == "query"
                    else "specialized": plan.model_dump(mode="json"),
                }
                return (
                    AssistantReply(
                        result["message"], status="clarify", query_context=pending
                    ),
                    ["query"],
                    [],
                )
            context = {
                "kind": "nl_query",
                "question": message,
                "employee_selector": employee_selector,
                "plan"
                if selection.action == "query"
                else "specialized": plan.model_dump(mode="json"),
                "chart": selection.chart,
            }
            reply = present(result, context, selection.chart)
            reports = []
            if selection.export:
                full = (
                    await self.backend.call(
                        "query", identity.credential, source["arguments"]
                    )
                    if selection.action == "query"
                    else result
                )
                if full.get("status") != "answered":
                    raise HTTPException(
                        409, "Display the reporting selection again before exporting."
                    )
                if selection.action == "query":
                    validate_result(plan, full, self.catalogue, exporting=True)
                full_reply = present(full, context, selection.chart)
                receipt = exports.snapshot(
                    identity, source, full, self.settings.report_ttl_seconds
                )
                formats = (
                    ["pdf", "xlsx"]
                    if selection.export == "both"
                    else [selection.export]
                )
                if full["total_rows"] > self.settings.background_export_rows:
                    if full.get("has_more") or full.get("offset"):
                        raise HTTPException(
                            413,
                            "Narrow this report before exporting; partial reports are not exported.",
                        )
                    job = {
                        "job_id": str(uuid4()),
                        "data": full,
                        "charts": full_reply.charts,
                        "formats": formats,
                        "receipt": receipt,
                    }
                    if len(json.dumps(job).encode()) > 8 * 1024 * 1024:
                        raise HTTPException(
                            413,
                            "This export is too large. Narrow the report and retry.",
                        )
                    reply = AssistantReply(
                        str(reply)
                        + "\nYour report is being prepared. You can continue chatting.",
                        query_context=context,
                        charts=reply.charts,
                        tables=reply.tables,
                        export_job=job,
                    )
                    return reply, ["nl_query", "queue_export"], []
                async with self.export_slots:
                    reports = await asyncio.to_thread(
                        exports.generate, full, full_reply.charts, formats, receipt
                    )
                reply = AssistantReply(
                    str(reply) + "\nThe complete authorized report is attached.",
                    query_context=context,
                    charts=reply.charts,
                    tables=reply.tables,
                )
            return (
                reply,
                ["nl_query" if selection.action == "query" else "specialized_query"],
                reports,
            )
        except HTTPException as exc:
            if exc.status_code in (403, 413, 422):
                # Keep intent, never records, for a corrected follow-up after denial.
                pending = {
                    "kind": "nl_query",
                    "question": message,
                    "employee_selector": employee_selector,
                    "plan"
                    if selection.action == "query"
                    else "specialized": plan.model_dump(mode="json"),
                }
                return (
                    AssistantReply(
                        str(exc.detail),
                        status="denied" if exc.status_code == 403 else "clarify",
                        query_context=pending,
                    ),
                    ["query"],
                    [],
                )
            raise
