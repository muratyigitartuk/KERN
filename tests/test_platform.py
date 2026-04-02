from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.database import get_schema_version
from app.backup import BackupService
from app.database import connect
from app.memory import MemoryRepository
from app.platform import PlatformStore, connect_platform_db
from app.types import BackupTarget, ProfileSummary


def test_platform_store_creates_default_profile_and_copies_legacy_db(tmp_path: Path):
    legacy_db = tmp_path / "legacy.db"
    legacy_repo = MemoryRepository(connect(legacy_db))
    legacy_repo.create_note("legacy-note")

    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=legacy_db,
    )

    assert Path(profile.db_path).exists()
    copied_repo = MemoryRepository(connect(Path(profile.db_path)))
    assert copied_repo.list_notes(limit=5)[0] == "legacy-note"


def test_platform_store_lock_and_unlock_cycle(tmp_path: Path):
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    platform.set_profile_pin(profile.slug, "1234")

    locked = platform.lock_profile(profile.slug, reason="Manual lock")
    failed = platform.unlock_profile(profile.slug, pin="0000")
    success = platform.unlock_profile(profile.slug, pin="1234")

    assert locked.unlocked is False
    assert failed.unlocked is False
    assert success.unlocked is True


def test_platform_secret_resolution_is_profile_scoped(tmp_path: Path):
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )

    secret = platform.store_secret(profile.slug, "email:test:password", "super-secret")

    assert platform.resolve_secret(secret.id, profile_slug=profile.slug) == "super-secret"
    assert platform.resolve_secret(secret.id, profile_slug="other-profile") is None


def test_profile_wrapped_secret_requires_unlock_after_restart(tmp_path: Path):
    db_path = tmp_path / "kern-system.db"
    platform = PlatformStore(connect_platform_db(db_path))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    platform.set_profile_pin(profile.slug, "1234")
    secret = platform.store_secret(profile.slug, "email:test:password", "wrapped-secret")
    assert platform.resolve_secret(secret.id, profile_slug=profile.slug) == "wrapped-secret"

    restarted = PlatformStore(connect_platform_db(db_path))

    assert restarted.is_profile_locked(profile.slug) is True
    assert restarted.resolve_secret(secret.id, profile_slug=profile.slug) is None
    unlocked = restarted.unlock_profile(profile.slug, "1234")
    assert unlocked.unlocked is True
    assert restarted.resolve_secret(secret.id, profile_slug=profile.slug) == "wrapped-secret"


def test_backup_service_creates_encrypted_backup_payload(tmp_path: Path):
    pytest.importorskip("cryptography")
    profile_root = tmp_path / "profiles" / "default"
    profile_root.mkdir(parents=True)
    (profile_root / "note.txt").write_text("sensitive profile payload", encoding="utf-8")
    target = BackupTarget(kind="local_folder", path=str(tmp_path / "exports"), label="Exports", writable=True)
    profile = ProfileSummary(
        slug="default",
        title="Primary profile",
        profile_root=str(profile_root),
        db_path=str(profile_root / "kern.db"),
        documents_root=str(profile_root / "documents"),
        attachments_root=str(profile_root / "attachments"),
        archives_root=str(profile_root / "archives"),
        meetings_root=str(profile_root / "meetings"),
        backups_root=str(tmp_path / "exports"),
        has_pin=False,
    )

    backup_path = BackupService().create_encrypted_profile_backup(profile=profile, target=target, password="secret-pass")

    raw = backup_path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert backup_path.exists()
    assert "sensitive profile payload" not in raw
    assert payload["profile_slug"] == "default"
    assert "ciphertext" in payload


