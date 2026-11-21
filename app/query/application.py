"""HTTP boundary for the isolated AI service."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.auth import Identity, authenticate
from app.backend_client import BackendClient
from app.config import Settings
from app.diagnostics import configure_logging
from app.messages import public_error
from app.query.exports import authorize
from app.query.service import ChatService
from app.query.store import Store
from app.query.wording import public_chart, public_table, public_text
from app.schemas import ChatData, ChatRequest, ChatResponse
from app.token_usage import conversation_context


def create_app(settings=None):
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        configure_logging(config.application_log_file)
        log = logging.getLogger(__name__)
        log.info("service_starting")
        try:
            app.state.store = await asyncio.to_thread(
                Store,
                config.database_file,
                config.conversation_database_url.get_secret_value(),
                config.conversation_retention_days,
            )
        except BaseException:
            log.exception("service_lifecycle_failed")
            raise
        app.state.backend = (
            BackendClient(config) if config.data_mode == "backend" else None
        )
        app.state.chat = ChatService(config, app.state.backend)
        app.state.capacity = asyncio.Semaphore(config.max_concurrent_chats)
        if app.state.chat.token_meter:
            app.state.chat.token_meter.start()

        from app.query.worker import consume, maintain

        maintenance_task = asyncio.create_task(
            maintain(app.state.store, config.conversation_retention_days)
        )

        workers = (
            [
                asyncio.create_task(
                    consume(app.state.store, app.state.chat.export_slots)
                )
                for _ in range(config.max_export_workers)
            ]
            if config.run_export_workers
            else []
        )
        log.info("service_ready")
        try:
            yield
        finally:
            log.info("service_stopping")
            maintenance_task.cancel()
            for worker in workers:
                worker.cancel()
            for worker in workers:
                with suppress(asyncio.CancelledError):
                    await worker
            with suppress(asyncio.CancelledError):
                await maintenance_task
            await app.state.chat.close()
            if app.state.backend:
                await app.state.backend.close()
            await asyncio.to_thread(app.state.store.close)
            log.info("service_stopped")

    app = FastAPI(
        title="Effortrak governed reporting", version="1.0.0", lifespan=lifespan
    )
    app.state.config = config

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        # Backend delegation remains private. Deploy behind a local authenticated
        # gateway; do not trust forwarded client addresses supplied by callers.
        if request.client is None or request.client.host not in (
            "127.0.0.1",
            "::1",
            "testclient",
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "message": "Please open the AI Assistant from Effortrak to continue.",
                },
            )
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger(__name__).exception(
                "request_failed route=%s duration_ms=%d",
                getattr(request.scope.get("route"), "path", "unmatched"),
                (time.monotonic() - started) * 1000,
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "message": public_error("The reporting request could not be completed. Please retry.", 500),
                },
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        logging.getLogger(__name__).info(
            "request_completed route=%s status=%d duration_ms=%d",
            getattr(request.scope.get("route"), "path", "unmatched"),
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    @app.exception_handler(HTTPException)
    async def error(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "message": public_error(exc.detail, exc.status_code)},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "message": "Please enter a question of up to 4,000 characters and check that the selected chat or report is still available.",
            },
        )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "mode": config.chat_mode,
            "query_engine": "governed_nl_sql",
            "data_source": "effortrak_backend"
            if config.data_mode == "backend"
            else "fictional_sample",
            "shared_storage": bool(config.conversation_database_url.get_secret_value()),
            "production_ready": False,
        }

    async def quota(identity):
        if config.ai_token_metering_enabled and app.state.backend:
            try:
                return await app.state.backend.call("token-status", identity.credential)
            except HTTPException:
                pass
        return None

    async def suggestions(context, identity, platform="web"):
        from app.help_features import followup_suggestions

        return await followup_suggestions(
            app.state.backend,
            context or {},
            identity,
            config.ai_help_followups_enabled,
            platform,
        )

    @app.get("/v1/me")
    async def me(identity: Identity = Depends(authenticate)):
        return {
            "success": True,
            "data": {
                "company_id": identity.company_id,
                "user_id": identity.user_id,
                "name": identity.name,
                "designation": identity.designation,
                "attendance_scopes": identity.scopes,
                "can_export": identity.can_export,
                "data_source": identity.data_source,
                "modules": identity.modules,
                "report_scopes": identity.report_scopes,
                "quota": await quota(identity),
                "response_version": 1,
            },
        }

    @asynccontextmanager
    async def admission(identity, conversation_id):
        try:
            await asyncio.wait_for(app.state.capacity.acquire(), timeout=0.05)
        except TimeoutError:
            raise HTTPException(429, "Chat is busy. Please retry shortly.") from None
        leases = []
        try:
            try:
                leases.append(
                    await asyncio.to_thread(
                        app.state.store.claim, "conversation:" + conversation_id
                    )
                )
                leases.append(
                    await asyncio.to_thread(
                        app.state.store.claim,
                        f"user:{identity.company_id}:{identity.user_id}",
                    )
                )
                for slot in range(config.max_company_concurrent_chats):
                    try:
                        leases.append(
                            await asyncio.to_thread(
                                app.state.store.claim,
                                f"company:{identity.company_id}:{slot}",
                            )
                        )
                        break
                    except HTTPException:
                        continue
                else:
                    raise HTTPException(
                        429,
                        "Your company has several requests running. Please retry shortly.",
                    )
            except HTTPException as exc:
                raise HTTPException(
                    429,
                    "A request is already running or chat capacity is busy. Please retry shortly.",
                ) from exc
            yield leases[0]
        finally:
            for lease in reversed(leases):
                await asyncio.to_thread(app.state.store.release, lease)
            app.state.capacity.release()

    @app.post("/v1/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, identity: Identity = Depends(authenticate)):
        store = app.state.store
        identifier = (
            str(body.conversation_id)
            if body.conversation_id
            else await asyncio.to_thread(store.create, identity)
        )
        usage_context = conversation_context.set(identifier)
        try:
            # Check ownership before acquiring resources associated with this ID.
            history = await asyncio.to_thread(store.history, identifier, identity)
            async with admission(identity, identifier) as lease:
                history = await asyncio.to_thread(store.history, identifier, identity)
                try:
                    async with asyncio.timeout(75):
                        reply, tools, reports = await app.state.chat.reply(
                            body.message,
                            history,
                            identity,
                            client_platform=body.client_platform,
                        )
                        if app.state.backend:
                            await app.state.backend.revalidate(identity)
                        followups = await suggestions(
                            getattr(reply, "query_context", {}),
                            identity,
                            body.client_platform,
                        )
                        await asyncio.to_thread(
                            store.save_turn,
                            identifier,
                            identity,
                            body.message,
                            reply,
                            reports,
                            lease,
                        )
                except TimeoutError:
                    raise HTTPException(
                        504,
                        "This report took too long. Narrow the selection and retry.",
                    ) from None
            artifacts = [
                {
                    key: report[key]
                    for key in ("report_id", "filename", "format", "download_url")
                }
                for report in reports
            ]
            return ChatResponse(
                data=ChatData(
                    conversation_id=identifier,
                    reply=str(reply),
                    status=getattr(reply, "status", "answered"),
                    tables=getattr(reply, "tables", []),
                    charts=getattr(reply, "charts", []),
                    suggestions=followups,
                    mode=config.chat_mode,
                    data_source=identity.data_source,
                    tools_used=tools,
                    artifacts=artifacts,
                    jobs=[{"job_id": reply.export_job["job_id"], "status": "queued"}]
                    if getattr(reply, "export_job", None)
                    else [],
                    quota=await quota(identity),
                )
            )
        except BaseException:
            if body.conversation_id is None:
                await asyncio.to_thread(store.delete, identifier, identity)
            raise
        finally:
            conversation_context.reset(usage_context)

    @app.get("/v1/conversations")
    async def conversations(identity: Identity = Depends(authenticate)):
        return {
            "success": True,
            "data": {
                "conversations": await asyncio.to_thread(
                    app.state.store.conversations, identity
                )
            },
        }

    @app.get("/v1/conversations/{conversation_id}")
    async def history(
        conversation_id: UUID, identity: Identity = Depends(authenticate)
    ):
        identifier = str(conversation_id)
        rows = await asyncio.to_thread(app.state.store.history, identifier, identity)
        if app.state.backend:
            from app.query.history import refresh_report_history
            from app.website_help import refresh_help_history

            rows = await refresh_help_history(rows, identity, app.state.backend)
            rows = await refresh_report_history(rows, identity, app.state.backend)
        followups = await suggestions(
            json.loads(rows[-1]["query_context"] or "{}") if rows else {}, identity
        )
        return {
            "success": True,
            "data": {
                "conversation_id": identifier,
                "messages": [
                    {
                        **{
                            key: row[key]
                            for key in ("role", "created_at", "status")
                        },
                        "content": public_text(row["content"]) if row["role"] == "assistant" else row["content"],
                        "charts": [public_chart(chart, json.loads(row["tables"] or "[]")) for chart in json.loads(row["charts"] or "[]")],
                        "tables": [public_table(table) for table in json.loads(row["tables"] or "[]")],
                        "suggestions": [item.model_dump() for item in followups]
                        if index == len(rows) - 1
                        else [],
                    }
                    for index, row in enumerate(rows)
                ],
                "artifacts": await asyncio.to_thread(
                    app.state.store.artifacts, identifier, identity
                ),
                "jobs": await asyncio.to_thread(
                    app.state.store.conversation_jobs, identifier, identity
                ),
            },
        }

    @app.get("/v1/jobs/{job_id}")
    async def job_status(job_id: UUID, identity: Identity = Depends(authenticate)):
        return {
            "success": True,
            "data": await asyncio.to_thread(app.state.store.job, str(job_id), identity),
        }

    @app.delete("/v1/conversations/{conversation_id}", status_code=204)
    async def remove(conversation_id: UUID, identity: Identity = Depends(authenticate)):
        identifier = str(conversation_id)
        await asyncio.to_thread(app.state.store.history, identifier, identity)
        async with admission(identity, identifier):
            await asyncio.to_thread(app.state.store.delete, identifier, identity)
        return Response(status_code=204)

    @app.get("/v1/reports/{report_id}/download")
    async def download(report_id: UUID, identity: Identity = Depends(authenticate)):
        report = await asyncio.to_thread(
            app.state.store.get_report, str(report_id), identity
        )
        if not app.state.backend:
            raise HTTPException(403, "Reports require authenticated backend access.")
        await authorize(
            json.loads(report["export_snapshot"]), identity, app.state.backend
        )
        mime = (
            "application/pdf"
            if report["format"] == "pdf"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        return Response(
            content=report["payload"],
            media_type=mime,
            headers={
                "Content-Disposition": f'attachment; filename="{report["filename"]}"'
            },
        )

    return app
