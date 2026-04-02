from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

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
        title="Primary workspace",
        slug="default",
    )
    identity = IdentityService(platform)
    runtime = SimpleNamespace(
        platform=platform,
        active_profile=profile,
        identity_service=identity,
    )
    return runtime


def _client_for_runtime(runtime) -> TestClient:
    app = FastAPI()
    app.state.platform = runtime.platform
    app.state.identity_service = runtime.identity_service
    register_routes(app, lambda: runtime)
    return TestClient(app)


def _create_active_user(runtime, *, email: str = "owner@example.com", role: str = "org_owner") -> str:
    org = runtime.platform.ensure_default_organization()
    user = runtime.platform.create_user(
        email=email,
        display_name="Owner",
        organization_id=org.id,
        auth_source="bootstrap",
        status="active",
    )
    runtime.platform.upsert_workspace_membership(user_id=user.id, workspace_slug=runtime.active_profile.slug, role=role)
    session = runtime.platform.create_session(
        organization_id=org.id,
        user_id=user.id,
        workspace_slug=runtime.active_profile.slug,
        auth_method="oidc",
    )
    return session.id


def test_session_workspace_listing_and_switch(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
        title="Finance",
        slug="finance",
    )
    session_id = _create_active_user(runtime)
    runtime.platform.upsert_workspace_membership(user_id=runtime.platform.list_users()[0].id, workspace_slug="finance", role="member")
    client = _client_for_runtime(runtime)
    client.cookies.set("kern_session", session_id)

    workspaces = client.get("/auth/session/workspaces")
    assert workspaces.status_code == 200
    assert {item["slug"] for item in workspaces.json()["workspaces"]} == {"default", "finance"}

    switched = client.post("/auth/session/select-workspace", json={"workspace_slug": "finance"})
    assert switched.status_code == 200
    assert switched.json()["workspace_slug"] == "finance"


def test_admin_can_approve_and_suspend_user(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    org = runtime.platform.ensure_default_organization()
    pending = runtime.platform.create_user(
        email="pending@example.com",
        display_name="Pending",
        organization_id=org.id,
        auth_source="oidc",
        status="pending",
    )
    client = _client_for_runtime(runtime)
    client.cookies.set("kern_session", session_id)

    approved = client.post(f"/admin/users/{pending.id}/approve", json={"workspace_slug": "default", "role": "member"})
    assert approved.status_code == 200
    assert approved.json()["user"]["status"] == "active"

    suspended = client.post(f"/admin/users/{pending.id}/suspend")
    assert suspended.status_code == 200
    assert suspended.json()["user"]["status"] == "suspended"


def test_legal_hold_blocks_erasure_request(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    org = runtime.platform.ensure_default_organization()
    target_user = runtime.platform.create_user(
        email="subject@example.com",
        display_name="Subject",
        organization_id=org.id,
        auth_source="oidc",
        status="active",
    )
    runtime.platform.create_legal_hold(
        organization_id=org.id,
        workspace_slug=runtime.active_profile.slug,
        target_user_id=target_user.id,
        reason="Tax evidence retention",
    )
    client = _client_for_runtime(runtime)
    client.cookies.set("kern_session", session_id)

    response = client.post(
        "/compliance/erasure-requests",
        json={"target_user_id": target_user.id, "workspace_slug": runtime.active_profile.slug, "reason": "GDPR request"},
    )
    assert response.status_code == 200
    assert response.json()["erasure_request"]["status"] == "blocked"
