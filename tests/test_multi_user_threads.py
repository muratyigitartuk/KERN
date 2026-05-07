from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import AdminAuthMiddleware, ServerModeRouteGuardMiddleware
from app.config import settings
from app.database import connect
from app.identity import IdentityService
from app.platform import PlatformStore, connect_platform_db
from app.routes import register_routes
from app.config_validation import validate_settings
from app.rate_limit import RateLimitMiddleware


def _build_client(tmp_path: Path) -> tuple[TestClient, object, object, object, object]:
    platform = PlatformStore(connect_platform_db(tmp_path / "system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    profile_connection = connect(Path(profile.db_path))
    runtime = SimpleNamespace(
        active_profile=profile,
        platform=platform,
        identity_service=IdentityService(platform),
        memory=SimpleNamespace(connection=profile_connection),
        orchestrator=SimpleNamespace(snapshot=SimpleNamespace()),
    )
    app = FastAPI()
    app.state.runtime = runtime
    app.state.identity_service = runtime.identity_service
    app.add_middleware(AdminAuthMiddleware)
    register_routes(app, lambda: runtime)
    settings.session_secret = "test-session-secret"
    return TestClient(app), runtime, platform, profile_connection, platform.connection


def _create_member_session(platform: PlatformStore, workspace_slug: str, email: str, role: str = "member") -> str:
    organization = platform.ensure_default_organization()
    user = platform.create_user(
        email=email,
        display_name=email.split("@", 1)[0],
        organization_id=organization.id,
        auth_source="oidc",
        status="active",
    )
    platform.upsert_workspace_membership(user_id=user.id, workspace_slug=workspace_slug, role=role)
    session = platform.create_session(
        organization_id=organization.id,
        user_id=user.id,
        workspace_slug=workspace_slug,
        auth_method="oidc",
    )
    return session.id


def test_private_thread_messages_are_not_visible_to_other_workspace_member(tmp_path: Path) -> None:
    client, runtime, platform, profile_connection, system_connection = _build_client(tmp_path)
    try:
        user_a_session = _create_member_session(platform, runtime.active_profile.slug, "a@example.com")
        user_b_session = _create_member_session(platform, runtime.active_profile.slug, "b@example.com")

        client.cookies.set(settings.session_cookie_name, user_a_session)
        thread_response = client.post(
            f"/workspaces/{runtime.active_profile.slug}/threads",
            json={"title": "A private thread"},
        )
        assert thread_response.status_code == 200
        thread_id = thread_response.json()["thread"]["id"]
        message_response = client.post(
            f"/threads/{thread_id}/messages",
            json={"content": "private user A content"},
        )
        assert message_response.status_code == 200

        client.cookies.set(settings.session_cookie_name, user_b_session)
        list_response = client.get(f"/workspaces/{runtime.active_profile.slug}/threads")
        read_response = client.get(f"/threads/{thread_id}/messages")
    finally:
        client.close()
        profile_connection.close()
        system_connection.close()

    assert list_response.status_code == 200
    assert list_response.json()["threads"] == []
    assert read_response.status_code == 404


def test_shared_thread_is_visible_to_workspace_member_after_owner_shares(tmp_path: Path) -> None:
    client, runtime, platform, profile_connection, system_connection = _build_client(tmp_path)
    try:
        user_a_session = _create_member_session(platform, runtime.active_profile.slug, "a@example.com")
        user_b_session = _create_member_session(platform, runtime.active_profile.slug, "b@example.com")

        client.cookies.set(settings.session_cookie_name, user_a_session)
        thread_id = client.post(
            f"/workspaces/{runtime.active_profile.slug}/threads",
            json={"title": "Shared later"},
        ).json()["thread"]["id"]
        assert client.post(f"/threads/{thread_id}/share").status_code == 200

        client.cookies.set(settings.session_cookie_name, user_b_session)
        list_response = client.get(f"/workspaces/{runtime.active_profile.slug}/threads")
    finally:
        client.close()
        profile_connection.close()
        system_connection.close()

    assert list_response.status_code == 200
    assert [thread["id"] for thread in list_response.json()["threads"]] == [thread_id]


def test_non_owner_cannot_promote_private_thread_memory(tmp_path: Path) -> None:
    client, runtime, platform, profile_connection, system_connection = _build_client(tmp_path)
    try:
        user_a_session = _create_member_session(platform, runtime.active_profile.slug, "a@example.com")
        user_b_session = _create_member_session(platform, runtime.active_profile.slug, "b@example.com")

        client.cookies.set(settings.session_cookie_name, user_a_session)
        thread_id = client.post(
            f"/workspaces/{runtime.active_profile.slug}/threads",
            json={"title": "Private memory source"},
        ).json()["thread"]["id"]

        client.cookies.set(settings.session_cookie_name, user_b_session)
        promote_response = client.post(
            f"/threads/{thread_id}/promote-memory",
            json={"key": "customer_context", "value": "private content"},
        )
    finally:
        client.close()
        profile_connection.close()
        system_connection.close()

    assert promote_response.status_code == 403


def test_shared_thread_member_cannot_promote_owner_memory(tmp_path: Path) -> None:
    client, runtime, platform, profile_connection, system_connection = _build_client(tmp_path)
    try:
        user_a_session = _create_member_session(platform, runtime.active_profile.slug, "a@example.com")
        user_b_session = _create_member_session(platform, runtime.active_profile.slug, "b@example.com")

        client.cookies.set(settings.session_cookie_name, user_a_session)
        thread_id = client.post(
            f"/workspaces/{runtime.active_profile.slug}/threads",
            json={"title": "Shared but owner-controlled"},
        ).json()["thread"]["id"]
        assert client.post(f"/threads/{thread_id}/share").status_code == 200

        client.cookies.set(settings.session_cookie_name, user_b_session)
        promote_response = client.post(
            f"/threads/{thread_id}/promote-memory",
            json={"key": "shared_summary", "value": "content from shared thread"},
        )
    finally:
        client.close()
        profile_connection.close()
        system_connection.close()

    assert promote_response.status_code == 403


def test_public_thread_api_rejects_system_audit_visibility(tmp_path: Path) -> None:
    client, runtime, platform, profile_connection, system_connection = _build_client(tmp_path)
    try:
        owner_session = _create_member_session(platform, runtime.active_profile.slug, "owner@example.com", role="org_owner")
        client.cookies.set(settings.session_cookie_name, owner_session)

        response = client.post(
            f"/workspaces/{runtime.active_profile.slug}/threads",
            json={"title": "Internal audit", "visibility": "system_audit"},
        )
    finally:
        client.close()
        profile_connection.close()
        system_connection.close()

    assert response.status_code == 400


def test_server_mode_requires_real_server_infrastructure() -> None:
    settings_like = SimpleNamespace(
        product_posture="production",
        policy_mode="corporate",
        admin_auth_token="",
        oidc_enabled=False,
        session_secret="secret",
        server_mode=True,
        postgres_dsn="",
        redis_url="",
        oidc_issuer_url="",
        oidc_client_id="",
        oidc_redirect_uri="",
        encryption_key_provider="",
        object_storage_root="",
        network_allowed_hosts="",
        public_base_url="",
        disable_auth_for_loopback=True,
        server_break_glass_enabled=False,
        break_glass_ip_allowlist="",
        break_glass_password="",
        cognition_backend="hybrid",
        db_encryption_mode="fernet",
        sync_mode="off",
        ui_language="en",
        ocr_engine="paddleocr",
        llm_enabled=False,
        llm_local_only=True,
        llama_server_url="http://127.0.0.1:8080",
        allow_cloud_llm=False,
        timezone="UTC",
        llm_max_tokens=1024,
        llm_context_window=8192,
        prompt_cache_size=24,
        snapshot_dirty_debounce_ms=120,
        context_clipboard_max_chars=280,
        network_monitor_interval=30,
        scheduler_retry_delay_minutes=10,
        scheduler_max_retries=2,
        scheduler_stale_run_minutes=45,
        session_ttl_hours=8,
        session_idle_minutes=60,
        retention_documents_days=3650,
        retention_email_days=730,
        retention_transcripts_days=365,
        retention_audit_days=2555,
        retention_backups_days=365,
        retention_run_interval_hours=12,
        rag_top_k=12,
        rag_rerank_top_n=4,
        inbox_watch_interval=300,
        proactive_scan_interval=600,
        ocr_min_text_chars_per_page=32,
        heartbeat_seconds=2.0,
        monitor_interval_seconds=0.35,
        context_refresh_seconds=1.5,
        capability_refresh_seconds=3.0,
        llama_server_timeout=120.0,
        llm_temperature=0.3,
        tts_speed=1.0,
        ocr_low_confidence_threshold=0.8,
        rag_min_score=0.1,
        llama_server_model_path=None,
        license_public_key_path=None,
    )

    errors = validate_settings(settings_like)

    assert "KERN_POSTGRES_DSN must be set when KERN_SERVER_MODE=true" in errors
    assert "KERN_REDIS_URL must be set when KERN_SERVER_MODE=true" in errors
    assert "KERN_OIDC_ENABLED=true is required when KERN_SERVER_MODE=true" in errors
    assert "KERN_DISABLE_AUTH_FOR_LOOPBACK=false is required when KERN_SERVER_MODE=true" in errors


def test_server_mode_break_glass_requires_password_when_enabled() -> None:
    settings_like = SimpleNamespace(
        product_posture="production",
        policy_mode="corporate",
        admin_auth_token="",
        oidc_enabled=True,
        session_secret="secret",
        server_mode=True,
        postgres_dsn="postgresql://db/kern",
        redis_url="redis://redis:6379/0",
        oidc_issuer_url="https://idp.example",
        oidc_client_id="kern",
        oidc_redirect_uri="https://kern.example/auth/oidc/callback",
        encryption_key_provider="vault",
        object_storage_root="s3://kern",
        network_allowed_hosts="kern.example",
        public_base_url="https://kern.example",
        disable_auth_for_loopback=False,
        server_break_glass_enabled=True,
        break_glass_ip_allowlist="203.0.113.7",
        break_glass_password="",
        cognition_backend="hybrid",
        db_encryption_mode="fernet",
        sync_mode="off",
        ui_language="en",
        ocr_engine="paddleocr",
        llm_enabled=False,
        llm_local_only=True,
        llama_server_url="http://127.0.0.1:8080",
        allow_cloud_llm=False,
        timezone="UTC",
        llm_max_tokens=1024,
        llm_context_window=8192,
        prompt_cache_size=24,
        snapshot_dirty_debounce_ms=120,
        context_clipboard_max_chars=280,
        network_monitor_interval=30,
        scheduler_retry_delay_minutes=10,
        scheduler_max_retries=2,
        scheduler_stale_run_minutes=45,
        session_ttl_hours=8,
        session_idle_minutes=60,
        retention_documents_days=3650,
        retention_email_days=730,
        retention_transcripts_days=365,
        retention_audit_days=2555,
        retention_backups_days=365,
        retention_run_interval_hours=12,
        rag_top_k=12,
        rag_rerank_top_n=4,
        inbox_watch_interval=300,
        proactive_scan_interval=600,
        ocr_min_text_chars_per_page=32,
        heartbeat_seconds=2.0,
        monitor_interval_seconds=0.35,
        context_refresh_seconds=1.5,
        capability_refresh_seconds=3.0,
        llama_server_timeout=120.0,
        llm_temperature=0.3,
        tts_speed=1.0,
        ocr_low_confidence_threshold=0.8,
        rag_min_score=0.1,
        llama_server_model_path=None,
        license_public_key_path=None,
    )

    errors = validate_settings(settings_like)

    assert "KERN_BREAK_GLASS_PASSWORD must be set when server break-glass is enabled" in errors


def test_server_mode_route_guard_blocks_unmigrated_local_subsystems(monkeypatch) -> None:
    app = FastAPI()
    app.add_middleware(ServerModeRouteGuardMiddleware)

    @app.post("/upload")
    async def upload_placeholder():
        return {"status": "unsafe"}

    @app.get("/workspaces")
    async def workspaces_placeholder():
        return {"status": "allowed"}

    @app.get("/workspaces-debug")
    async def workspaces_debug_placeholder():
        return {"status": "unsafe"}

    monkeypatch.setattr(settings, "server_mode", True)
    client = TestClient(app)
    try:
        blocked = client.post("/upload")
        allowed = client.get("/workspaces")
        boundary_blocked = client.get("/workspaces-debug")
    finally:
        client.close()

    assert blocked.status_code == 503
    assert allowed.status_code == 200
    assert boundary_blocked.status_code == 503


def test_server_mode_rate_limit_fails_closed_when_redis_unavailable(monkeypatch) -> None:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.post("/threads/thread-1/messages")
    async def append_message_placeholder():
        return {"status": "unsafe"}

    monkeypatch.setattr(settings, "server_mode", True)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:6379/15")
    monkeypatch.setattr("app.rate_limit._redis_rate_check", lambda *_args, **_kwargs: None)
    client = TestClient(app)
    try:
        response = client.post("/threads/thread-1/messages")
    finally:
        client.close()

    assert response.status_code == 503
