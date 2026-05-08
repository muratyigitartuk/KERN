from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from app.logging_config import setup_logging

setup_logging()  # configure logging before any other app import

from fastapi import FastAPI, Request, WebSocket  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.routes import export_logs, register_routes  # noqa: E402, F401
from app.runtime import KernRuntime
from app.runtime_manager import RuntimeManager
from app.ws_handlers import websocket_endpoint

runtime_manager = RuntimeManager()
runtime = KernRuntime(profile_slug=runtime_manager.default_workspace_slug)
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
app.state.runtime = runtime
app.state.platform = runtime_manager.platform
app.state.runtime_manager = runtime_manager


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": "Internal server error"}, status_code=500)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
register_routes(app, lambda: runtime_manager)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    workspace_slug = websocket.query_params.get("workspace_slug") or runtime_manager.default_workspace_slug
    runtime = await runtime_manager.get_runtime(workspace_slug)
    await websocket_endpoint(websocket, runtime, workspace_checked=True)