def test_platform_rotate_profile_db_key_reopens_encrypted_database(tmp_path: Path):
    pytest.importorskip("cryptography")
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    security_state = platform.ensure_profile_db_encryption(profile.slug, mode="fernet")
    old_key = platform.resolve_secret(str(security_state["db_key_ref"]), profile_slug=profile.slug)
    assert old_key is not None

    connection = connect(
        Path(profile.db_path),
        encryption_mode="fernet",
        encryption_key=old_key,
        key_version=int(security_state["key_version"]),
        key_derivation_version="v1",
    )
    repo = MemoryRepository(connection)
    repo.create_note("pre-rotation note")

    rotated = platform.rotate_profile_db_key(profile.slug, profile_connection=connection)
    connection.close()

    new_key = platform.resolve_secret(str(rotated["db_key_ref"]), profile_slug=profile.slug)
    assert new_key is not None
    assert rotated["db_key_available"] is True
    assert rotated["key_version"] == int(security_state["key_version"]) + 1

    reopened = connect(
        Path(profile.db_path),
        encryption_mode="fernet",
        encryption_key=new_key,
        key_version=int(rotated["key_version"]),
        key_derivation_version="v1",
    )
    reopened_repo = MemoryRepository(reopened)
    assert "pre-rotation note" in reopened_repo.list_notes(limit=5)


def test_platform_counts_jobs_by_status(tmp_path: Path):
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    queued = platform.create_job("queued_job", "Queued job", profile_slug=profile.slug)
    completed = platform.create_job("completed_job", "Completed job", profile_slug=profile.slug)
    platform.update_job(completed.id, status="completed")

    counts = platform.count_jobs(profile.slug)

    assert counts["queued"] == 1
    assert counts["completed"] == 1
    assert counts["failed"] == 0


def test_platform_repairs_legacy_audit_chain_rows(tmp_path: Path):
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    platform.connection.execute("UPDATE audit_events SET prev_hash = NULL, event_hash = NULL")
    platform.connection.commit()

    repaired = platform.repair_legacy_audit_chain()
    ok, reason = platform.verify_audit_chain()

    assert repaired is True
    assert ok is True
    assert reason is None
    first = platform.connection.execute(
        "SELECT prev_hash, event_hash FROM audit_events WHERE profile_slug = ? ORDER BY id ASC LIMIT 1",
        (profile.slug,),
    ).fetchone()
    assert first["prev_hash"] is None
    assert first["event_hash"]


def test_platform_connection_stamps_supported_system_schema_version(tmp_path: Path):
    connection = connect_platform_db(tmp_path / "kern-system.db")

    assert get_schema_version(connection) >= 13


def test_platform_bootstraps_default_organization_workspace_and_membership_context(tmp_path: Path):
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    organization = platform.ensure_default_organization()
    user = platform.create_user(
        email="owner@example.com",
        display_name="Owner",
        organization_id=organization.id,
        auth_source="bootstrap",
        status="active",
    )
    membership = platform.upsert_workspace_membership(
        user_id=user.id,
        workspace_slug=profile.slug,
        role="org_owner",
    )
    session = platform.create_session(
        organization_id=organization.id,
        user_id=user.id,
        workspace_slug=profile.slug,
        auth_method="oidc",
    )
    context = platform.build_auth_context(session.id)

    assert profile.workspace_id is not None
    assert profile.organization_id == organization.id
    assert membership.workspace_slug == profile.slug
    assert context is not None
    assert context.user_id == user.id
    assert context.workspace_slug == profile.slug
    assert "org_owner" in context.roles


def test_platform_break_glass_admin_can_authenticate_and_create_session(tmp_path: Path):
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    admin = platform.create_break_glass_admin("operator", "secret-pass")
    authed = platform.authenticate_break_glass_admin("operator", "secret-pass")
    session = platform.create_session(
        organization_id=platform.ensure_default_organization().id,
        auth_method="break_glass",
        workspace_slug=profile.slug,
        metadata={"username": admin.username},
    )
    context = platform.build_auth_context(session.id)

    assert authed is not None
    assert context is not None
    assert context.is_break_glass is True
    assert context.workspace_slug == profile.slug
