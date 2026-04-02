"""Request/connection tracing for KERN.

Provides a ``request_id`` context variable that is set by middleware for
HTTP requests and by the WebSocket handler for WS connections.  All
log records automatically include the ``request_id`` when the filter is
installed via ``setup_logging()``.
"""

from __future__ import annotations

import contextvars
import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def generate_request_id() -> str:
    """Return a short unique request identifier."""
    return uuid.uuid4().hex[:12]


class RequestTracingMiddleware(BaseHTTPMiddleware):
    """Attach a unique ``X-Request-ID`` to every HTTP request/response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("X-Request-ID") or generate_request_id()
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            request_id_var.reset(token)


class _RequestIdFilter(logging.Filter):
    """Inject ``request_id`` into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()  # type: ignore[attr-defined]
        return True


def install_request_id_filter() -> None:
    """Add the request-id filter to the root logger."""
    root = logging.getLogger()
    if not any(isinstance(f, _RequestIdFilter) for f in root.filters):
        root.addFilter(_RequestIdFilter())
