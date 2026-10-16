import asyncio
import json
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.auth import Identity, authenticate
from app.chat_service import ChatService
from app.backend_client import BackendClient
from app.config import Settings
from app.schemas import ChatData, ChatRequest, ChatResponse
from app.store import Store
from app.token_usage import conversation_context
from app.tools import allowed_employee_ids
from app.employee_lookup import named_attendance, named_tasks
from app.task_report import AssistantReply
from app.manager_query import capability_reply, report_kind


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = Store(config.database_file)
        app.state.backend = BackendClient(config) if config.data_mode == 'backend' else None
        app.state.chat = ChatService(config, app.state.backend)
        if app.state.chat.token_meter:
            app.state.chat.token_meter.start()
        app.state.chat_lock = asyncio.Lock()
        yield
        await app.state.chat.close()
        if app.state.backend:
            await app.state.backend.close()

    app = FastAPI(title='Effortrak AI REST API', version='0.2.0', lifespan=lifespan,
                  description='Sample mode or CodeIgniter-delegated real attendance. Use the backend gateway for real data.')
    app.state.config = config

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        # Keep both sample mode and the backend delegation channel local in this phase.
        if request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient'):
            return JSONResponse(status_code=403, content={'success': False, 'message': 'Local prototype only.'})
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code,
                            content={'success': False, 'message': exc.detail}, headers=exc.headers)

    @app.get('/health')
    def health():
        return {'status': 'ok', 'mode': config.chat_mode,
                'authentication': 'demo_bearer' if config.data_mode == 'sample' else 'codeigniter_delegation',
                'data_source': 'fictional_sample' if config.data_mode == 'sample' else 'effortrak_backend',
                'production_ready': False}

    async def usage_status(identity):
        if config.ai_token_metering_enabled and app.state.backend:
            try:
                return await app.state.backend.call('token-status', identity.credential)
            except HTTPException:
                pass
        return None

    @app.get('/v1/me')
    async def me(identity: Identity = Depends(authenticate)):
        return {'success': True, 'data': {'company_id': identity.company_id,
                'user_id': identity.user_id, 'name': identity.name,
                'designation': identity.designation, 'attendance_scopes': identity.scopes,
                'can_export': identity.can_export, 'data_source': identity.data_source,
                'modules': identity.modules, 'quota': await usage_status(identity)}}

    @app.post('/v1/chat', response_model=ChatResponse)
    async def chat(body: ChatRequest, identity: Identity = Depends(authenticate)):
        # Serialize local prototype turns so concurrent requests cannot reorder history.
        async with app.state.chat_lock:
            store = app.state.store
            conversation_id = str(body.conversation_id) if body.conversation_id else store.create(identity)
            history = store.history(conversation_id, identity)
            usage_context = conversation_context.set(conversation_id)
            try:
                reply, tools_used, reports = await app.state.chat.reply(body.message, history, identity)
                if app.state.backend:
                    await app.state.backend.revalidate(identity)
                message = body.message
                if app.state.backend and (named_attendance(message) or named_tasks(message) or 'export_attendance' in tools_used
                                          or 'project_search' in tools_used or 'get_project_tasks' in tools_used
                                          or 'get_manager_report' in tools_used or capability_reply(message) or report_kind(message)
                                          or 'get_employee_work_report' in tools_used
                                          or (getattr(reply, 'query_context', None) or {}).get('source') in ('local_project_search', 'local_manager_report')):
                    message = AssistantReply(message, model_content='An employee report was requested by name and handled locally.')
                store.save_turn(conversation_id, identity, message, reply, reports)
            except Exception:
                if body.conversation_id is None:
                    store.delete(conversation_id, identity)
                raise
            finally:
                conversation_context.reset(usage_context)
            artifacts = [{key: report[key] for key in ('report_id', 'filename', 'format', 'download_url')}
                         for report in reports]
            return ChatResponse(data=ChatData(conversation_id=conversation_id, reply=reply,
                                mode=config.chat_mode, data_source=identity.data_source,
                                tools_used=tools_used, artifacts=artifacts, quota=await usage_status(identity)))

    @app.get('/v1/conversations')
    def conversations(identity: Identity = Depends(authenticate)):
        return {'success': True, 'data': {'conversations': app.state.store.conversations(identity)}}

    @app.get('/v1/conversations/{conversation_id}')
    def history(conversation_id: UUID, identity: Identity = Depends(authenticate)):
        return {'success': True, 'data': {'conversation_id': str(conversation_id),
                'messages': [{key: row[key] for key in ('role', 'content', 'created_at')}
                             for row in app.state.store.history(str(conversation_id), identity)],
                'artifacts': app.state.store.artifacts(str(conversation_id), identity)}}

    @app.delete('/v1/conversations/{conversation_id}', status_code=204)
    async def delete(conversation_id: UUID, identity: Identity = Depends(authenticate)):
        async with app.state.chat_lock:
            app.state.store.delete(str(conversation_id), identity)
        return Response(status_code=204)

    @app.get('/v1/reports/{report_id}/download')
    async def download(report_id: UUID, identity: Identity = Depends(authenticate)):
        report = app.state.store.get_report(str(report_id), identity)
        if app.state.backend:
            await app.state.backend.authorize_report(identity, json.loads(report['employee_ids']))
        elif not identity.can_export or not set(json.loads(report['employee_ids'])).issubset(allowed_employee_ids(identity)):
            raise HTTPException(403, 'You no longer have permission to download this report.')
        mime = ('application/pdf' if report['format'] == 'pdf' else
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        return Response(content=report['payload'], media_type=mime,
                        headers={'Content-Disposition': f'attachment; filename="{report["filename"]}"'})

    return app


app = create_app()
