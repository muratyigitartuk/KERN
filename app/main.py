from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from app.logging_config import setup_logging

setup_logging()  # configure logging before any other app import

from fastapi import FastAPI, Request, WebSocket  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.auth import AdminAuthMiddleware, ServerModeRouteGuardMiddleware, StrictHostMiddleware, ensure_websocket_allowed, redact_error_detail
from app.routes import export_logs, register_routes  # noqa: E402, F401
from app.runtime import KernRuntime
from app.runtime_manager import RuntimeManager
from app.server_runtime import ServerRuntimeManager
from app.server_ws import server_websocket_endpoint
from app.ws_handlers import websocket_endpoint
from app.config import settings

runtime_manager = ServerRuntimeManager() if settings.server_mode else RuntimeManager()
runtime = None if settings.server_mode else KernRuntime(profile_slug=runtime_manager.default_workspace_slug)
if runtime is not None:
    runtime_manager._runtimes[runtime.active_profile.slug] = runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime_manager.start()
    app.state.runtime = await runtime_manager.get_runtime(runtime_manager.default_workspace_slug)
    yield
    await runtime_manager.stop()


app = FastAPI(lifespan=lifespan)

from app.csrf import CSRFMiddleware  # noqa: E402
from app.body_limit import BodySizeLimitMiddleware  # noqa: E402
from app.rate_limit import RateLimitMiddleware  # noqa: E402
from app.tracing import RequestTracingMiddleware, install_request_id_filter  # noqa: E402

install_request_id_filter()
app.add_middleware(RequestTracingMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(AdminAuthMiddleware)
app.add_middleware(ServerModeRouteGuardMiddleware)
app.add_middleware(StrictHostMiddleware)
app.state.runtime = runtime
app.state.platform = runtime_manager.platform
app.state.identity_service = runtime_manager.identity_service
app.state.runtime_manager = runtime_manager


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(redact_error_detail(), status_code=500)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
register_routes(app, lambda: runtime_manager)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    ensure_websocket_allowed(websocket)
    auth_context = getattr(websocket.state, "auth_context", None)
    if settings.server_mode:
        thread_id = websocket.query_params.get("thread_id")
        if not thread_id:
            await websocket.close(code=1008, reason="thread_id is required in server mode.")
            return
        workspace_slug = getattr(auth_context, "workspace_slug", None) or runtime_manager.default_workspace_slug
        runtime = await runtime_manager.get_runtime(workspace_slug)
        await server_websocket_endpoint(websocket, runtime, thread_id=thread_id)
        return
    workspace_slug = getattr(auth_context, "workspace_slug", None) or runtime_manager.default_workspace_slug
    runtime = await runtime_manager.get_runtime(workspace_slug)
    await websocket_endpoint(websocket, runtime, auth_checked=True)
