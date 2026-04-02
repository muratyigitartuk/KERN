from __future__ import annotations

import os
import secrets
import hmac

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.config import settings

CSRF_COOKIE_NAME = "kern_csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_TOKEN_LENGTH = 32
STATE_CHANGING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
EXEMPT_PATHS = frozenset({"/health", "/health/live", "/health/ready", "/api/version"})


def _is_enabled() -> bool:
    if settings.product_posture == "production" or settings.policy_mode == "corporate":
        return True
    return os.getenv("KERN_CSRF_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not _is_enabled():
            return await call_next(request)

        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)

        if request.method in STATE_CHANGING_METHODS and request.url.path not in EXEMPT_PATHS:
            csrf_header = request.headers.get(CSRF_HEADER_NAME, "")
            if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
                return Response(
                    content='{"detail":"CSRF token missing or invalid"}',
                    status_code=403,
                    media_type="application/json",
                )

        response = await call_next(request)

        if not csrf_cookie:
            token = secrets.token_hex(CSRF_TOKEN_LENGTH)
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=token,
                httponly=False,  # JS needs to read this to send in header
                samesite="strict",
                secure=request.url.scheme == "https",
                path="/",
            )

        return response
