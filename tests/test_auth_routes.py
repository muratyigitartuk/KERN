from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import AdminAuthMiddleware, is_break_glass_host_allowed
from app.config import settings
from app.database import connect
from app.identity import IdentityService, sanitize_local_redirect
from app.platform import PlatformStore, connect_platform_db
from app.routes import register_routes


def _build_runtime(tmp_path: Path):
    system_db = tmp_path / "kern-system.db"
    profile_root = tmp_path / "profiles"
    backup_root = tmp_path / "backups"
    platform = PlatformStore(connect_platform_db(system_db))
    profile = platform.ensure_default_profile(
        profile_root=profile_root,
        backup_root=backup_root,
        legacy_db_path=tmp_path / "legacy.db",
    )
    profile_connection = connect(Path(profile.db_path))
    runtime = SimpleNamespace(
        active_profile=profile,
        platform=platform,
        identity_service=IdentityService(platform),
        memory=SimpleNamespace(connection=profile_connection),
        orchestrator=SimpleNamespace(
            snapshot=SimpleNamespace(
                llm_available=False,
                model_info=SimpleNamespace(app_version="1.0.0-test"),
                background_components={},
                runtime_degraded_reasons=[],
                policy_mode="corporate",
                product_posture="production",
                retention_policies={},
                retention_status={},
                last_monitor_tick_at=None,
            )
        ),
        network_monitor=SimpleNamespace(status=SimpleNamespace(status="ok")),
        audit_chain_ok=True,
        audit_chain_reason=None,
        last_audit_verification_at=None,
        scheduler_service=None,
        _pending_proactive_alerts=[],
        _using_locked_scaffold=False,
    )
    return runtime, profile_connection, platform.connection


def _build_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, object]:
    runtime, profile_connection, system_connection = _build_runtime(tmp_path)
    app = FastAPI()
    app.state.runtime = runtime
    app.state.identity_service = runtime.identity_service
    app.add_middleware(AdminAuthMiddleware)
    register_routes(app, lambda: runtime)
    monkeypatch.setattr("app.auth.is_loopback_client", lambda _request: True)
    settings.admin_auth_token = "test-token"
    settings.session_secret = "test-session-secret"
    client = TestClient(app)

    def _cleanup() -> None:
        client.close()
        profile_connection.close()
        system_connection.close()

    return client, runtime, _cleanup


def test_break_glass_bootstrap_sets_session_cookie(tmp_path: Path, monkeypatch) -> None:
    client, _runtime, cleanup = _build_client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/auth/break-glass/bootstrap",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "operator", "password": "secret-pass"},
        )
    finally:
        cleanup()

    assert response.status_code == 200
    assert response.json()["admin"]["username"] == "operator"
    assert settings.session_cookie_name in response.cookies


def test_authenticated_session_can_read_session_state_and_admin_users(tmp_path: Path, monkeypatch) -> None:
    client, runtime, cleanup = _build_client(tmp_path, monkeypatch)
    try:
        organization = runtime.platform.ensure_default_organization()
        user = runtime.platform.create_user(
            email="owner@example.com",
            display_name="Owner",
            organization_id=organization.id,
            auth_source="bootstrap",
            status="active",
        )
        runtime.platform.upsert_workspace_membership(
            user_id=user.id,
            workspace_slug=runtime.active_profile.slug,
            role="org_owner",
        )
        session = runtime.platform.create_session(
            organization_id=organization.id,
            user_id=user.id,
            workspace_slug=runtime.active_profile.slug,
            auth_method="oidc",
        )
        client.cookies.set(settings.session_cookie_name, session.id)

        session_response = client.get("/auth/session")
        users_response = client.get("/admin/users")
    finally:
        cleanup()

    assert session_response.status_code == 200
    assert session_response.json()["user_id"] == user.id
    assert users_response.status_code == 200
    assert users_response.json()["users"][0]["email"] == "owner@example.com"


def test_oidc_redirect_targets_are_local_only(monkeypatch) -> None:
    service = IdentityService(platform=None)
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_client_id", "client")
    monkeypatch.setattr(settings, "oidc_redirect_uri", "http://127.0.0.1:8000/auth/oidc/callback")
    monkeypatch.setattr(
        service,
        "_oidc_discovery",
        lambda: asyncio.sleep(0, result={"authorization_endpoint": "https://idp.example/authorize"}),
    )

    assert sanitize_local_redirect("https://evil.example/phish") == "/dashboard"
    assert sanitize_local_redirect("//evil.example/phish") == "/dashboard"
    assert sanitize_local_redirect("/workbench?tab=review") == "/workbench?tab=review"

    _redirect_url, state_cookie = asyncio.run(service.begin_oidc_login(redirect_to="https://evil.example/phish"))
    state_payload = service._unsign_state(state_cookie)  # noqa: SLF001

    assert state_payload["redirect_to"] == "/dashboard"


