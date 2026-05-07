from __future__ import annotations

import hmac
import ipaddress
import os
from types import SimpleNamespace
from typing import Iterable
from urllib.parse import urlparse

from fastapi import HTTPException, Request, WebSocket
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.config import settings
from app.path_safety import validate_workspace_slug
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


def _allowlist_hosts(value: str) -> list[str]:
    return [item.strip().lower() for item in str(value or "").split(",") if item.strip()]


def _host_matches_allowlist(host: str | None, allowlist: list[str]) -> bool:
    normalized = _normalize_host(host)
    if not normalized:
        return False
    try:
        candidate_ip = ipaddress.ip_address(normalized)
    except ValueError:
        candidate_ip = None
    for entry in allowlist:
        if "/" in entry and candidate_ip is not None:
            try:
                if candidate_ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        if normalized == _normalize_host(entry):
            return True
    return False


def is_break_glass_host_allowed(host: str | None) -> bool:
    if settings.server_mode:
        if not settings.server_break_glass_enabled:
            return False
        return _host_matches_allowlist(host, _allowlist_hosts(settings.break_glass_ip_allowlist))
    return is_loopback_host(host)


def is_break_glass_request_allowed(request: Request) -> bool:
    if not settings.server_mode:
        return is_loopback_client(request)
    return is_break_glass_host_allowed(request.client.host if request.client else "")


def _candidate_admin_tokens(request: Request) -> list[str]:
    if settings.server_mode:
        return []
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


def _runtime_from_scope(scope) -> object | None:
    app = getattr(scope, "app", None)
    state = getattr(app, "state", None)
    runtime = getattr(state, "runtime", None)
    if runtime is not None:
        return runtime
    platform = getattr(state, "platform", None)
    return getattr(platform, "runtime", None)


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


def _loopback_bootstrap_context(request: Request, platform) -> AuthContext | None:
    if settings.server_mode:
        return None
    if not getattr(settings, "disable_auth_for_loopback", False):
        return None
    if not is_loopback_client(request):
        return None
    organization_id = None
    workspace_id = None
    workspace_slug = None
    runtime = _runtime_from_scope(request)
    preferred_workspace_slug = None
    raw_workspace_slug = request.cookies.get("kern_workspace_slug")
    if raw_workspace_slug:
        try:
            candidate = validate_workspace_slug(raw_workspace_slug)
            if platform is None or platform.get_profile(candidate) is not None:
                preferred_workspace_slug = candidate
        except Exception:
            preferred_workspace_slug = None
    if platform is not None and hasattr(platform, "ensure_default_organization"):
        try:
            organization = platform.ensure_default_organization()
            organization_id = getattr(organization, "id", None)
        except Exception:
            organization_id = None
    if preferred_workspace_slug and platform is not None:
        try:
            workspace = platform.get_profile(preferred_workspace_slug)
            if workspace is not None:
                workspace_id = getattr(workspace, "workspace_id", None)
                workspace_slug = getattr(workspace, "slug", None)
                organization_id = getattr(workspace, "organization_id", None) or organization_id
        except Exception:
            workspace_id = None
            workspace_slug = None
    if runtime is not None:
        try:
            workspace = getattr(runtime, "active_profile", None)
            workspace_id = workspace_id or getattr(workspace, "workspace_id", None)
            workspace_slug = workspace_slug or getattr(workspace, "slug", None)
        except Exception:
            workspace_id = None
            workspace_slug = None
    return AuthContext(
        authenticated=True,
        auth_method="break_glass",
        organization_id=organization_id,
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        roles=["break_glass_admin"],
        is_bootstrap_token=True,
        is_break_glass=True,
    )


def request_auth_context(request: Request) -> AuthContext | None:
    platform = _platform_from_scope(request)
    context = _session_context_from_platform(platform, _session_cookie_from_request(request))
    if context is not None:
        if context.is_break_glass and not is_break_glass_request_allowed(request):
            return None
        return context
    loopback_context = _loopback_bootstrap_context(request, platform)
    if loopback_context is not None:
        return loopback_context
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
    if "PYTEST_CURRENT_TEST" in os.environ:
        hosts.add("testserver")
    for item in str(settings.network_allowed_hosts or "").split(","):
        host = _normalize_host(item)
        if not host:
            continue
        if ":" in host and not host.startswith("["):
            host = host.split(":", 1)[0]
        hosts.add(host)
    return hosts


def allowed_origin_hosts() -> set[str]:
    return set(_allowed_origin_hosts())


