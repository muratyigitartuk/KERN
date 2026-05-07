from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlencode, urlparse

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicNumbers
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from fastapi import HTTPException, Request, WebSocket
from starlette.responses import Response

from app.auth import (
    has_valid_admin_token,
    is_allowed_origin,
    is_break_glass_host_allowed,
    is_break_glass_request_allowed,
    is_loopback_client,
    is_loopback_host,
    websocket_admin_token,
)
from app.config import settings
from app.types import AuthContext, ProfileSummary, UserRecord


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding_len = (-len(value)) % 4
    return base64.urlsafe_b64decode(value + ("=" * padding_len))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_local_redirect(value: str | None, *, fallback: str = "/dashboard") -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return fallback
    decoded = unquote(candidate)
    if any(ord(ch) < 32 for ch in decoded) or "\\" in decoded:
        return fallback
    parsed = urlparse(decoded)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not decoded.startswith("/") or decoded.startswith("//"):
        return fallback
    return decoded


@dataclass(slots=True)
class OIDCLoginResult:
    user: UserRecord | None
    context: AuthContext | None
    status: str
    message: str


class IdentityService:
    def __init__(self, platform=None) -> None:
        self.platform = platform
        self._discovery_cache: tuple[float, dict[str, Any]] | None = None
        self._jwks_cache: tuple[float, dict[str, Any]] | None = None
        if self.platform is not None:
            self.seed_break_glass_admin()

    def seed_break_glass_admin(self) -> None:
        if settings.server_mode and not settings.server_break_glass_enabled:
            return
        password = str(settings.break_glass_password or "").strip()
        if not password:
            return
        self.platform.create_break_glass_admin(settings.break_glass_username, password)

    def session_cookie_name(self) -> str:
        return settings.session_cookie_name

    def _cookie_secure(self, scheme: str) -> bool:
        return scheme == "https"

    def set_session_cookie(self, response: Response, session_id: str, *, secure: bool) -> None:
        response.set_cookie(
            key=self.session_cookie_name(),
            value=session_id,
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
            max_age=settings.session_ttl_hours * 60 * 60,
        )

    def clear_session_cookie(self, response: Response, *, secure: bool) -> None:
        response.delete_cookie(
            key=self.session_cookie_name(),
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
        )

    def _sign_state(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        secret = str(settings.session_secret or "").encode("utf-8")
        digest = hmac.new(secret, raw, hashlib.sha256).digest()
        return f"{_b64url_encode(raw)}.{_b64url_encode(digest)}"

    def _unsign_state(self, token: str) -> dict[str, Any] | None:
        try:
            raw_b64, sig_b64 = token.split(".", 1)
            raw = _b64url_decode(raw_b64)
            actual = _b64url_decode(sig_b64)
        except Exception:
            return None
        expected = hmac.new(str(settings.session_secret or "").encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(actual, expected):
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        expires_at = float(payload.get("exp") or 0)
        if expires_at <= time.time():
            return None
        return payload

    async def _oidc_discovery(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._discovery_cache and now - self._discovery_cache[0] < 3600:
            return self._discovery_cache[1]
        issuer = str(settings.oidc_issuer_url or "").rstrip("/")
        if not issuer:
            raise RuntimeError("OIDC issuer is not configured.")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{issuer}/.well-known/openid-configuration")
            response.raise_for_status()
            payload = response.json()
        self._discovery_cache = (now, payload)
        return payload

    async def _jwks(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._jwks_cache and now - self._jwks_cache[0] < 3600:
            return self._jwks_cache[1]
        discovery = await self._oidc_discovery()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(str(discovery["jwks_uri"]))
            response.raise_for_status()
            payload = response.json()
        self._jwks_cache = (now, payload)
        return payload

    def _jwk_public_key(self, kid: str, jwks: dict[str, Any]):
        for key in list(jwks.get("keys") or []):
            if str(key.get("kid") or "") != kid:
                continue
            kty = str(key.get("kty") or "")
            if kty == "RSA":
                e = int.from_bytes(_b64url_decode(str(key["e"])), "big")
                n = int.from_bytes(_b64url_decode(str(key["n"])), "big")
                return RSAPublicNumbers(e, n).public_key()
            if kty == "EC":
                curve_name = str(key.get("crv") or "")
                curve = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1()}.get(curve_name)
                if curve is None:
                    raise RuntimeError(f"Unsupported EC curve: {curve_name}")
                x = int.from_bytes(_b64url_decode(str(key["x"])), "big")
                y = int.from_bytes(_b64url_decode(str(key["y"])), "big")
                return EllipticCurvePublicNumbers(x, y, curve).public_key()
        raise RuntimeError("No matching OIDC signing key was found.")

    async def _verify_id_token(self, token: str, *, nonce: str) -> dict[str, Any]:
        header_raw, payload_raw, signature_raw = token.split(".", 2)
        header = json.loads(_b64url_decode(header_raw).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_raw).decode("utf-8"))
        signing_input = f"{header_raw}.{payload_raw}".encode("ascii")
        signature = _b64url_decode(signature_raw)
        jwks = await self._jwks()
        public_key = self._jwk_public_key(str(header.get("kid") or ""), jwks)
        alg = str(header.get("alg") or "")
        if alg == "RS256":
            assert isinstance(public_key, rsa.RSAPublicKey)
            public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        elif alg == "ES256":
            assert isinstance(public_key, ec.EllipticCurvePublicKey)
            public_key.verify(signature, signing_input, ec.ECDSA(hashes.SHA256()))
        else:
            raise RuntimeError(f"Unsupported OIDC signing algorithm: {alg}")
        issuer = str(settings.oidc_issuer_url or "").rstrip("/")
        if str(payload.get("iss") or "").rstrip("/") != issuer:
            raise RuntimeError("OIDC issuer mismatch.")
        audience = payload.get("aud")
        allowed_audience = str(settings.oidc_client_id or "")
        if isinstance(audience, list):
            if allowed_audience not in audience:
                raise RuntimeError("OIDC audience mismatch.")
        elif str(audience or "") != allowed_audience:
            raise RuntimeError("OIDC audience mismatch.")
        if str(payload.get("nonce") or "") != nonce:
            raise RuntimeError("OIDC nonce mismatch.")
        exp = int(payload.get("exp") or 0)
        if exp <= int(time.time()):
            raise RuntimeError("OIDC token has expired.")
        return payload

    def _allowed_domains(self) -> set[str]:
        return {item.strip().lower() for item in str(settings.oidc_allowed_email_domains or "").split(",") if item.strip()}

    def _default_workspace(self, user_id: str | None = None) -> ProfileSummary | None:
        if user_id:
            memberships = self.platform.list_workspace_memberships(user_id)
            if memberships:
                return self.platform.get_profile(memberships[0].workspace_slug)
        profiles = self.platform.list_profiles()
        return profiles[0] if profiles else None

    def _session_context_or_raise(self, session_id: str) -> AuthContext:
        context = self.platform.build_auth_context(session_id)
        if context is None:
            raise RuntimeError("Session could not be resolved.")
        return context

    def _admin_token_context(self) -> AuthContext:
        organization = self.platform.ensure_default_organization()
        workspace = self._default_workspace()
        return AuthContext(
            authenticated=True,
            auth_method="admin_token",
            organization_id=organization.id,
            workspace_id=workspace.workspace_id if workspace else None,
            workspace_slug=workspace.slug if workspace else None,
            roles=["break_glass_admin"],
            is_break_glass=True,
            is_bootstrap_token=True,
        )

    def resolve_request_context(self, request: Request) -> AuthContext | None:
        if not settings.server_mode and is_loopback_client(request) and any(has_valid_admin_token(token) for token in self._request_admin_tokens(request)):
            return self._admin_token_context()
        session_id = request.cookies.get(self.session_cookie_name())
        if not session_id:
            return None
        context = self.platform.build_auth_context(session_id)
        if context is None:
            return None
        if context.is_break_glass and not is_break_glass_request_allowed(request):
            return None
        if context.workspace_slug is None and context.user_id:
            workspace = self._default_workspace(context.user_id)
            if workspace is not None:
                self.platform.set_session_workspace(session_id, workspace.slug)
                context = self.platform.build_auth_context(session_id)
        return context

    def resolve_websocket_context(self, websocket: WebSocket) -> AuthContext | None:
        admin_token = websocket_admin_token(websocket)
        if not settings.server_mode and admin_token and is_loopback_host(websocket.client.host if websocket.client else "") and has_valid_admin_token(admin_token):
            return self._admin_token_context()
        if not is_allowed_origin(websocket.headers.get("origin")):
            return None
        session_id = websocket.cookies.get(self.session_cookie_name())
        if not session_id:
            return None
        context = self.platform.build_auth_context(session_id)
        if context and context.is_break_glass and not is_break_glass_host_allowed(websocket.client.host if websocket.client else ""):
            return None
        return context

    def _request_admin_tokens(self, request: Request) -> list[str]:
        header = request.headers.get("authorization", "")
        tokens: list[str] = []
        if header.lower().startswith("bearer "):
            tokens.append(header[7:].strip())
        query_token = request.query_params.get("token", "")
        if query_token:
            tokens.append(query_token.strip())
        return [item for item in tokens if item]

    def login_break_glass(self, username: str, password: str, *, workspace_slug: str | None = None) -> tuple[str, AuthContext]:
        admin = self.platform.authenticate_break_glass_admin(username, password)
        if admin is None:
            raise HTTPException(status_code=401, detail="Invalid break-glass credentials.")
        organization = self.platform.ensure_default_organization()
        workspace = self.platform.get_profile(workspace_slug) if workspace_slug else self._default_workspace()
        ttl_seconds = settings.session_ttl_hours * 60 * 60
        if settings.server_mode:
            ttl_seconds = min(ttl_seconds, 15 * 60)
        session = self.platform.create_session(
            organization_id=organization.id,
            auth_method="break_glass",
            workspace_slug=workspace.slug if workspace else None,
            ttl_seconds=ttl_seconds,
            metadata={"username": admin.username, "high_severity": settings.server_mode},
        )
        if hasattr(self.platform, "record_audit"):
            self.platform.record_audit(
                "auth",
                "break_glass_login",
                "warning",
                "Break-glass administrator session created.",
                profile_slug=workspace.slug if workspace else None,
                details={
                    "username": admin.username,
                    "session_id": session.id,
                    "server_mode": settings.server_mode,
                    "organization_id": organization.id,
                    "workspace_id": workspace.workspace_id if workspace else None,
                },
            )
        return session.id, self._session_context_or_raise(session.id)

    async def begin_oidc_login(self, *, redirect_to: str = "/dashboard") -> tuple[str, str]:
        if not settings.oidc_enabled:
            raise HTTPException(status_code=404, detail="OIDC login is not enabled.")
        discovery = await self._oidc_discovery()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        payload = {
            "state": state,
            "nonce": nonce,
            "redirect_to": sanitize_local_redirect(redirect_to),
            "exp": time.time() + 600,
        }
        cookie_value = self._sign_state(payload)
        params = {
            "client_id": settings.oidc_client_id,
            "response_type": "code",
            "scope": settings.oidc_scopes,
            "redirect_uri": settings.oidc_redirect_uri,
            "state": state,
            "nonce": nonce,
        }
        return f"{discovery['authorization_endpoint']}?{urlencode(params)}", cookie_value

    def enabled(self) -> bool:
        return bool(settings.oidc_enabled and settings.oidc_issuer_url and settings.oidc_client_id and settings.oidc_redirect_uri)

    async def authorization_redirect(self, return_to: str | None = None) -> tuple[str, str]:
        return await self.begin_oidc_login(redirect_to=return_to or "/dashboard")

    async def exchange_code(self, *, code: str) -> dict[str, Any]:
        discovery = await self._oidc_discovery()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                str(discovery["token_endpoint"]),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.oidc_client_id,
                    "client_secret": settings.oidc_client_secret,
                    "redirect_uri": settings.oidc_redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()

    async def verify_id_token(self, token: str, *, nonce: str) -> dict[str, Any]:
        return await self._verify_id_token(token, nonce=nonce)

    async def complete_oidc_login(self, *, code: str, state: str, signed_state: str | None) -> OIDCLoginResult:
        if not signed_state:
            raise HTTPException(status_code=400, detail="Missing OIDC state.")
        state_payload = self._unsign_state(signed_state)
        if state_payload is None or str(state_payload.get("state") or "") != state:
            raise HTTPException(status_code=400, detail="Invalid OIDC state.")
        token_payload = await self.exchange_code(code=code)
        id_token = str(token_payload.get("id_token") or "")
        claims = await self._verify_id_token(id_token, nonce=str(state_payload["nonce"]))
        email = str(claims.get(settings.oidc_email_claim) or "").strip().lower()
        if not email:
            raise HTTPException(status_code=422, detail="OIDC token did not provide an email address.")
        display_name = str(claims.get(settings.oidc_name_claim) or email.split("@", 1)[0]).strip()
        allowed_domains = self._allowed_domains()
        domain = email.split("@", 1)[-1].lower()
        organization = self.platform.ensure_default_organization()
        user = self.platform.get_user_by_email(organization.id, email)
        oidc_subject = str(claims.get("sub") or "")
        if user is None:
            active_users = [item for item in self.platform.list_users(organization.id) if item.status == "active"]
            status = "active" if not active_users and (not allowed_domains or domain in allowed_domains) else "pending"
            user = self.platform.create_user(
                email=email,
                display_name=display_name,
                organization_id=organization.id,
                oidc_subject=oidc_subject,
                auth_source="oidc",
                status=status,
            )
            if status == "active":
                for workspace in self.platform.list_profiles():
                    self.platform.upsert_workspace_membership(user_id=user.id, workspace_slug=workspace.slug, role="org_owner")
        else:
            if user.oidc_subject and user.oidc_subject != oidc_subject:
                raise HTTPException(status_code=403, detail="OIDC subject does not match the existing local account.")
            user = self.platform.bind_user_oidc_identity(
                user.id,
                oidc_subject=oidc_subject,
                display_name=display_name,
            )
        if allowed_domains and domain not in allowed_domains:
            return OIDCLoginResult(user=user, context=None, status="pending", message="Your account is pending approval for this organization.")
        if settings.oidc_required_group:
            groups_value = claims.get(settings.oidc_groups_claim) or []
            groups = groups_value if isinstance(groups_value, list) else [str(groups_value)]
            if settings.oidc_required_group not in {str(item) for item in groups}:
                return OIDCLoginResult(user=user, context=None, status="pending", message="Your account is missing the required organization group.")
        memberships = self.platform.list_workspace_memberships(user.id)
        if user.status != "active" or not memberships:
            return OIDCLoginResult(user=user, context=None, status="pending", message="Your account is pending workspace approval.")
        workspace = self.platform.get_profile(memberships[0].workspace_slug)
        session = self.platform.create_session(
            organization_id=organization.id,
            auth_method="oidc",
            user_id=user.id,
            workspace_slug=workspace.slug if workspace else None,
            ttl_seconds=settings.session_ttl_hours * 60 * 60,
        )
        return OIDCLoginResult(
            user=user,
            context=self._session_context_or_raise(session.id),
            status="authenticated",
            message=sanitize_local_redirect(str(state_payload.get("redirect_to") or "/dashboard")),
        )

    def logout(self, session_id: str | None) -> None:
        if session_id:
            self.platform.revoke_session(session_id)

    def select_workspace(self, context: AuthContext, workspace_slug: str) -> AuthContext:
        if context.is_break_glass or context.is_bootstrap_token:
            updated = self.platform.set_session_workspace(str(context.session_id), workspace_slug) if context.session_id else None
            if updated is None:
                raise HTTPException(status_code=404, detail="Session not found.")
            resolved = self.platform.build_auth_context(updated.id)
            if resolved is None:
                raise HTTPException(status_code=403, detail="Workspace selection failed.")
            return resolved
        if not self.platform.has_workspace_access(context.user_id, workspace_slug):
            raise HTTPException(status_code=403, detail="You are not assigned to that workspace.")
        updated = self.platform.set_session_workspace(str(context.session_id), workspace_slug) if context.session_id else None
        if updated is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        resolved = self.platform.build_auth_context(updated.id)
        if resolved is None:
            raise HTTPException(status_code=403, detail="Workspace selection failed.")
        return resolved

    def list_accessible_workspaces(self, context: AuthContext) -> list[ProfileSummary]:
        if context.is_break_glass or context.is_bootstrap_token:
            return self.platform.list_profiles()
        if not context.user_id:
            return []
        memberships = self.platform.list_workspace_memberships(context.user_id)
        workspaces: list[ProfileSummary] = []
        seen: set[str] = set()
        for membership in memberships:
            if membership.workspace_slug in seen:
                continue
            workspace = self.platform.get_profile(membership.workspace_slug)
            if workspace is not None:
                workspaces.append(workspace)
                seen.add(membership.workspace_slug)
        return workspaces


class OIDCService(IdentityService):
    pass


def session_cookie_options(*, max_age: int | None = None) -> dict[str, object]:
    secure = str(settings.oidc_redirect_uri or "").startswith("https://")
    options: dict[str, object] = {
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
    }
    if max_age is not None:
        options["max_age"] = max_age
    else:
        options["max_age"] = settings.session_ttl_hours * 60 * 60
    return options


def decode_state_cookie(value: str) -> dict[str, Any]:
    service = IdentityService(platform=None)
    payload = service._unsign_state(value)
    if payload is None:
        raise HTTPException(status_code=400, detail="OIDC state validation failed.")
    return payload