def test_training_export_detail_rejects_path_traversal_export_id(tmp_path: Path, monkeypatch) -> None:
    client, runtime, cleanup = _build_client(tmp_path, monkeypatch)
    try:
        export_root = Path(runtime.active_profile.profile_root) / "training-exports" / "training-20260428120000"
        export_root.mkdir(parents=True)
        (export_root / "manifest.json").write_text(
            '{"id": "training-20260428120000", "artifacts": []}',
            encoding="utf-8",
        )

        ok = client.get(
            "/intelligence/training-exports/training-20260428120000",
            headers={"Authorization": "Bearer test-token"},
        )
        rejected = client.get(
            "/intelligence/training-exports/training-20260428120000%5C..%5Cmanifest",
            headers={"Authorization": "Bearer test-token"},
        )
    finally:
        cleanup()

    assert ok.status_code == 200
    assert rejected.status_code == 404


def test_oidc_rejects_subject_rebind_for_existing_user(tmp_path: Path, monkeypatch) -> None:
    runtime, profile_connection, system_connection = _build_runtime(tmp_path)
    try:
        organization = runtime.platform.ensure_default_organization()
        user = runtime.platform.create_user(
            email="owner@example.com",
            display_name="Owner",
            organization_id=organization.id,
            oidc_subject="old-subject",
            auth_source="oidc",
            status="active",
        )
        runtime.platform.upsert_workspace_membership(user_id=user.id, workspace_slug=runtime.active_profile.slug, role="org_owner")
        service = runtime.identity_service
        monkeypatch.setattr(service, "exchange_code", lambda code: asyncio.sleep(0, result={"id_token": "token"}))
        monkeypatch.setattr(
            service,
            "_verify_id_token",
            lambda token, nonce: asyncio.sleep(
                0,
                result={
                    "sub": "new-subject",
                    settings.oidc_email_claim: "owner@example.com",
                    settings.oidc_name_claim: "Owner",
                },
            ),
        )
        signed_state = service._sign_state(  # noqa: SLF001
            {"state": "state", "nonce": "nonce", "redirect_to": "/dashboard", "exp": datetime.now(timezone.utc).timestamp() + 600}
        )

        with pytest.raises(HTTPException) as exc:
            asyncio.run(service.complete_oidc_login(code="code", state="state", signed_state=signed_state))
    finally:
        profile_connection.close()
        system_connection.close()

    assert exc.value.status_code == 403


def test_sessions_expire_after_idle_timeout(tmp_path: Path, monkeypatch) -> None:
    runtime, profile_connection, system_connection = _build_runtime(tmp_path)
    try:
        organization = runtime.platform.ensure_default_organization()
        session = runtime.platform.create_session(
            organization_id=organization.id,
            auth_method="oidc",
            ttl_seconds=8 * 60 * 60,
        )
        stale = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        runtime.platform.connection.execute(
            "UPDATE user_sessions SET last_activity_at = ? WHERE id = ?",
            (stale, session.id),
        )
        runtime.platform.connection.commit()
        monkeypatch.setattr(settings, "session_idle_minutes", 30)

        assert runtime.platform.get_session(session.id, touch=True) is None
    finally:
        profile_connection.close()
        system_connection.close()


def test_server_break_glass_allowlist_supports_exact_ip_and_cidr(monkeypatch) -> None:
    monkeypatch.setattr(settings, "server_mode", True)
    monkeypatch.setattr(settings, "server_break_glass_enabled", True)
    monkeypatch.setattr(settings, "break_glass_ip_allowlist", "203.0.113.7, 10.10.0.0/16")

    assert is_break_glass_host_allowed("203.0.113.7") is True
    assert is_break_glass_host_allowed("10.10.4.8") is True
    assert is_break_glass_host_allowed("198.51.100.9") is False


def test_server_break_glass_login_uses_short_ttl_and_audit(tmp_path: Path, monkeypatch) -> None:
    runtime, profile_connection, system_connection = _build_runtime(tmp_path)
    try:
        monkeypatch.setattr(settings, "server_mode", True)
        monkeypatch.setattr(settings, "session_ttl_hours", 8)
        runtime.platform.create_break_glass_admin("operator", "secret-pass")

        session_id, context = runtime.identity_service.login_break_glass(
            "operator",
            "secret-pass",
            workspace_slug=runtime.active_profile.slug,
        )
        session = runtime.platform.get_session(session_id)
        audit = runtime.platform.connection.execute(
            "SELECT * FROM audit_events WHERE category = ? AND action = ?",
            ("auth", "break_glass_login"),
        ).fetchone()
    finally:
        profile_connection.close()
        system_connection.close()

    assert context.is_break_glass is True
    assert session is not None
    assert (session.expires_at - session.issued_at) <= timedelta(minutes=15, seconds=5)
    assert audit is not None