def is_allowed_host(host: str | None) -> bool:
    parsed = _normalize_host(host)
    if not parsed:
        return False
    if parsed.startswith("[") and "]" in parsed:
        parsed = parsed[1:parsed.index("]")]
    elif ":" in parsed:
        parsed = parsed.split(":", 1)[0]
    return parsed in _allowed_origin_hosts()


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
        if session_context.is_break_glass and not is_break_glass_host_allowed(client_host):
            raise HTTPException(status_code=403, detail="Break-glass access is not allowed from this address.")
        origin = websocket.headers.get("origin")
        if not origin:
            raise HTTPException(status_code=403, detail="Origin header is required for WebSocket connections.")
        if not is_allowed_origin(origin):
            raise HTTPException(status_code=403, detail="Origin is not allowed.")
        websocket.state.auth_context = session_context
        return
    if settings.server_mode:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if getattr(settings, "disable_auth_for_loopback", False) and is_loopback_host(client_host):
        origin = websocket.headers.get("origin")
        if not origin:
            raise HTTPException(status_code=403, detail="Origin header is required for WebSocket connections.")
        if not is_allowed_origin(origin):
            raise HTTPException(status_code=403, detail="Origin is not allowed.")
        ws_nonce = ""
        cookie_header = websocket.headers.get("cookie", "")
        for item in cookie_header.split(";"):
            name, _, value = item.strip().partition("=")
            if name == "kern_loopback" and value:
                ws_nonce = value
                break
        if ws_nonce and hmac.compare_digest(ws_nonce, getattr(settings, "loopback_nonce", "")):
            websocket.state.auth_context = AuthContext(
                authenticated=True,
                auth_method="break_glass",
                roles=["break_glass_admin"],
                is_bootstrap_token=True,
                is_break_glass=True,
            )
            return
    if not is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="Control plane is limited to loopback clients.")
    if not has_valid_admin_token(websocket_admin_token(websocket)):
        raise HTTPException(status_code=401, detail="Missing or invalid admin token.")
    origin = websocket.headers.get("origin")
    if not origin:
        raise HTTPException(status_code=403, detail="Origin header is required for WebSocket connections.")
    if not is_allowed_origin(origin):
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
            if path in _LOOPBACK_ONLY_PUBLIC_PATHS and not is_break_glass_request_allowed(request):
                return JSONResponse({"detail": "Break-glass access is not allowed from this address."}, status_code=403)
            return await call_next(request)
        context = request_auth_context(request)
        if context is None:
            return JSONResponse({"detail": "Missing or invalid credentials."}, status_code=401)
        request.state.auth_context = context
        if context.auth_method == "admin_token" and not is_loopback_client(request):
            return JSONResponse({"detail": "Bootstrap admin token is limited to loopback clients."}, status_code=403)
        if context.is_break_glass and not is_break_glass_request_allowed(request):
            return JSONResponse({"detail": "Break-glass access is not allowed from this address."}, status_code=403)
        response = await call_next(request)
        if (
            getattr(settings, "disable_auth_for_loopback", False)
            and context.is_bootstrap_token
            and is_loopback_client(request)
        ):
            response.set_cookie(
                "kern_loopback",
                getattr(settings, "loopback_nonce", ""),
                httponly=True,
                samesite="lax",
                secure=False,
                path="/",
            )
        return response


class StrictHostMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not is_allowed_host(request.headers.get("host", "")):
            return JSONResponse({"detail": "Host is not allowed."}, status_code=400)
        return await call_next(request)


class ServerModeRouteGuardMiddleware(BaseHTTPMiddleware):
    _ALLOWED_PREFIXES = (
        "/auth/",
        "/admin/",
        "/workspaces",
        "/threads/",
        "/health",
        "/metrics",
        "/static/",
    )
    _ALLOWED_EXACT = frozenset({
        "/",
        "/dashboard",
        "/login",
        "/sw.js",
        "/api/version",
    })

    def _is_allowed_path(self, path: str) -> bool:
        if path in self._ALLOWED_EXACT:
            return True
        for prefix in self._ALLOWED_PREFIXES:
            if prefix.endswith("/"):
                if path.startswith(prefix):
                    return True
            elif path == prefix or path.startswith(f"{prefix}/"):
                return True
        return False

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not settings.server_mode:
            return await call_next(request)
        path = request.url.path
        if self._is_allowed_path(path):
            return await call_next(request)
        return JSONResponse(
            {
                "detail": (
                    "This endpoint is disabled in server mode until its persistence, authorization, "
                    "and audit path is migrated to the server runtime."
                )
            },
            status_code=503,
        )


def redact_error_detail(_detail: object = None) -> dict[str, str]:
    return {"detail": "The request could not be completed."}


def iter_allowed_roots(extra_roots: Iterable[str]) -> list[str]:
    return [root for root in extra_roots if root]
