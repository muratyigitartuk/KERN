from __future__ import annotations

import hmac
from types import SimpleNamespace
from typing import Iterable
from urllib.parse import urlparse

from fastapi import HTTPException, Request, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.config import settings
from app.types import AuthContext

_PUBLIC_HTTP_PATHS = frozenset({
    "/",
    "/login",
    "/dashboard",
    "/health/live",
    "/health/ready",
    "/api/version",
    "/auth/break-glass/login",
    "/auth/break-glass/bootstrap",
    "/auth/oidc/login",
    "/auth/oidc/callback",
})
_LOOPBACK_ONLY_PUBLIC_PATHS = frozenset({
    "/auth/break-glass/login",
    "/auth/break-glass/bootstrap",
})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _normalize_host(value: str | None) -> str:
    return (value or "").strip().lower().rstrip(".")


def is_loopback_host(value: str | None) -> bool:
    return _normalize_host(value) in _LOOPBACK_HOSTS


def is_loopback_client(request: Request) -> bool:
    client_host = request.client.host if request.client else ""
    return is_loopback_host(client_host)


def _candidate_admin_tokens(request: Request) -> list[str]:
    header = request.headers.get("authorization", "")
    query_token = request.query_params.get("token", "")
    tokens: list[str] = []
    if header.lower().startswith("bearer "):
        tokens.append(header[7:].strip())
    if query_token:
        tokens.append(query_token.strip())
    return [token for token in tokens if token]


def has_valid_admin_token(token: str | None) -> bool:
    expected = str(settings.admin_auth_token or "").strip()
    candidate = str(token or "").strip()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


def request_has_valid_admin_token(request: Request) -> bool:
    return any(has_valid_admin_token(token) for token in _candidate_admin_tokens(request))


def _platform_from_scope(scope) -> object | None:
    app = getattr(scope, "app", None)
    state = getattr(app, "state", None)
    platform = getattr(state, "platform", None)
    if platform is not None:
        return platform
    runtime = getattr(state, "runtime", None)
    return getattr(runtime, "platform", None)


def _session_cookie_from_request(request: Request) -> str | None:
    return request.cookies.get(settings.session_cookie_name)


def _session_cookie_from_websocket(websocket: WebSocket) -> str | None:
    cookie_header = websocket.headers.get("cookie", "")
    for item in cookie_header.split(";"):
        name, _, value = item.strip().partition("=")
        if name == settings.session_cookie_name and value:
            return value
    return None


def _session_context_from_platform(platform, session_id: str | None) -> AuthContext | None:
    if platform is None or not session_id or not hasattr(platform, "build_auth_context"):
        return None
    try:
        return platform.build_auth_context(session_id)
    except Exception:
        return None


def request_auth_context(request: Request) -> AuthContext | None:
    platform = _platform_from_scope(request)
    context = _session_context_from_platform(platform, _session_cookie_from_request(request))
    if context is not None:
        return context
    if request_has_valid_admin_token(request):
        organization_id = None
        if platform is not None and hasattr(platform, "ensure_default_organization"):
            organization = platform.ensure_default_organization()
            organization_id = organization.id
        return AuthContext(
            authenticated=True,
            auth_method="admin_token",
            organization_id=organization_id,
            roles=["break_glass_admin"],
            is_bootstrap_token=True,
            is_break_glass=True,
        )
    return None


def require_request_auth_context(request: Request) -> AuthContext:
    context = request_auth_context(request)
    if context is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return context


def _allowed_origin_hosts() -> set[str]:
    hosts = set(_LOOPBACK_HOSTS)
    for item in str(settings.network_allowed_hosts or "").split(","):
        host = _normalize_host(item)
        if not host:
            continue
        if ":" in host and not host.startswith("["):
            host = host.split(":", 1)[0]
        hosts.add(host)
    return hosts


def is_allowed_origin(origin: str | None) -> bool:
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    return _normalize_host(parsed.hostname) in _allowed_origin_hosts()


def websocket_admin_token(websocket: WebSocket) -> str | None:
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    query_token = websocket.query_params.get("token", "")
    return query_token.strip() or None


def ensure_websocket_allowed(websocket: WebSocket) -> None:
    if not hasattr(websocket, "state"):
        websocket.state = SimpleNamespace()
    platform = _platform_from_scope(websocket)
    client = getattr(websocket, "client", None)
    client_host = client.host if client else ""
    session_context = _session_context_from_platform(platform, _session_cookie_from_websocket(websocket))
    if session_context is not None:
        if session_context.is_break_glass and not is_loopback_host(client_host):
            raise HTTPException(status_code=403, detail="Break-glass access is limited to loopback clients.")
        origin = websocket.headers.get("origin")
        if origin and not is_allowed_origin(origin):
            raise HTTPException(status_code=403, detail="Origin is not allowed.")
        websocket.state.auth_context = session_context
        return
    if not is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="Control plane is limited to loopback clients.")
    if not has_valid_admin_token(websocket_admin_token(websocket)):
        raise HTTPException(status_code=401, detail="Missing or invalid admin token.")
    origin = websocket.headers.get("origin")
    if origin and not is_allowed_origin(origin):
        raise HTTPException(status_code=403, detail="Origin is not allowed.")
    organization_id = None
    if platform is not None and hasattr(platform, "ensure_default_organization"):
        organization = platform.ensure_default_organization()
        organization_id = organization.id
    websocket.state.auth_context = AuthContext(
        authenticated=True,
        auth_method="admin_token",
        organization_id=organization_id,
        roles=["break_glass_admin"],
        is_bootstrap_token=True,
        is_break_glass=True,
    )


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith("/static/") or path in _PUBLIC_HTTP_PATHS:
            if path in _LOOPBACK_ONLY_PUBLIC_PATHS and not is_loopback_client(request):
                return JSONResponse({"detail": "Break-glass access is limited to loopback clients."}, status_code=403)
            return await call_next(request)
        context = request_auth_context(request)
        if context is None:
            return JSONResponse({"detail": "Missing or invalid credentials."}, status_code=401)
        request.state.auth_context = context
        if context.auth_method == "admin_token" and not is_loopback_client(request):
            return JSONResponse({"detail": "Bootstrap admin token is limited to loopback clients."}, status_code=403)
        if context.is_break_glass and not is_loopback_client(request):
            return JSONResponse({"detail": "Break-glass access is limited to loopback clients."}, status_code=403)
        return await call_next(request)


def redact_error_detail(_detail: object = None) -> dict[str, str]:
    return {"detail": "The request could not be completed."}


def iter_allowed_roots(extra_roots: Iterable[str]) -> list[str]:
    return [root for root in extra_roots if root]
