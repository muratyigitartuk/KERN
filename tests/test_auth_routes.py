from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import AdminAuthMiddleware
from app.config import settings
from app.database import connect
from app.identity import IdentityService
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
        tts=None,
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
