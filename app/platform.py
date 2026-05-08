from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

from app.path_safety import validate_workspace_slug
from app.types import (
    AuditEvent,
    BackgroundJob,
    BackupTarget,
    DataExportRecord,
    EvidenceManifest,
    ErasureRequestRecord,
    ErasureExecutionStep,
    LegalHoldRecord,
    OrganizationRecord,
    ProfileSession,
    ProfileSummary,
    RecoveryCheckpoint,
    RetentionDecision,
    RetentionPolicyRecord,
    SecretRef,
    MessageRecord,
    ThreadRecord,
    UserRecord,
    WorkspaceMembershipRecord,
)

_SECRET_CACHE_TTL = int(os.environ.get("KERN_SECRET_CACHE_TTL_SECONDS", "300"))
SYSTEM_SCHEMA_VERSION = 14


class _TTLCache:
    """Simple dict-like cache with per-entry TTL expiration."""

    __slots__ = ("_store", "_ttl")

    def __init__(self, ttl: int = _SECRET_CACHE_TTL):
        self._store: dict = {}
        self._ttl = ttl

    def get(self, key, default=None):
        entry = self._store.get(key)
        if entry is None:
            return default
        value, ts = entry
        if time.monotonic() - ts > self._ttl:
            self._store.pop(key, None)
            return default
        return value

    def __contains__(self, key) -> bool:
        return self.get(key) is not None

    def __setitem__(self, key, value):
        self._store[key] = (value, time.monotonic())

    def __getitem__(self, key):
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def pop(self, key, *args):
        entry = self._store.pop(key, None)
        if entry is None:
            if args:
                return args[0]
            raise KeyError(key)
        return entry[0]

    def items(self):
        now = time.monotonic()
        return [(k, v) for k, (v, ts) in self._store.items() if now - ts <= self._ttl]

    def clear(self):
        self._store.clear()


PLATFORM_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_meta (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT,
    organization_id TEXT,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    profile_root TEXT NOT NULL,
    db_path TEXT NOT NULL,
    documents_root TEXT NOT NULL,
    attachments_root TEXT NOT NULL,
    archives_root TEXT NOT NULL,
    meetings_root TEXT NOT NULL,
    backups_root TEXT NOT NULL,
    pin_hash TEXT,
    pin_salt TEXT,
    wrapped_profile_key TEXT,
    profile_key_salt TEXT,
    profile_key_derivation_version TEXT NOT NULL DEFAULT 'v1',
    db_encryption_mode TEXT NOT NULL DEFAULT 'off',
    db_key_ref TEXT,
    db_key_version INTEGER NOT NULL DEFAULT 0,
    artifact_key_ref TEXT,
    artifact_key_version INTEGER NOT NULL DEFAULT 0,
    artifact_migration_state TEXT NOT NULL DEFAULT 'not enabled',
    last_key_rotation TEXT,
    locked INTEGER NOT NULL DEFAULT 0,
    last_unlocked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    profile_slug TEXT,
    category TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    prev_hash TEXT,
    event_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS background_jobs (
    id TEXT PRIMARY KEY,
    profile_slug TEXT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_stage TEXT,
    recoverable INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_background_jobs_updated_at ON background_jobs(updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS backup_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_slug TEXT,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    label TEXT NOT NULL,
    writable INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    profile_slug TEXT,
    stage TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_checkpoints_job_id ON recovery_checkpoints(job_id, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS profile_secrets (
    id TEXT PRIMARY KEY,
    profile_slug TEXT NOT NULL,
    name TEXT NOT NULL,
    scheme TEXT NOT NULL DEFAULT 'platform',
    encrypted_value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profile_secrets_profile_name ON profile_secrets(profile_slug, name, updated_at DESC);

CREATE TABLE IF NOT EXISTS audit_retention_anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_slug TEXT,
    created_at TEXT NOT NULL,
    before_iso TEXT NOT NULL,
    retained_from TEXT,
    dropped_count INTEGER NOT NULL DEFAULT 0,
    last_removed_hash TEXT,
    last_removed_created_at TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_retention_anchors_profile_created_at
    ON audit_retention_anchors(profile_slug, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_email ON users(organization_id, email);
CREATE INDEX IF NOT EXISTS idx_users_org_status ON users(organization_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS workspace_memberships (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    workspace_slug TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_memberships_unique
    ON workspace_memberships(workspace_id, user_id, role);
CREATE INDEX IF NOT EXISTS idx_workspace_memberships_user
    ON workspace_memberships(user_id, workspace_slug, updated_at DESC);

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    workspace_slug TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_owner
    ON threads(organization_id, workspace_slug, owner_user_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_threads_workspace_visibility
    ON threads(organization_id, workspace_slug, visibility, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS thread_participants (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    workspace_slug TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_participants_unique
    ON thread_participants(thread_id, user_id);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    workspace_slug TEXT NOT NULL,
    actor_user_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread_created
    ON messages(thread_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_actor
    ON messages(organization_id, workspace_slug, actor_user_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS workspace_memory_items (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    workspace_slug TEXT NOT NULL,
    source_thread_id TEXT,
    promoted_by_user_id TEXT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspace_memory_scope
    ON workspace_memory_items(organization_id, workspace_slug, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS private_memory_items (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    workspace_slug TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source_thread_id TEXT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_private_memory_scope
    ON private_memory_items(organization_id, workspace_slug, user_id, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS retention_policies (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    data_class TEXT NOT NULL,
    retention_days INTEGER NOT NULL,
    legal_hold_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_retention_policies_org_class
    ON retention_policies(organization_id, data_class);

CREATE TABLE IF NOT EXISTS legal_holds (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    workspace_slug TEXT,
    target_user_id TEXT,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_legal_holds_org_active
    ON legal_holds(organization_id, active, updated_at DESC);

CREATE TABLE IF NOT EXISTS erasure_requests (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    target_user_id TEXT NOT NULL,
    requested_by_user_id TEXT,
    workspace_slug TEXT,
    status TEXT NOT NULL DEFAULT 'requested',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_erasure_requests_org_status
    ON erasure_requests(organization_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS data_exports (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    workspace_slug TEXT,
    target_user_id TEXT,
    requested_by_user_id TEXT,
    status TEXT NOT NULL DEFAULT 'requested',
    artifact_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_data_exports_org_status
    ON data_exports(organization_id, status, updated_at DESC);
"""


def _set_platform_schema_version(connection: sqlite3.Connection, version: int = SYSTEM_SCHEMA_VERSION) -> None:
    connection.execute(
        """
        INSERT INTO schema_meta (key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(version),),
    )
    connection.execute(
        """
        INSERT INTO platform_meta (key, value)
        VALUES ('platform_schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(version),),
    )


def connect_platform_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(PLATFORM_SCHEMA)
    _set_platform_schema_version(connection)
    connection.commit()
    return connection


_PBKDF2_ITERATIONS = 600_000  # OWASP 2024 recommendation for PBKDF2-SHA256
_LEGACY_PIN_ITERATIONS = [200_000]  # Old iteration counts for migration
_LEGACY_KDF_ITERATIONS = [390_000]


def _hash_pin(pin: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return digest.hex(), salt.hex()


def _verify_pin(pin: str, stored_hash: str, salt_hex: str) -> bool:
    """Verify a PIN against its hash, supporting legacy iteration counts."""
    salt = bytes.fromhex(salt_hex)
    # Try current iteration count first
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    if hmac.compare_digest(digest.hex(), stored_hash):
        return True
    # Try legacy iteration counts for migration
    for legacy_count in _LEGACY_PIN_ITERATIONS:
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, legacy_count)
        if hmac.compare_digest(digest.hex(), stored_hash):
            return True
    return False


def _derive_fernet_key(secret: str, salt: bytes, *, iterations: int = _PBKDF2_ITERATIONS) -> bytes:
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations, dklen=32)
    return base64.urlsafe_b64encode(digest)


class PlatformStore:
    def __init__(self, connection: sqlite3.Connection, audit_enabled: bool = True) -> None:
        self.connection = connection
        self.audit_enabled = audit_enabled
        self.secret_key_path = self._default_secret_key_path()
        self._secret_cache: _TTLCache = _TTLCache()
        self._profile_master_cache: _TTLCache = _TTLCache()
        self._ensure_platform_compat()
        self._invalidate_wrapped_profile_sessions()
        self.recover_stale_jobs()

    def _default_secret_key_path(self) -> Path:
        row = self.connection.execute("PRAGMA database_list").fetchone()
        db_file = Path(row["file"]) if row and row["file"] else Path("kern-system.db")
        return db_file.with_suffix(".key")

    def _ensure_platform_compat(self) -> None:
        self._ensure_column("profiles", "workspace_id", "TEXT")
        self._ensure_column("profiles", "organization_id", "TEXT")
        self._ensure_column("audit_events", "prev_hash", "TEXT")
        self._ensure_column("audit_events", "event_hash", "TEXT")
        self._ensure_column("background_jobs", "checkpoint_stage", "TEXT")
        self._ensure_column("background_jobs", "recoverable", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("background_jobs", "error_code", "TEXT")
        self._ensure_column("background_jobs", "error_message", "TEXT")
        self._ensure_column("profiles", "db_encryption_mode", "TEXT NOT NULL DEFAULT 'off'")
        self._ensure_column("profiles", "wrapped_profile_key", "TEXT")
        self._ensure_column("profiles", "profile_key_salt", "TEXT")
        self._ensure_column("profiles", "profile_key_derivation_version", "TEXT NOT NULL DEFAULT 'v1'")
        self._ensure_column("profiles", "db_key_ref", "TEXT")
        self._ensure_column("profiles", "db_key_version", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("profiles", "artifact_key_ref", "TEXT")
        self._ensure_column("profiles", "artifact_key_version", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("profiles", "artifact_migration_state", "TEXT NOT NULL DEFAULT 'not enabled'")
        self._ensure_column("profiles", "last_key_rotation", "TEXT")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_secrets (
                id TEXT PRIMARY KEY,
                profile_slug TEXT NOT NULL,
                name TEXT NOT NULL,
                scheme TEXT NOT NULL DEFAULT 'platform',
                encrypted_value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._ensure_column("profile_secrets", "scheme", "TEXT NOT NULL DEFAULT 'platform'")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_profile_secrets_profile_name ON profile_secrets(profile_slug, name, updated_at DESC)"
        )
        self._ensure_column("recovery_checkpoints", "profile_slug", "TEXT")
        self.connection.execute("UPDATE recovery_checkpoints SET profile_slug = COALESCE(profile_slug, 'default')")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_recovery_checkpoints_profile_slug ON recovery_checkpoints(profile_slug, updated_at DESC, id DESC)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_retention_anchors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_slug TEXT,
                created_at TEXT NOT NULL,
                before_iso TEXT NOT NULL,
                retained_from TEXT,
                dropped_count INTEGER NOT NULL DEFAULT 0,
                last_removed_hash TEXT,
                last_removed_created_at TEXT,
                details_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_retention_anchors_profile_created_at ON audit_retention_anchors(profile_slug, created_at DESC, id DESC)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS organizations (
                id TEXT PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                email TEXT NOT NULL,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            )
            """
        )
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_email ON users(organization_id, email)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_users_org_status ON users(organization_id, status, updated_at DESC)")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_memberships (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                workspace_slug TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_memberships_unique ON workspace_memberships(workspace_id, user_id, role)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_workspace_memberships_user ON workspace_memberships(user_id, workspace_slug, updated_at DESC)"
        )
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                workspace_slug TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'private',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_threads_owner
                ON threads(organization_id, workspace_slug, owner_user_id, updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_threads_workspace_visibility
                ON threads(organization_id, workspace_slug, visibility, updated_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS thread_participants (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                organization_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                workspace_slug TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'owner',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_participants_unique
                ON thread_participants(thread_id, user_id);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                organization_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                workspace_slug TEXT NOT NULL,
                actor_user_id TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_thread_created
                ON messages(thread_id, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_actor
                ON messages(organization_id, workspace_slug, actor_user_id, created_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS workspace_memory_items (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                workspace_slug TEXT NOT NULL,
                source_thread_id TEXT,
                promoted_by_user_id TEXT,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_memory_scope
                ON workspace_memory_items(organization_id, workspace_slug, updated_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS private_memory_items (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                workspace_slug TEXT NOT NULL,
                user_id TEXT NOT NULL,
                source_thread_id TEXT,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_private_memory_scope
                ON private_memory_items(organization_id, workspace_slug, user_id, updated_at DESC, id DESC);
            """
        )
        self._ensure_column("erasure_requests", "approved_by_user_id", "TEXT")
        self._ensure_column("erasure_requests", "retention_decision", "TEXT")
        self._ensure_column("erasure_requests", "legal_hold_decision", "TEXT")
        self._ensure_column("erasure_requests", "steps_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("erasure_requests", "artifact_refs_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("data_exports", "approved_by_user_id", "TEXT")
        self._ensure_column("data_exports", "manifest_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("data_exports", "artifact_refs_json", "TEXT NOT NULL DEFAULT '[]'")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS deletion_tombstones (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                workspace_slug TEXT,
                target_user_id TEXT,
                artifact_class TEXT NOT NULL,
                reference_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_deletion_tombstones_org_user ON deletion_tombstones(organization_id, target_user_id, created_at DESC, id DESC)"
        )
        self._backfill_workspace_metadata()
        self.connection.commit()

    def _invalidate_wrapped_profile_sessions(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            UPDATE profiles
            SET locked = 1, updated_at = ?
            WHERE wrapped_profile_key IS NOT NULL AND wrapped_profile_key != ''
            """,
            (now,),
        )
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self.connection.commit()

    def _backfill_workspace_metadata(self) -> None:
        organization = self.ensure_default_organization()
        rows = self.connection.execute("SELECT id, slug, workspace_id, organization_id FROM profiles ORDER BY id ASC").fetchall()
        for row in rows:
            workspace_id = str(row["workspace_id"] or "").strip() or f"ws-{row['id']}"
            organization_id = str(row["organization_id"] or "").strip() or organization.id
            self.connection.execute(
                "UPDATE profiles SET workspace_id = ?, organization_id = ? WHERE id = ?",
                (workspace_id, organization_id, row["id"]),
            )

    def ensure_default_profile(
        self,
        profile_root: Path,
        backup_root: Path,
        legacy_db_path: Path,
        title: str = "Primary profile",
        slug: str = "default",
    ) -> ProfileSummary:
        slug = validate_workspace_slug(slug)
        existing = self.get_profile(slug)
        if existing:
            self._ensure_profile_directories(existing)
            return existing

        profile_root = Path(profile_root).expanduser().resolve()
        backup_root = Path(backup_root).expanduser().resolve()
        root = (profile_root / slug).resolve()
        documents_root = root / "documents"
        attachments_root = root / "attachments"
        archives_root = root / "archives"
        meetings_root = root / "meetings"
        backups_root = (backup_root / slug).resolve()
        db_path = root / "kern.db"
        root.relative_to(profile_root)
        backups_root.relative_to(backup_root)
        for path in (root, documents_root, attachments_root, archives_root, meetings_root, backups_root):
            path.mkdir(parents=True, exist_ok=True)
        if legacy_db_path.exists() and legacy_db_path.resolve() != db_path.resolve() and not db_path.exists():
            self._copy_legacy_database(legacy_db_path, db_path)

        now = datetime.now(timezone.utc).isoformat()
        organization = self.ensure_default_organization()
        workspace_id = f"ws-{slug}"
        self.connection.execute(
            """
            INSERT INTO profiles (
                workspace_id, organization_id, slug, title, profile_root, db_path, documents_root, attachments_root,
                archives_root, meetings_root, backups_root, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                organization.id,
                slug,
                title,
                str(root),
                str(db_path),
                str(documents_root),
                str(attachments_root),
                str(archives_root),
                str(meetings_root),
                str(backups_root),
                now,
                now,
            ),
        )
        self.connection.commit()
        self.record_audit("profile", "profile_created", "success", "Created default profile.", profile_slug=slug)
        self.upsert_backup_target(slug, "local_folder", str(backups_root), "Profile backup folder", True)
        return self.get_profile(slug)

    def _ensure_profile_directories(self, profile: ProfileSummary) -> None:
        for path in (
            profile.profile_root,
            profile.documents_root,
            profile.attachments_root,
            profile.archives_root,
            profile.meetings_root,
            profile.backups_root,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)

    def _copy_legacy_database(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        source_connection = sqlite3.connect(source)
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()

    def ensure_default_organization(self) -> OrganizationRecord:
        row = self.connection.execute("SELECT * FROM organizations ORDER BY created_at ASC, id ASC LIMIT 1").fetchone()
        if row:
            return OrganizationRecord(
                id=str(row["id"]),
                slug=str(row["slug"]),
                name=str(row["name"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        now = datetime.now(timezone.utc).isoformat()
        organization_id = str(uuid4())
        self.connection.execute(
            "INSERT INTO organizations (id, slug, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (organization_id, "default-org", "Primary organization", now, now),
        )
        self.connection.commit()
        return OrganizationRecord(
            id=organization_id,
            slug="default-org",
            name="Primary organization",
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )

    def list_organizations(self) -> list[OrganizationRecord]:
        rows = self.connection.execute("SELECT * FROM organizations ORDER BY created_at ASC, id ASC").fetchall()
        return [
            OrganizationRecord(
                id=str(row["id"]),
                slug=str(row["slug"]),
                name=str(row["name"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def get_profile(self, slug: str) -> ProfileSummary | None:
        try:
            slug = validate_workspace_slug(slug)
        except ValueError:
            return None
        row = self.connection.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
        if not row:
            return None
        return ProfileSummary(
            workspace_id=str(row["workspace_id"] or "") or None,
            organization_id=str(row["organization_id"] or "") or None,
            slug=row["slug"],
            title=row["title"],
            profile_root=row["profile_root"],
            db_path=row["db_path"],
            documents_root=row["documents_root"],
            attachments_root=row["attachments_root"],
            archives_root=row["archives_root"],
            meetings_root=row["meetings_root"],
            backups_root=row["backups_root"],
            has_pin=bool(row["pin_hash"]),
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )

    def _profile_row(self, slug: str):
        return self.connection.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()

    def _has_wrapped_profile_key(self, slug: str) -> bool:
        row = self._profile_row(slug)
        return bool(row and row["wrapped_profile_key"])

    def _uses_profile_secret_scheme(self, profile_slug: str, name: str) -> bool:
        if name.startswith("profile-db:") or name.startswith("profile-artifacts:"):
            return False
        return self._has_wrapped_profile_key(profile_slug)

    def _profile_master_key(self, slug: str) -> str | None:
        return self._profile_master_cache.get(slug)

    def _generate_profile_master_key(self) -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")

    def _wrap_profile_master_key(self, slug: str, master_key: str, pin: str, *, derivation_version: str = "v2") -> None:
        from cryptography.fernet import Fernet

        salt = secrets.token_bytes(16)
        cipher = Fernet(_derive_fernet_key(pin, salt))
        wrapped = cipher.encrypt(master_key.encode("utf-8")).decode("ascii")
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            UPDATE profiles
            SET wrapped_profile_key = ?, profile_key_salt = ?, profile_key_derivation_version = ?, updated_at = ?
            WHERE slug = ?
            """,
            (wrapped, salt.hex(), derivation_version, now, slug),
        )
        self.connection.commit()
        self._profile_master_cache[slug] = master_key

    def _unwrap_profile_master_key(self, slug: str, pin: str) -> str | None:
        from cryptography.fernet import Fernet, InvalidToken

        row = self._profile_row(slug)
        if not row or not row["wrapped_profile_key"] or not row["profile_key_salt"]:
            return None
        cipher = Fernet(_derive_fernet_key(pin, bytes.fromhex(row["profile_key_salt"])))
        try:
            master_key = cipher.decrypt(str(row["wrapped_profile_key"]).encode("ascii")).decode("utf-8")
        except InvalidToken:
            return None
        self._profile_master_cache[slug] = master_key
        return master_key

    def _encrypt_with_profile_master(self, slug: str, value: str) -> str:
        from cryptography.fernet import Fernet

        master_key = self._profile_master_key(slug)
        if not master_key:
            raise PermissionError("Profile encryption key is not loaded.")
        return Fernet(master_key.encode("ascii")).encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt_with_profile_master(self, slug: str, encrypted_value: str) -> str | None:
        from cryptography.fernet import Fernet, InvalidToken

        master_key = self._profile_master_key(slug)
        if not master_key:
            return None
        try:
            return Fernet(master_key.encode("ascii")).decrypt(encrypted_value.encode("ascii")).decode("utf-8")
        except InvalidToken:
            return None

    def _reencrypt_profile_secrets(self, slug: str, *, to_scheme: str, master_key: str | None = None) -> None:
        rows = self.connection.execute(
            "SELECT id, name, encrypted_value, scheme FROM profile_secrets WHERE profile_slug = ?",
            (slug,),
        ).fetchall()
        machine_cipher = self._load_secret_cipher()
        for row in rows:
            name = str(row["name"])
            if name.startswith("profile-db:") or name.startswith("profile-artifacts:"):
                continue
            current_scheme = str(row["scheme"] or "platform")
            if current_scheme == to_scheme:
                continue
            if current_scheme == "profile":
                if not master_key:
                    raise RuntimeError("Profile master key is required to migrate secrets.")
                from cryptography.fernet import Fernet

                plaintext = Fernet(master_key.encode("ascii")).decrypt(str(row["encrypted_value"]).encode("ascii")).decode("utf-8")
            else:
                plaintext = machine_cipher.decrypt(str(row["encrypted_value"]).encode("ascii")).decode("utf-8")
            if to_scheme == "profile":
                if not master_key:
                    raise RuntimeError("Profile master key is required to migrate secrets.")
                encrypted = self._encrypt_with_profile_master(slug, plaintext)
            else:
                encrypted = machine_cipher.encrypt(plaintext.encode("utf-8")).decode("ascii")
            self.connection.execute(
                "UPDATE profile_secrets SET scheme = ?, encrypted_value = ?, updated_at = ? WHERE id = ?",
                (to_scheme, encrypted, datetime.now(timezone.utc).isoformat(), row["id"]),
            )
        self.connection.commit()

    def ensure_profile_db_encryption(self, slug: str, mode: str = "fernet") -> dict[str, object]:
        profile = self.get_profile(slug)
        if profile is None:
            raise RuntimeError("Unknown profile.")
        row = self.connection.execute(
            "SELECT db_encryption_mode, db_key_ref, db_key_version, last_key_rotation FROM profiles WHERE slug = ?",
            (slug,),
        ).fetchone()
        key_ref = row["db_key_ref"] if row else None
        key_version = int(row["db_key_version"] or 0) if row else 0
        if not key_ref:
            secret_value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
            secret = self.store_secret(slug, f"profile-db:{slug}:master", secret_value)
            key_ref = secret.id
            key_version = 1
            rotated_at = datetime.now(timezone.utc).isoformat()
            self.connection.execute(
                """
                UPDATE profiles
                SET db_encryption_mode = ?, db_key_ref = ?, db_key_version = ?, last_key_rotation = ?, updated_at = ?
                WHERE slug = ?
                """,
                (mode, key_ref, key_version, rotated_at, rotated_at, slug),
            )
            self.connection.commit()
        elif row and row["db_encryption_mode"] != mode:
            now = datetime.now(timezone.utc).isoformat()
            self.connection.execute(
                "UPDATE profiles SET db_encryption_mode = ?, updated_at = ? WHERE slug = ?",
                (mode, now, slug),
            )
            self.connection.commit()
        return self.get_profile_security_state(slug)

    def ensure_profile_artifact_encryption(self, slug: str) -> dict[str, object]:
        profile = self.get_profile(slug)
        if profile is None:
            raise RuntimeError("Unknown profile.")
        row = self.connection.execute(
            "SELECT artifact_key_ref, artifact_key_version, artifact_migration_state FROM profiles WHERE slug = ?",
            (slug,),
        ).fetchone()
        key_ref = row["artifact_key_ref"] if row else None
        key_version = int(row["artifact_key_version"] or 0) if row else 0
        migration_state = row["artifact_migration_state"] if row else "not enabled"
        if not key_ref:
            secret_value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
            secret = self.store_secret(slug, f"profile-artifacts:{slug}:master", secret_value)
            key_ref = secret.id
            key_version = 1
            migration_state = "pending"
            now = datetime.now(timezone.utc).isoformat()
            self.connection.execute(
                """
                UPDATE profiles
                SET artifact_key_ref = ?, artifact_key_version = ?, artifact_migration_state = ?, updated_at = ?
                WHERE slug = ?
                """,
                (key_ref, key_version, migration_state, now, slug),
            )
            self.connection.commit()
        return self.get_profile_security_state(slug)

    def set_artifact_migration_state(self, slug: str, state: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "UPDATE profiles SET artifact_migration_state = ?, updated_at = ? WHERE slug = ?",
            (state, now, slug),
        )
        self.connection.commit()

    def rotate_profile_db_key(self, slug: str, profile_connection: sqlite3.Connection | None = None) -> dict[str, object]:
        profile = self.get_profile(slug)
        if profile is None:
            raise RuntimeError("Unknown profile.")
        row = self.connection.execute(
            "SELECT db_key_ref, db_key_version FROM profiles WHERE slug = ?",
            (slug,),
        ).fetchone()
        old_key_ref = row["db_key_ref"] if row else None
        old_key = self.resolve_secret(old_key_ref, profile_slug=slug) if old_key_ref else None
        secret_value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        import binascii
        try:
            from cryptography.fernet import Fernet
            Fernet(secret_value.encode("ascii"))
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError(f"Generated key failed Fernet validation: {exc}") from exc
        secret = self.store_secret(slug, f"profile-db:{slug}:master", secret_value)
        next_version = int(row["db_key_version"] or 0) + 1 if row else 1
        if profile_connection is not None and hasattr(profile_connection, "rotate_encrypted_storage"):
            profile_connection.rotate_encrypted_storage(
                fernet_key=secret_value,
                key_version=next_version,
                key_derivation_version="v1",
            )
            profile_connection.commit()
        elif old_key:
            from app.encrypted_db import rewrite_encrypted_database

            rewrite_encrypted_database(
                Path(profile.db_path),
                old_fernet_key=old_key,
                new_fernet_key=secret_value,
                key_version=next_version,
                key_derivation_version="v1",
            )
        elif Path(profile.db_path).exists():
            raise RuntimeError("Encrypted profile database key could not be resolved for rotation.")
        rotated_at = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            UPDATE profiles
            SET db_key_ref = ?, db_key_version = ?, last_key_rotation = ?, updated_at = ?
            WHERE slug = ?
            """,
            (secret.id, next_version, rotated_at, rotated_at, slug),
        )
        self.connection.commit()
        self.record_audit("security", "rotate_profile_db_key", "success", "Rotated profile database key.", profile_slug=slug)
        return self.get_profile_security_state(slug)

    def get_profile_security_state(self, slug: str) -> dict[str, object]:
        row = self.connection.execute(
            """
            SELECT db_encryption_mode, db_key_ref, db_key_version, artifact_key_ref, artifact_key_version,
                   artifact_migration_state, last_key_rotation, locked
            FROM profiles WHERE slug = ?
            """,
            (slug,),
        ).fetchone()
        if not row:
            return {
                "db_encryption_enabled": False,
                "db_encryption_mode": "off",
                "db_key_available": False,
                "artifact_encryption_enabled": False,
                "artifact_encryption_migration_state": "not enabled",
                "profile_key_loaded": False,
                "artifact_key_loaded": False,
                "key_version": 0,
                "last_key_rotation": None,
            }
        db_key_available = False
        if row["db_key_ref"]:
            try:
                db_key_available = bool(self.resolve_secret(row["db_key_ref"], profile_slug=slug, audit=False))
            except Exception as exc:
                logger.debug("Failed to resolve db_key_ref for profile %r: %s", slug, exc)
        artifact_key_available = False
        if row["artifact_key_ref"]:
            try:
                artifact_key_available = bool(self.resolve_secret(row["artifact_key_ref"], profile_slug=slug, audit=False))
            except Exception as exc:
                logger.debug("Failed to resolve artifact_key_ref for profile %r: %s", slug, exc)
        profile_key_loaded = slug in self._profile_master_cache or (slug, str(row["db_key_ref"] or "")) in self._secret_cache
        artifact_key_loaded = (slug, str(row["artifact_key_ref"] or "")) in self._secret_cache
        return {
            "db_encryption_enabled": row["db_encryption_mode"] != "off" and db_key_available,
            "db_encryption_mode": row["db_encryption_mode"] or "off",
            "db_key_available": db_key_available,
            "db_key_ref": row["db_key_ref"],
            "artifact_encryption_enabled": bool(row["artifact_key_ref"]),
            "artifact_key_available": artifact_key_available,
            "artifact_key_ref": row["artifact_key_ref"],
            "artifact_key_version": int(row["artifact_key_version"] or 0),
            "artifact_encryption_migration_state": row["artifact_migration_state"] or "not enabled",
            "profile_key_loaded": profile_key_loaded,
            "artifact_key_loaded": artifact_key_loaded,
            "key_version": int(row["db_key_version"] or 0),
            "last_key_rotation": row["last_key_rotation"],
            "locked": bool(row["locked"]),
        }

    def list_profiles(self) -> list[ProfileSummary]:
        rows = self.connection.execute("SELECT slug FROM profiles ORDER BY id ASC").fetchall()
        return [self.get_profile(row["slug"]) for row in rows if self.get_profile(row["slug"]) is not None]

    def list_workspace_memberships(self, user_id: str) -> list[WorkspaceMembershipRecord]:
        rows = self.connection.execute(
            "SELECT * FROM workspace_memberships WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
            (user_id,),
        ).fetchall()
        return [
            WorkspaceMembershipRecord(
                id=str(row["id"]),
                organization_id=str(row["organization_id"]),
                workspace_id=str(row["workspace_id"]),
                workspace_slug=str(row["workspace_slug"]),
                user_id=str(row["user_id"]),
                role=str(row["role"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def create_user(
        self,
        *,
        email: str,
        display_name: str,
        organization_id: str | None = None,
        status: str = "pending",
    ) -> UserRecord:
        organization = self.ensure_default_organization() if not organization_id else None
        resolved_org_id = organization_id or organization.id
        existing = self.get_user_by_email(resolved_org_id, email)
        if existing:
            return existing
        user_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            INSERT INTO users (id, organization_id, email, display_name, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, resolved_org_id, email.strip().lower(), display_name.strip(), status, now, now),
        )
        self.connection.commit()
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> UserRecord | None:
        row = self.connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        return UserRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            email=str(row["email"]),
            display_name=str(row["display_name"]),
            status=str(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            deleted_at=datetime.fromisoformat(row["deleted_at"]) if row["deleted_at"] else None,
        )

    def get_user_by_email(self, organization_id: str, email: str) -> UserRecord | None:
        row = self.connection.execute(
            "SELECT id FROM users WHERE organization_id = ? AND email = ? LIMIT 1",
            (organization_id, email.strip().lower()),
        ).fetchone()
        return self.get_user(str(row["id"])) if row else None

    def list_users(self, organization_id: str | None = None) -> list[UserRecord]:
        resolved_org_id = organization_id or self.ensure_default_organization().id
        rows = self.connection.execute(
            "SELECT id FROM users WHERE organization_id = ? ORDER BY updated_at DESC, id DESC",
            (resolved_org_id,),
        ).fetchall()
        return [self.get_user(str(row["id"])) for row in rows if self.get_user(str(row["id"])) is not None]

    def set_user_status(self, user_id: str, status: str) -> UserRecord:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute("UPDATE users SET status = ?, updated_at = ? WHERE id = ?", (status, now, user_id))
        self.connection.commit()
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("Unknown user.")
        return user

    def upsert_workspace_membership(self, *, user_id: str, workspace_slug: str, role: str) -> WorkspaceMembershipRecord:
        profile = self.get_profile(workspace_slug)
        if profile is None or not profile.workspace_id or not profile.organization_id:
            raise RuntimeError("Unknown workspace.")
        now = datetime.now(timezone.utc).isoformat()
        existing = self.connection.execute(
            """
            SELECT id FROM workspace_memberships
            WHERE workspace_id = ? AND user_id = ? AND role = ?
            LIMIT 1
            """,
            (profile.workspace_id, user_id, role),
        ).fetchone()
        membership_id = str(existing["id"]) if existing else str(uuid4())
        self.connection.execute(
            """
            INSERT INTO workspace_memberships (id, organization_id, workspace_id, workspace_slug, user_id, role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                organization_id = excluded.organization_id,
                workspace_id = excluded.workspace_id,
                workspace_slug = excluded.workspace_slug,
                user_id = excluded.user_id,
                role = excluded.role,
                updated_at = excluded.updated_at
            """,
            (membership_id, profile.organization_id, profile.workspace_id, profile.slug, user_id, role, now, now),
        )
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM workspace_memberships WHERE id = ?", (membership_id,)).fetchone()
        return WorkspaceMembershipRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            workspace_id=str(row["workspace_id"]),
            workspace_slug=str(row["workspace_slug"]),
            user_id=str(row["user_id"]),
            role=str(row["role"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_thread(self, row: sqlite3.Row) -> ThreadRecord:
        return ThreadRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            workspace_id=str(row["workspace_id"]),
            workspace_slug=str(row["workspace_slug"]),
            owner_user_id=str(row["owner_user_id"]),
            title=str(row["title"]),
            visibility=str(row["visibility"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_message(self, row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            id=str(row["id"]),
            thread_id=str(row["thread_id"]),
            organization_id=str(row["organization_id"]),
            workspace_id=str(row["workspace_id"]),
            workspace_slug=str(row["workspace_slug"]),
            actor_user_id=str(row["actor_user_id"]) if row["actor_user_id"] else None,
            role=str(row["role"]),
            content=str(row["content"]),
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def create_thread(
        self,
        *,
        organization_id: str,
        workspace_slug: str,
        owner_user_id: str,
        title: str = "New thread",
        visibility: str = "private",
    ) -> ThreadRecord:
        if visibility not in {"private", "shared", "system_audit"}:
            raise ValueError("Unsupported thread visibility.")
        profile = self.get_profile(workspace_slug)
        if profile is None or not profile.workspace_id:
            raise RuntimeError("Unknown workspace.")
        if profile.organization_id != organization_id:
            raise RuntimeError("Workspace does not belong to the active organization.")
        if not self.has_workspace_access(owner_user_id, workspace_slug):
            raise PermissionError("User does not have workspace access.")
        now = datetime.now(timezone.utc).isoformat()
        thread_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO threads (id, organization_id, workspace_id, workspace_slug, owner_user_id, title, visibility, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (thread_id, organization_id, profile.workspace_id, profile.slug, owner_user_id, title.strip() or "New thread", visibility, now, now),
        )
        self.connection.execute(
            """
            INSERT INTO thread_participants (id, thread_id, organization_id, workspace_id, workspace_slug, user_id, role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'owner', ?, ?)
            """,
            (str(uuid4()), thread_id, organization_id, profile.workspace_id, profile.slug, owner_user_id, now, now),
        )
        self.connection.commit()
        self.record_audit(
            "conversation",
            "thread_created",
            "success",
            "Created private workspace thread." if visibility == "private" else f"Created {visibility} workspace thread.",
            profile_slug=profile.slug,
            details={"thread_id": thread_id, "actor_user_id": owner_user_id, "visibility": visibility},
        )
        row = self.connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return self._row_to_thread(row)

    def get_thread_for_user(self, thread_id: str, *, user_id: str, organization_id: str) -> ThreadRecord | None:
        row = self.connection.execute("SELECT * FROM threads WHERE id = ? AND organization_id = ?", (thread_id, organization_id)).fetchone()
        if not row:
            return None
        thread = self._row_to_thread(row)
        if thread.visibility == "system_audit":
            return None
        if thread.visibility == "shared" and self.has_workspace_access(user_id, thread.workspace_slug):
            return thread
        participant = self.connection.execute(
            "SELECT 1 FROM thread_participants WHERE thread_id = ? AND user_id = ? LIMIT 1",
            (thread_id, user_id),
        ).fetchone()
        if participant:
            return thread
        return None

    def list_threads_for_user(
        self,
        *,
        organization_id: str,
        workspace_slug: str,
        user_id: str,
        limit: int = 50,
    ) -> list[ThreadRecord]:
        if not self.has_workspace_access(user_id, workspace_slug):
            return []
        rows = self.connection.execute(
            """
            SELECT DISTINCT t.*
            FROM threads t
            LEFT JOIN thread_participants tp ON tp.thread_id = t.id AND tp.user_id = ?
            WHERE t.organization_id = ?
              AND t.workspace_slug = ?
              AND t.visibility != 'system_audit'
              AND (t.visibility = 'shared' OR tp.user_id IS NOT NULL)
            ORDER BY t.updated_at DESC, t.id DESC
            LIMIT ?
            """,
            (user_id, organization_id, workspace_slug, limit),
        ).fetchall()
        return [self._row_to_thread(row) for row in rows]

    def append_message(
        self,
        *,
        thread_id: str,
        actor_user_id: str | None,
        role: str,
        content: str,
        metadata: dict[str, object] | None = None,
        acting_user_id: str | None = None,
        organization_id: str,
    ) -> MessageRecord:
        requester = acting_user_id or actor_user_id
        if not requester:
            raise PermissionError("A user context is required to append a message.")
        thread = self.get_thread_for_user(thread_id, user_id=requester, organization_id=organization_id)
        if thread is None:
            raise PermissionError("Thread is not available to the active user.")
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("Unsupported message role.")
        now = datetime.now(timezone.utc).isoformat()
        message_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO messages (id, thread_id, organization_id, workspace_id, workspace_slug, actor_user_id, role, content, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                thread.id,
                thread.organization_id,
                thread.workspace_id,
                thread.workspace_slug,
                actor_user_id,
                role,
                content,
                json.dumps(metadata or {}, sort_keys=True),
                now,
            ),
        )
        self.connection.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread.id))
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._row_to_message(row)

    def list_messages_for_user(
        self,
        *,
        thread_id: str,
        user_id: str,
        organization_id: str,
        limit: int = 100,
    ) -> list[MessageRecord]:
        thread = self.get_thread_for_user(thread_id, user_id=user_id, organization_id=organization_id)
        if thread is None:
            return []
        rows = self.connection.execute(
            """
            SELECT *
            FROM messages
            WHERE thread_id = ? AND organization_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (thread_id, organization_id, limit),
        ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def share_thread(self, *, thread_id: str, user_id: str, organization_id: str) -> ThreadRecord:
        thread = self.get_thread_for_user(thread_id, user_id=user_id, organization_id=organization_id)
        if thread is None or thread.owner_user_id != user_id:
            raise PermissionError("Only the private thread owner can share it.")
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute("UPDATE threads SET visibility = 'shared', updated_at = ? WHERE id = ?", (now, thread_id))
        self.connection.commit()
        self.record_audit(
            "conversation",
            "thread_shared",
            "warning",
            "Private thread was shared with the workspace.",
            profile_slug=thread.workspace_slug,
            details={"thread_id": thread_id, "actor_user_id": user_id},
        )
        row = self.connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return self._row_to_thread(row)

    def promote_thread_memory(
        self,
        *,
        thread_id: str,
        user_id: str,
        organization_id: str,
        key: str,
        value: str,
        metadata: dict[str, object] | None = None,
    ) -> str:
        thread = self.get_thread_for_user(thread_id, user_id=user_id, organization_id=organization_id)
        if thread is None:
            raise PermissionError("Thread is not available to the active user.")
        if thread.owner_user_id != user_id and not self.has_workspace_access(user_id, thread.workspace_slug, "org_owner", "org_admin"):
            raise PermissionError("Only the thread owner or workspace admin can promote thread memory.")
        memory_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            INSERT INTO workspace_memory_items (
                id, organization_id, workspace_id, workspace_slug, source_thread_id,
                promoted_by_user_id, key, value, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                thread.organization_id,
                thread.workspace_id,
                thread.workspace_slug,
                thread.id,
                user_id,
                key,
                value,
                json.dumps(metadata or {}, sort_keys=True),
                now,
                now,
            ),
        )
        self.connection.commit()
        self.record_audit(
            "memory",
            "thread_memory_promoted",
            "warning",
            "Private thread content was promoted to workspace memory.",
            profile_slug=thread.workspace_slug,
            details={"thread_id": thread.id, "memory_id": memory_id, "actor_user_id": user_id},
        )
        return memory_id

    def list_workspace_users(self, workspace_slug: str) -> list[UserRecord]:
        profile = self.get_profile(workspace_slug)
        if profile is None or not profile.workspace_id:
            return []
        rows = self.connection.execute(
            "SELECT DISTINCT user_id FROM workspace_memberships WHERE workspace_id = ? ORDER BY updated_at DESC, id DESC",
            (profile.workspace_id,),
        ).fetchall()
        users: list[UserRecord] = []
        for row in rows:
            user = self.get_user(str(row["user_id"]))
            if user is not None:
                users.append(user)
        return users

    def has_workspace_access(self, user_id: str | None, workspace_slug: str, *roles: str) -> bool:
        if not user_id:
            return False
        profile = self.get_profile(workspace_slug)
        if profile is None or not profile.workspace_id:
            return False
        memberships = self.connection.execute(
            "SELECT role FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?",
            (profile.workspace_id, user_id),
        ).fetchall()
        if not memberships:
            return False
        if not roles:
            return True
        allowed = set(roles)
        return any(str(row["role"]) in allowed for row in memberships)

    def upsert_retention_policy(
        self,
        *,
        organization_id: str,
        data_class: str,
        retention_days: int,
        legal_hold_enabled: bool,
    ) -> RetentionPolicyRecord:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.connection.execute(
            "SELECT id FROM retention_policies WHERE organization_id = ? AND data_class = ?",
            (organization_id, data_class),
        ).fetchone()
        policy_id = str(existing["id"]) if existing else str(uuid4())
        self.connection.execute(
            """
            INSERT INTO retention_policies (id, organization_id, data_class, retention_days, legal_hold_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                retention_days = excluded.retention_days,
                legal_hold_enabled = excluded.legal_hold_enabled,
                updated_at = excluded.updated_at
            """,
            (policy_id, organization_id, data_class, retention_days, 1 if legal_hold_enabled else 0, now, now),
        )
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM retention_policies WHERE id = ?", (policy_id,)).fetchone()
        return RetentionPolicyRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            data_class=str(row["data_class"]),
            retention_days=int(row["retention_days"]),
            legal_hold_enabled=bool(row["legal_hold_enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_retention_policies(self, organization_id: str) -> list[RetentionPolicyRecord]:
        rows = self.connection.execute(
            "SELECT * FROM retention_policies WHERE organization_id = ? ORDER BY data_class ASC",
            (organization_id,),
        ).fetchall()
        return [
            RetentionPolicyRecord(
                id=str(row["id"]),
                organization_id=str(row["organization_id"]),
                data_class=str(row["data_class"]),
                retention_days=int(row["retention_days"]),
                legal_hold_enabled=bool(row["legal_hold_enabled"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def create_legal_hold(
        self,
        *,
        organization_id: str,
        reason: str,
        workspace_slug: str | None = None,
        target_user_id: str | None = None,
    ) -> LegalHoldRecord:
        hold_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            INSERT INTO legal_holds (id, organization_id, workspace_slug, target_user_id, reason, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (hold_id, organization_id, workspace_slug, target_user_id, reason.strip(), now, now),
        )
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM legal_holds WHERE id = ?", (hold_id,)).fetchone()
        return LegalHoldRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            workspace_slug=str(row["workspace_slug"]) if row["workspace_slug"] else None,
            target_user_id=str(row["target_user_id"]) if row["target_user_id"] else None,
            reason=str(row["reason"]),
            active=bool(row["active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_legal_holds(self, organization_id: str, *, active_only: bool = False) -> list[LegalHoldRecord]:
        if active_only:
            rows = self.connection.execute(
                "SELECT * FROM legal_holds WHERE organization_id = ? AND active = 1 ORDER BY updated_at DESC, id DESC",
                (organization_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM legal_holds WHERE organization_id = ? ORDER BY updated_at DESC, id DESC",
                (organization_id,),
            ).fetchall()
        return [
            LegalHoldRecord(
                id=str(row["id"]),
                organization_id=str(row["organization_id"]),
                workspace_slug=str(row["workspace_slug"]) if row["workspace_slug"] else None,
                target_user_id=str(row["target_user_id"]) if row["target_user_id"] else None,
                reason=str(row["reason"]),
                active=bool(row["active"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def create_erasure_request(
        self,
        *,
        organization_id: str,
        target_user_id: str,
        requested_by_user_id: str | None = None,
        workspace_slug: str | None = None,
        reason: str = "",
    ) -> ErasureRequestRecord:
        request_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        status = "requested"
        legal_hold_decision = "clear"
        if any(
            hold.active and (hold.target_user_id in {None, target_user_id}) and (hold.workspace_slug in {None, workspace_slug})
            for hold in self.list_legal_holds(organization_id, active_only=True)
        ):
            status = "blocked"
            legal_hold_decision = "blocked_by_active_hold"
        self.connection.execute(
            """
            INSERT INTO erasure_requests (
                id, organization_id, target_user_id, requested_by_user_id, workspace_slug, status, reason,
                legal_hold_decision, steps_json, artifact_refs_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', ?, ?)
            """,
            (
                request_id,
                organization_id,
                target_user_id,
                requested_by_user_id,
                workspace_slug,
                status,
                reason.strip(),
                legal_hold_decision,
                now,
                now,
            ),
        )
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM erasure_requests WHERE id = ?", (request_id,)).fetchone()
        return self._row_to_erasure_request(row)

    def list_erasure_requests(self, organization_id: str) -> list[ErasureRequestRecord]:
        rows = self.connection.execute(
            "SELECT * FROM erasure_requests WHERE organization_id = ? ORDER BY updated_at DESC, id DESC",
            (organization_id,),
        ).fetchall()
        return [self._row_to_erasure_request(row) for row in rows]

    def get_erasure_request(self, request_id: str) -> ErasureRequestRecord | None:
        row = self.connection.execute("SELECT * FROM erasure_requests WHERE id = ?", (request_id,)).fetchone()
        return self._row_to_erasure_request(row) if row else None

    def create_data_export(
        self,
        *,
        organization_id: str,
        requested_by_user_id: str | None = None,
        workspace_slug: str | None = None,
        target_user_id: str | None = None,
        status: str = "requested",
        artifact_path: str | None = None,
        approved_by_user_id: str | None = None,
        manifest: EvidenceManifest | None = None,
        artifact_refs: list[str] | None = None,
    ) -> DataExportRecord:
        export_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            INSERT INTO data_exports (
                id, organization_id, workspace_slug, target_user_id, requested_by_user_id, status,
                artifact_path, approved_by_user_id, manifest_json, artifact_refs_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                export_id,
                organization_id,
                workspace_slug,
                target_user_id,
                requested_by_user_id,
                status,
                artifact_path,
                approved_by_user_id,
                json.dumps(manifest.model_dump(mode="json")) if manifest else "{}",
                json.dumps(artifact_refs or []),
                now,
                now,
            ),
        )
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM data_exports WHERE id = ?", (export_id,)).fetchone()
        return self._row_to_data_export(row)

    def list_data_exports(self, organization_id: str) -> list[DataExportRecord]:
        rows = self.connection.execute(
            "SELECT * FROM data_exports WHERE organization_id = ? ORDER BY updated_at DESC, id DESC",
            (organization_id,),
        ).fetchall()
        return [self._row_to_data_export(row) for row in rows]

    def get_data_export(self, export_id: str) -> DataExportRecord | None:
        row = self.connection.execute("SELECT * FROM data_exports WHERE id = ?", (export_id,)).fetchone()
        return self._row_to_data_export(row) if row else None

    def update_erasure_request(
        self,
        request_id: str,
        *,
        status: str | None = None,
        approved_by_user_id: str | None = None,
        retention_decision: RetentionDecision | None = None,
        legal_hold_decision: str | None = None,
        steps: list[ErasureExecutionStep] | None = None,
        artifact_refs: list[str] | None = None,
    ) -> ErasureRequestRecord | None:
        row = self.connection.execute("SELECT * FROM erasure_requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            return None
        now = datetime.now(timezone.utc).isoformat()
        merged_steps = steps if steps is not None else self._row_to_erasure_request(row).steps
        merged_refs = artifact_refs if artifact_refs is not None else self._row_to_erasure_request(row).artifact_refs
        self.connection.execute(
            """
            UPDATE erasure_requests
            SET status = COALESCE(?, status),
                approved_by_user_id = COALESCE(?, approved_by_user_id),
                retention_decision = COALESCE(?, retention_decision),
                legal_hold_decision = COALESCE(?, legal_hold_decision),
                steps_json = ?,
                artifact_refs_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                approved_by_user_id,
                retention_decision,
                legal_hold_decision,
                json.dumps([step.model_dump(mode="json") for step in merged_steps]),
                json.dumps(merged_refs),
                now,
                request_id,
            ),
        )
        self.connection.commit()
        return self.get_erasure_request(request_id)

    def update_data_export(
        self,
        export_id: str,
        *,
        status: str | None = None,
        artifact_path: str | None = None,
        approved_by_user_id: str | None = None,
        manifest: EvidenceManifest | None = None,
        artifact_refs: list[str] | None = None,
    ) -> DataExportRecord | None:
        row = self.connection.execute("SELECT * FROM data_exports WHERE id = ?", (export_id,)).fetchone()
        if not row:
            return None
        current = self._row_to_data_export(row)
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            UPDATE data_exports
            SET status = COALESCE(?, status),
                artifact_path = COALESCE(?, artifact_path),
                approved_by_user_id = COALESCE(?, approved_by_user_id),
                manifest_json = ?,
                artifact_refs_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                artifact_path,
                approved_by_user_id,
                json.dumps((manifest or current.manifest or EvidenceManifest()).model_dump(mode="json")) if (manifest or current.manifest) else "{}",
                json.dumps(artifact_refs if artifact_refs is not None else current.artifact_refs),
                now,
                export_id,
            ),
        )
        self.connection.commit()
        return self.get_data_export(export_id)

    def record_deletion_tombstone(
        self,
        *,
        organization_id: str,
        workspace_slug: str | None,
        target_user_id: str | None,
        artifact_class: str,
        reference_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> str:
        tombstone_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO deletion_tombstones (
                id, organization_id, workspace_slug, target_user_id, artifact_class, reference_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tombstone_id,
                organization_id,
                workspace_slug,
                target_user_id,
                artifact_class,
                reference_id,
                json.dumps(metadata or {}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()
        return tombstone_id

    def list_deletion_tombstones(
        self,
        organization_id: str,
        *,
        target_user_id: str | None = None,
        workspace_slug: str | None = None,
    ) -> list[dict[str, object]]:
        query = """
            SELECT id, organization_id, workspace_slug, target_user_id, artifact_class, reference_id, metadata_json, created_at
            FROM deletion_tombstones
            WHERE organization_id = ?
        """
        params: list[object] = [organization_id]
        if target_user_id is not None:
            query += " AND target_user_id = ?"
            params.append(target_user_id)
        if workspace_slug is not None:
            query += " AND workspace_slug = ?"
            params.append(workspace_slug)
        query += " ORDER BY created_at DESC, id DESC"
        rows = self.connection.execute(query, tuple(params)).fetchall()
        return [
            {
                "id": str(row["id"]),
                "organization_id": str(row["organization_id"]),
                "workspace_slug": str(row["workspace_slug"]) if row["workspace_slug"] else None,
                "target_user_id": str(row["target_user_id"]) if row["target_user_id"] else None,
                "artifact_class": str(row["artifact_class"]),
                "reference_id": str(row["reference_id"]) if row["reference_id"] else None,
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _row_to_erasure_request(self, row: sqlite3.Row) -> ErasureRequestRecord:
        raw_steps = json.loads(row["steps_json"] or "[]") if "steps_json" in row.keys() else []
        steps = [ErasureExecutionStep.model_validate(item) for item in raw_steps if isinstance(item, dict)]
        artifact_refs = json.loads(row["artifact_refs_json"] or "[]") if "artifact_refs_json" in row.keys() else []
        return ErasureRequestRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            target_user_id=str(row["target_user_id"]),
            requested_by_user_id=str(row["requested_by_user_id"]) if row["requested_by_user_id"] else None,
            workspace_slug=str(row["workspace_slug"]) if row["workspace_slug"] else None,
            status=str(row["status"]),
            reason=str(row["reason"] or ""),
            approved_by_user_id=str(row["approved_by_user_id"]) if "approved_by_user_id" in row.keys() and row["approved_by_user_id"] else None,
            retention_decision=str(row["retention_decision"]) if "retention_decision" in row.keys() and row["retention_decision"] else None,
            legal_hold_decision=str(row["legal_hold_decision"]) if "legal_hold_decision" in row.keys() and row["legal_hold_decision"] else None,
            steps=steps,
            artifact_refs=artifact_refs if isinstance(artifact_refs, list) else [],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_data_export(self, row: sqlite3.Row) -> DataExportRecord:
        manifest_payload = json.loads(row["manifest_json"] or "{}") if "manifest_json" in row.keys() else {}
        artifact_refs = json.loads(row["artifact_refs_json"] or "[]") if "artifact_refs_json" in row.keys() else []
        manifest = None
        if isinstance(manifest_payload, dict) and manifest_payload:
            manifest = EvidenceManifest.model_validate(manifest_payload)
        return DataExportRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            workspace_slug=str(row["workspace_slug"]) if row["workspace_slug"] else None,
            target_user_id=str(row["target_user_id"]) if row["target_user_id"] else None,
            requested_by_user_id=str(row["requested_by_user_id"]) if row["requested_by_user_id"] else None,
            status=str(row["status"]),
            artifact_path=str(row["artifact_path"]) if row["artifact_path"] else None,
            approved_by_user_id=str(row["approved_by_user_id"]) if "approved_by_user_id" in row.keys() and row["approved_by_user_id"] else None,
            manifest=manifest,
            artifact_refs=artifact_refs if isinstance(artifact_refs, list) else [],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def set_profile_pin(self, slug: str, pin: str | None) -> None:
        row = self._profile_row(slug)
        if not row:
            raise RuntimeError("Unknown profile.")
        digest = None
        salt = None
        if pin:
            digest, salt = _hash_pin(pin)
            master_key = self._profile_master_key(slug)
            if not master_key:
                if row["wrapped_profile_key"]:
                    raise RuntimeError("Unlock the profile before changing its PIN.")
                master_key = self._generate_profile_master_key()
            self._wrap_profile_master_key(slug, master_key, pin, derivation_version="v2")
            self._reencrypt_profile_secrets(slug, to_scheme="profile", master_key=master_key)
        elif row["wrapped_profile_key"]:
            master_key = self._profile_master_key(slug)
            if not master_key:
                raise RuntimeError("Unlock the profile before clearing its PIN.")
            self._reencrypt_profile_secrets(slug, to_scheme="platform", master_key=master_key)
            now = datetime.now(timezone.utc).isoformat()
            self.connection.execute(
                """
                UPDATE profiles
                SET wrapped_profile_key = NULL, profile_key_salt = NULL, profile_key_derivation_version = 'v1', updated_at = ?
                WHERE slug = ?
                """,
                (now, slug),
            )
            self._profile_master_cache.pop(slug, None)
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "UPDATE profiles SET pin_hash = ?, pin_salt = ?, updated_at = ? WHERE slug = ?",
            (digest, salt, now, slug),
        )
        self.connection.commit()
        self.record_audit("security", "pin_updated", "success", "Updated profile PIN.", profile_slug=slug)

    def unlock_profile(self, slug: str, pin: str | None = None) -> ProfileSession:
        row = self.connection.execute(
            "SELECT slug, pin_hash, pin_salt, locked, wrapped_profile_key FROM profiles WHERE slug = ?",
            (slug,),
        ).fetchone()
        if not row:
            self.record_audit("security", "unlock_attempt", "failure", "Unknown profile unlock attempt.", profile_slug=slug)
            return ProfileSession(profile_slug=slug, unlocked=False, locked_reason="Unknown profile.")
        if row["pin_hash"]:
            if not _verify_pin(pin or "", row["pin_hash"], row["pin_salt"]):
                self.record_audit("security", "unlock_attempt", "failure", "Invalid PIN.", profile_slug=slug)
                return ProfileSession(profile_slug=slug, unlocked=False, locked_reason="Invalid PIN.")
            # Re-hash with current iteration count if needed (migration)
            current_digest, _ = _hash_pin(pin or "", bytes.fromhex(row["pin_salt"]))
            if current_digest != row["pin_hash"]:
                new_hash, new_salt = _hash_pin(pin or "")
                self.connection.execute(
                    "UPDATE profiles SET pin_hash = ?, pin_salt = ?, updated_at = ? WHERE slug = ?",
                    (new_hash, new_salt, datetime.now(timezone.utc).isoformat(), slug),
                )
                self.connection.commit()
            if row["wrapped_profile_key"] and not self._unwrap_profile_master_key(slug, pin or ""):
                self.record_audit("security", "unlock_attempt", "failure", "Profile key unwrap failed.", profile_slug=slug)
                return ProfileSession(profile_slug=slug, unlocked=False, locked_reason="Profile key unwrap failed.")
        unlocked_at = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "UPDATE profiles SET locked = 0, last_unlocked_at = ?, updated_at = ? WHERE slug = ?",
            (unlocked_at, unlocked_at, slug),
        )
        self.connection.commit()
        self.warm_profile_keys(slug)
        self.record_audit("security", "unlock_attempt", "success", "Profile unlocked.", profile_slug=slug)
        return ProfileSession(profile_slug=slug, unlocked=True, last_unlocked_at=datetime.fromisoformat(unlocked_at))

    def lock_profile(self, slug: str, reason: str = "Profile locked.") -> ProfileSession:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "UPDATE profiles SET locked = 1, updated_at = ? WHERE slug = ?",
            (now, slug),
        )
        self.connection.commit()
        self.clear_profile_keys(slug)
        self.record_audit("security", "lock_profile", "info", reason, profile_slug=slug)
        return ProfileSession(profile_slug=slug, unlocked=False, locked_reason=reason)

    def warm_profile_keys(self, slug: str, *, allow_locked: bool = False) -> None:
        row = self.connection.execute(
            "SELECT db_key_ref, artifact_key_ref FROM profiles WHERE slug = ?",
            (slug,),
        ).fetchone()
        if not row:
            return
        for key_ref in (row["db_key_ref"], row["artifact_key_ref"]):
            if not key_ref:
                continue
            self.resolve_secret(key_ref, profile_slug=slug, allow_locked=allow_locked, audit=False)

    def clear_profile_keys(self, slug: str) -> None:
        kept = [(k, v) for k, v in self._secret_cache.items() if k[0] != slug]
        self._secret_cache.clear()
        for k, v in kept:
            self._secret_cache[k] = v
        self._profile_master_cache.pop(slug, None)

    def is_profile_locked(self, slug: str) -> bool:
        row = self.connection.execute("SELECT locked FROM profiles WHERE slug = ?", (slug,)).fetchone()
        return bool(row["locked"]) if row else True

    def record_audit(
        self,
        category: str,
        action: str,
        status: str,
        message: str,
        profile_slug: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        if not self.audit_enabled:
            return
        created_at = datetime.now(timezone.utc).isoformat()
        details_json = json.dumps(details or {}, sort_keys=True)
        previous_hash_row = self.connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_hash = previous_hash_row["event_hash"] if previous_hash_row else None
        event_hash = hashlib.sha256(
            "|".join(
                [
                    prev_hash or "",
                    created_at,
                    profile_slug or "",
                    category,
                    action,
                    status,
                    message,
                    details_json,
                ]
            ).encode("utf-8")
        ).hexdigest()
        self.connection.execute(
            """
            INSERT INTO audit_events (created_at, profile_slug, category, action, status, message, details_json, prev_hash, event_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                profile_slug,
                category,
                action,
                status,
                message,
                details_json,
                prev_hash,
                event_hash,
            ),
        )
        self.connection.commit()

    def list_audit_events(
        self,
        profile_slug: str | None = None,
        limit: int = 10,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        offset: int = 0,
    ) -> list[AuditEvent]:
        conditions: list[str] = []
        params: list[object] = []

        if profile_slug:
            conditions.append("(profile_slug = ? OR profile_slug IS NULL)")
            params.append(profile_slug)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= ?")
            params.append(date_to)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        rows = self.connection.execute(
            f"""
            SELECT created_at, profile_slug, category, action, status, message, details_json, prev_hash, event_hash
            FROM audit_events
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [
            AuditEvent(
                created_at=datetime.fromisoformat(row["created_at"]),
                profile_slug=row["profile_slug"],
                category=row["category"],
                action=row["action"],
                status=row["status"],
                message=row["message"],
                details=json.loads(row["details_json"]),
                prev_hash=row["prev_hash"],
                event_hash=row["event_hash"],
            )
            for row in rows
        ]

    def export_audit_trail(self, profile_slug: str | None = None) -> str:
        events = self.list_audit_events(profile_slug=profile_slug, limit=10_000)
        chain_ok, chain_reason = self.verify_audit_chain()
        export = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "profile_slug": profile_slug,
            "chain_valid": chain_ok,
            "chain_reason": chain_reason,
            "event_count": len(events),
            "retention_anchors": self.list_audit_retention_anchors(profile_slug=profile_slug, limit=25),
            "events": [e.model_dump(mode="json") for e in events],
        }
        return json.dumps(export, indent=2, default=str)

    def verify_audit_chain(self) -> tuple[bool, str | None]:
        rows = self.connection.execute(
            "SELECT created_at, profile_slug, category, action, status, message, details_json, prev_hash, event_hash FROM audit_events ORDER BY id ASC"
        ).fetchall()
        prev_hash = None
        for row in rows:
            expected = hashlib.sha256(
                "|".join(
                    [
                        prev_hash or "",
                        row["created_at"],
                        row["profile_slug"] or "",
                        row["category"],
                        row["action"],
                        row["status"],
                        row["message"],
                        row["details_json"],
                    ]
                ).encode("utf-8")
            ).hexdigest()
            if row["prev_hash"] != prev_hash or row["event_hash"] != expected:
                return False, f"Audit chain broken at {row['category']}:{row['action']}."
            prev_hash = row["event_hash"]
        return True, None

    def repair_legacy_audit_chain(self) -> bool:
        first_id_row = self.connection.execute("SELECT MIN(id) AS first_id FROM audit_events").fetchone()
        first_id = first_id_row["first_id"] if first_id_row else None
        if first_id is None:
            return False
        needs_repair = self.connection.execute(
            """
            SELECT 1
            FROM audit_events
            WHERE COALESCE(event_hash, '') = ''
               OR (id = ? AND COALESCE(prev_hash, '') <> '')
               OR (id <> ? AND COALESCE(prev_hash, '') = '')
            LIMIT 1
            """,
            (first_id, first_id),
        ).fetchone()
        if not needs_repair:
            return False
        self._rebuild_audit_chain()
        self.connection.commit()
        return True

    def prune_audit_events(self, profile_slug: str, before_iso: str) -> int:
        rows = self.connection.execute(
            """
            SELECT id, created_at, event_hash
            FROM audit_events
            WHERE (profile_slug = ? OR profile_slug IS NULL) AND created_at < ?
            ORDER BY id ASC
            """,
            (profile_slug, before_iso),
        ).fetchall()
        if not rows:
            return 0
        retained_row = self.connection.execute(
            """
            SELECT created_at, event_hash
            FROM audit_events
            WHERE (profile_slug = ? OR profile_slug IS NULL) AND created_at >= ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (profile_slug, before_iso),
        ).fetchone()
        last_removed = rows[-1]
        self.connection.execute(
            """
            INSERT INTO audit_retention_anchors (
                profile_slug, created_at, before_iso, retained_from, dropped_count, last_removed_hash, last_removed_created_at, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_slug,
                datetime.now(timezone.utc).isoformat(),
                before_iso,
                retained_row["created_at"] if retained_row else None,
                len(rows),
                last_removed["event_hash"],
                last_removed["created_at"],
                json.dumps(
                    {
                        "retained_from_hash": retained_row["event_hash"] if retained_row else None,
                        "verification_scope": "retained_window",
                    },
                    sort_keys=True,
                ),
            ),
        )
        self.connection.executemany(
            "DELETE FROM audit_events WHERE id = ?",
            [(row["id"],) for row in rows],
        )
        self._rebuild_audit_chain()
        self.connection.commit()
        return len(rows)

    def _rebuild_audit_chain(self) -> None:
        rows = self.connection.execute(
            "SELECT id, created_at, profile_slug, category, action, status, message, details_json FROM audit_events ORDER BY id ASC"
        ).fetchall()
        prev_hash = None
        for row in rows:
            event_hash = hashlib.sha256(
                "|".join(
                    [
                        prev_hash or "",
                        row["created_at"],
                        row["profile_slug"] or "",
                        row["category"],
                        row["action"],
                        row["status"],
                        row["message"],
                        row["details_json"],
                    ]
                ).encode("utf-8")
            ).hexdigest()
            self.connection.execute(
                "UPDATE audit_events SET prev_hash = ?, event_hash = ? WHERE id = ?",
                (prev_hash, event_hash, row["id"]),
            )
            prev_hash = event_hash

    def list_audit_retention_anchors(self, profile_slug: str | None = None, limit: int = 20) -> list[dict[str, object]]:
        if profile_slug:
            rows = self.connection.execute(
                """
                SELECT created_at, before_iso, retained_from, dropped_count, last_removed_hash, last_removed_created_at, details_json
                FROM audit_retention_anchors
                WHERE profile_slug = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (profile_slug, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT created_at, before_iso, retained_from, dropped_count, last_removed_hash, last_removed_created_at, details_json
                FROM audit_retention_anchors
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        anchors: list[dict[str, object]] = []
        for row in rows:
            try:
                details = json.loads(row["details_json"] or "{}")
            except json.JSONDecodeError:
                details = {}
            anchors.append(
                {
                    "created_at": row["created_at"],
                    "before_iso": row["before_iso"],
                    "retained_from": row["retained_from"],
                    "dropped_count": int(row["dropped_count"] or 0),
                    "last_removed_hash": row["last_removed_hash"],
                    "last_removed_created_at": row["last_removed_created_at"],
                    "details": details,
                }
            )
        return anchors

    def create_job(
        self,
        job_type: str,
        title: str,
        profile_slug: str | None = None,
        detail: str = "",
        payload: dict[str, object] | None = None,
    ) -> BackgroundJob:
        job_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            INSERT INTO background_jobs (
                id, profile_slug, job_type, status, title, detail, progress, created_at, updated_at,
                payload_json, result_json, checkpoint_stage, recoverable, error_code, error_message
            )
            VALUES (?, ?, ?, 'queued', ?, ?, 0.0, ?, ?, ?, '{}', NULL, 0, NULL, NULL)
            """,
            (job_id, profile_slug, job_type, title, detail, now, now, json.dumps(payload or {})),
        )
        self.connection.commit()
        return self.get_job(job_id)

    def recover_stale_jobs(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            UPDATE background_jobs
            SET status = 'recoverable',
                detail = CASE
                    WHEN detail = '' THEN 'Recovered after interrupted runtime.'
                    ELSE detail || ' Recovered after interrupted runtime.'
                END,
                recoverable = 1,
                error_code = COALESCE(error_code, 'interrupted'),
                error_message = COALESCE(error_message, 'Interrupted during previous runtime.'),
                updated_at = ?
            WHERE status IN ('running', 'waiting_for_commit')
            """,
            (now,),
        )
        self.connection.commit()

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        detail: str | None = None,
        progress: float | None = None,
        result: dict[str, object] | None = None,
        checkpoint_stage: str | None = None,
        recoverable: bool | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> BackgroundJob | None:
        row = self.connection.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        next_status = status or row["status"]
        next_error_code = error_code if error_code is not None else row["error_code"]
        next_error_message = error_message if error_message is not None else row["error_message"]
        if next_status == "completed" and error_code is None and error_message is None:
            next_error_code = None
            next_error_message = None
        self.connection.execute(
            """
            UPDATE background_jobs
            SET status = ?, detail = ?, progress = ?, result_json = ?, checkpoint_stage = ?, recoverable = ?, error_code = ?, error_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                detail if detail is not None else row["detail"],
                progress if progress is not None else row["progress"],
                json.dumps(result if result is not None else json.loads(row["result_json"])),
                checkpoint_stage if checkpoint_stage is not None else row["checkpoint_stage"],
                int(recoverable if recoverable is not None else row["recoverable"]),
                next_error_code,
                next_error_message,
                datetime.now(timezone.utc).isoformat(),
                job_id,
            ),
        )
        self.connection.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> BackgroundJob | None:
        row = self.connection.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return BackgroundJob(
            id=row["id"],
            profile_slug=row["profile_slug"],
            job_type=row["job_type"],
            status=row["status"],
            title=row["title"],
            detail=row["detail"],
            progress=row["progress"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            payload=json.loads(row["payload_json"]),
            result=json.loads(row["result_json"]),
            checkpoint_stage=row["checkpoint_stage"],
            recoverable=bool(row["recoverable"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    def list_jobs(self, profile_slug: str | None = None, limit: int = 8) -> list[BackgroundJob]:
        if profile_slug:
            rows = self.connection.execute(
                "SELECT id FROM background_jobs WHERE profile_slug = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
                (profile_slug, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT id FROM background_jobs ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
        ).fetchall()
        return [self.get_job(row["id"]) for row in rows if self.get_job(row["id"]) is not None]

    def count_jobs(self, profile_slug: str | None = None) -> dict[str, int]:
        counts = {status: 0 for status in ("queued", "running", "waiting_for_commit", "completed", "failed", "recoverable", "rolled_back", "cancelled")}
        if profile_slug:
            rows = self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM background_jobs WHERE profile_slug = ? GROUP BY status",
                (profile_slug,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM background_jobs GROUP BY status",
            ).fetchall()
        for row in rows:
            counts[row["status"]] = int(row["count"])
        return counts

    def update_checkpoint(self, job_id: str, stage: str, payload: dict[str, object] | None = None) -> RecoveryCheckpoint:
        updated_at = datetime.now(timezone.utc).isoformat()
        job = self.get_job(job_id)
        if job and job.profile_slug is not None:
            existing = self.connection.execute(
                """
                SELECT id
                FROM recovery_checkpoints
                WHERE job_id = ? AND stage = ? AND profile_slug = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (job_id, stage, job.profile_slug),
            ).fetchone()
        else:
            existing = self.connection.execute(
                """
                SELECT id
                FROM recovery_checkpoints
                WHERE job_id = ? AND stage = ? AND profile_slug IS NULL
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (job_id, stage),
            ).fetchone()
        if existing:
            self.connection.execute(
                """
                UPDATE recovery_checkpoints
                SET payload_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(payload or {}), updated_at, existing["id"]),
            )
        else:
            self.connection.execute(
                """
                INSERT INTO recovery_checkpoints (job_id, profile_slug, stage, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, job.profile_slug if job else None, stage, json.dumps(payload or {}), updated_at),
            )
        self.connection.commit()
        self.update_job(job_id, checkpoint_stage=stage)
        return RecoveryCheckpoint(job_id=job_id, stage=stage, updated_at=datetime.fromisoformat(updated_at))

    def list_checkpoints(self, job_id: str) -> list[RecoveryCheckpoint]:
        rows = self.connection.execute(
            """
            SELECT job_id, stage, updated_at
            FROM recovery_checkpoints
            WHERE job_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (job_id,),
        ).fetchall()
        return [
            RecoveryCheckpoint(
                job_id=row["job_id"],
                stage=row["stage"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def upsert_backup_target(
        self,
        profile_slug: str,
        kind: str,
        path: str,
        label: str,
        writable: bool,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        row = self.connection.execute(
            "SELECT id FROM backup_targets WHERE profile_slug = ? AND kind = ? AND path = ?",
            (profile_slug, kind, path),
        ).fetchone()
        if row:
            self.connection.execute(
                "UPDATE backup_targets SET label = ?, writable = ?, updated_at = ? WHERE id = ?",
                (label, 1 if writable else 0, now, row["id"]),
            )
        else:
            self.connection.execute(
                """
                INSERT INTO backup_targets (profile_slug, kind, path, label, writable, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (profile_slug, kind, path, label, 1 if writable else 0, now, now),
            )
        self.connection.commit()

    def list_backup_targets(self, profile_slug: str | None = None) -> list[BackupTarget]:
        if profile_slug:
            rows = self.connection.execute(
                "SELECT kind, path, label, writable FROM backup_targets WHERE profile_slug = ? ORDER BY id ASC",
                (profile_slug,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT kind, path, label, writable FROM backup_targets ORDER BY id ASC",
            ).fetchall()
        return [
            BackupTarget(
                kind=row["kind"],
                path=row["path"],
                label=row["label"],
                writable=bool(row["writable"]),
            )
            for row in rows
        ]

    def export_profile_backup_state(self, slug: str) -> dict[str, object]:
        row = self._profile_row(slug)
        if not row:
            raise RuntimeError("Unknown profile.")
        master_key = self._profile_master_key(slug)
        if row["wrapped_profile_key"] and not master_key:
            raise RuntimeError("Unlock the profile before creating a portable encrypted backup.")
        cipher = self._load_secret_cipher()
        secrets: list[dict[str, object]] = []
        secret_rows = self.connection.execute(
            """
            SELECT id, name, scheme, encrypted_value, created_at, updated_at
            FROM profile_secrets
            WHERE profile_slug = ?
            ORDER BY created_at ASC, id ASC
            """,
            (slug,),
        ).fetchall()
        for secret_row in secret_rows:
            scheme = str(secret_row["scheme"] or "platform")
            if scheme == "profile":
                value = self._decrypt_with_profile_master(slug, str(secret_row["encrypted_value"]))
                if value is None:
                    raise RuntimeError("Profile secret material is unavailable until the profile is unlocked.")
            else:
                value = cipher.decrypt(str(secret_row["encrypted_value"]).encode("ascii")).decode("utf-8")
            secrets.append(
                {
                    "id": str(secret_row["id"]),
                    "name": str(secret_row["name"]),
                    "scheme": scheme,
                    "value": value,
                    "created_at": secret_row["created_at"],
                    "updated_at": secret_row["updated_at"],
                }
            )
        profile_payload = {
            "slug": row["slug"],
            "title": row["title"],
            "pin_hash": row["pin_hash"],
            "pin_salt": row["pin_salt"],
            "wrapped_profile_key": row["wrapped_profile_key"],
            "profile_key_salt": row["profile_key_salt"],
            "profile_key_derivation_version": row["profile_key_derivation_version"] or "v1",
            "db_encryption_mode": row["db_encryption_mode"] or "off",
            "db_key_ref": row["db_key_ref"],
            "db_key_version": int(row["db_key_version"] or 0),
            "artifact_key_ref": row["artifact_key_ref"],
            "artifact_key_version": int(row["artifact_key_version"] or 0),
            "artifact_migration_state": row["artifact_migration_state"] or "not enabled",
            "last_key_rotation": row["last_key_rotation"],
            "locked": bool(row["locked"]),
            "last_unlocked_at": row["last_unlocked_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "profile": profile_payload,
            "profile_master_key": master_key,
            "backup_targets": [target.model_dump(mode="json") for target in self.list_backup_targets(slug)],
            "secrets": secrets,
            "deletion_tombstones": self.list_deletion_tombstones(
                profile_payload.get("organization_id") or self.ensure_default_organization().id,
                workspace_slug=slug,
            ),
        }

    def import_secret(
        self,
        *,
        secret_id: str,
        profile_slug: str,
        name: str,
        value: str,
        scheme: str = "platform",
        created_at: str | None = None,
        updated_at: str | None = None,
        master_key: str | None = None,
    ) -> SecretRef:
        now = datetime.now(timezone.utc).isoformat()
        secret_scheme = "profile" if scheme == "profile" else "platform"
        if secret_scheme == "profile":
            if not master_key:
                raise RuntimeError("Profile master key is required to import profile-scoped secrets.")
            from cryptography.fernet import Fernet

            encrypted = Fernet(master_key.encode("ascii")).encrypt(value.encode("utf-8")).decode("ascii")
        else:
            cipher = self._load_secret_cipher()
            encrypted = cipher.encrypt(value.encode("utf-8")).decode("ascii")
        self.connection.execute(
            """
            INSERT INTO profile_secrets (id, profile_slug, name, scheme, encrypted_value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                profile_slug = excluded.profile_slug,
                name = excluded.name,
                scheme = excluded.scheme,
                encrypted_value = excluded.encrypted_value,
                updated_at = excluded.updated_at
            """,
            (
                secret_id,
                profile_slug,
                name,
                secret_scheme,
                encrypted,
                created_at or now,
                updated_at or now,
            ),
        )
        self.connection.commit()
        return SecretRef(id=f"secret:{secret_id}", profile_slug=profile_slug, name=name)

    def import_profile_backup_state(
        self,
        payload: dict[str, object],
        *,
        restored_root: Path,
        backup_root: Path | None = None,
    ) -> ProfileSummary:
        profile_payload = dict(payload.get("profile") or {})
        slug = str(profile_payload.get("slug", "") or "").strip()
        if not slug:
            raise RuntimeError("Backup metadata is missing the profile slug.")
        title = str(profile_payload.get("title", "") or "Restored profile")
        profile_root = restored_root.resolve()
        documents_root = profile_root / "documents"
        attachments_root = profile_root / "attachments"
        archives_root = profile_root / "archives"
        meetings_root = profile_root / "meetings"
        if backup_root is not None:
            backups_root = Path(backup_root).resolve()
        else:
            backup_base = profile_root.parent
            if len(profile_root.parents) >= 2:
                backup_base = profile_root.parents[1]
            backups_root = (backup_base / "backups" / slug).resolve()
        db_path = profile_root / "kern.db"
        for path in (profile_root, documents_root, attachments_root, archives_root, meetings_root, backups_root):
            path.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        locked = bool(profile_payload.get("pin_hash") or profile_payload.get("wrapped_profile_key") or profile_payload.get("locked"))
        self.connection.execute(
            """
            INSERT INTO profiles (
                slug, title, profile_root, db_path, documents_root, attachments_root, archives_root, meetings_root,
                backups_root, pin_hash, pin_salt, wrapped_profile_key, profile_key_salt, profile_key_derivation_version,
                db_encryption_mode, db_key_ref, db_key_version, artifact_key_ref, artifact_key_version,
                artifact_migration_state, last_key_rotation, locked, last_unlocked_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                title = excluded.title,
                profile_root = excluded.profile_root,
                db_path = excluded.db_path,
                documents_root = excluded.documents_root,
                attachments_root = excluded.attachments_root,
                archives_root = excluded.archives_root,
                meetings_root = excluded.meetings_root,
                backups_root = excluded.backups_root,
                pin_hash = excluded.pin_hash,
                pin_salt = excluded.pin_salt,
                wrapped_profile_key = excluded.wrapped_profile_key,
                profile_key_salt = excluded.profile_key_salt,
                profile_key_derivation_version = excluded.profile_key_derivation_version,
                db_encryption_mode = excluded.db_encryption_mode,
                db_key_ref = excluded.db_key_ref,
                db_key_version = excluded.db_key_version,
                artifact_key_ref = excluded.artifact_key_ref,
                artifact_key_version = excluded.artifact_key_version,
                artifact_migration_state = excluded.artifact_migration_state,
                last_key_rotation = excluded.last_key_rotation,
                locked = excluded.locked,
                last_unlocked_at = excluded.last_unlocked_at,
                updated_at = excluded.updated_at
            """,
            (
                slug,
                title,
                str(profile_root),
                str(db_path),
                str(documents_root),
                str(attachments_root),
                str(archives_root),
                str(meetings_root),
                str(backups_root),
                profile_payload.get("pin_hash"),
                profile_payload.get("pin_salt"),
                profile_payload.get("wrapped_profile_key"),
                profile_payload.get("profile_key_salt"),
                profile_payload.get("profile_key_derivation_version") or "v1",
                profile_payload.get("db_encryption_mode") or "off",
                profile_payload.get("db_key_ref"),
                int(profile_payload.get("db_key_version") or 0),
                profile_payload.get("artifact_key_ref"),
                int(profile_payload.get("artifact_key_version") or 0),
                profile_payload.get("artifact_migration_state") or "not enabled",
                profile_payload.get("last_key_rotation"),
                1 if locked else 0,
                None,
                profile_payload.get("created_at") or now,
                now,
            ),
        )
        master_key = str(payload.get("profile_master_key") or "").strip() or None
        if master_key:
            self._profile_master_cache[slug] = master_key
        for secret in list(payload.get("secrets") or []):
            secret_data = dict(secret)
            self.import_secret(
                secret_id=str(secret_data.get("id", "") or ""),
                profile_slug=slug,
                name=str(secret_data.get("name", "") or ""),
                value=str(secret_data.get("value", "") or ""),
                scheme=str(secret_data.get("scheme", "platform") or "platform"),
                created_at=str(secret_data.get("created_at", "") or "") or None,
                updated_at=str(secret_data.get("updated_at", "") or "") or None,
                master_key=master_key,
            )
        for tombstone in list(payload.get("deletion_tombstones") or []):
            if not isinstance(tombstone, dict):
                continue
            self.connection.execute(
                """
                INSERT OR IGNORE INTO deletion_tombstones (
                    id, organization_id, workspace_slug, target_user_id, artifact_class, reference_id, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(tombstone.get("id") or uuid4()),
                    str(tombstone.get("organization_id") or self.ensure_default_organization().id),
                    str(tombstone.get("workspace_slug") or slug),
                    str(tombstone.get("target_user_id")) if tombstone.get("target_user_id") else None,
                    str(tombstone.get("artifact_class") or "unknown"),
                    str(tombstone.get("reference_id")) if tombstone.get("reference_id") else None,
                    json.dumps(tombstone.get("metadata") or {}),
                    str(tombstone.get("created_at") or now),
                ),
            )
        self.upsert_backup_target(slug, "local_folder", str(backups_root), "Profile backup folder", True)
        self.connection.commit()
        self.clear_profile_keys(slug)
        self.record_audit("backup", "import_profile_backup_state", "success", "Imported profile backup state.", profile_slug=slug)
        profile = self.get_profile(slug)
        if profile is None:
            raise RuntimeError("Failed to import restored profile metadata.")
        return profile

    def assert_profile_unlocked(self, slug: str, category: str, action: str) -> None:
        if not self.is_profile_locked(slug):
            return
        self.record_audit(category, action, "failure", "Profile is locked.", profile_slug=slug)
        raise PermissionError("Active profile is locked.")

    def _load_secret_cipher(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - depends on local optional package
            raise RuntimeError("cryptography is required for encrypted secret storage.") from exc
        self.secret_key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.secret_key_path.exists():
            key = self.secret_key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            self.secret_key_path.write_bytes(key)
            with contextlib.suppress(Exception):  # cleanup â€” best-effort
                os.chmod(self.secret_key_path, 0o600)
        return Fernet(key)

    def store_secret(self, profile_slug: str, name: str, value: str) -> SecretRef:
        secret_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        scheme = "profile" if self._uses_profile_secret_scheme(profile_slug, name) else "platform"
        if scheme == "profile":
            encrypted = self._encrypt_with_profile_master(profile_slug, value)
        else:
            cipher = self._load_secret_cipher()
            encrypted = cipher.encrypt(value.encode("utf-8")).decode("ascii")
        self.connection.execute(
            """
            INSERT INTO profile_secrets (id, profile_slug, name, scheme, encrypted_value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (secret_id, profile_slug, name, scheme, encrypted, now, now),
        )
        self.connection.commit()
        self.record_audit("security", "secret_stored", "success", f"Stored secret reference {name}.", profile_slug=profile_slug)
        return SecretRef(id=f"secret:{secret_id}", profile_slug=profile_slug, name=name)

    def resolve_secret(
        self,
        secret_ref: str | None,
        profile_slug: str | None = None,
        *,
        allow_locked: bool = False,
        audit: bool = True,
    ) -> str | None:
        if not secret_ref:
            return None
        if secret_ref.startswith("env:"):
            return os.getenv(secret_ref.split(":", 1)[1])
        if not secret_ref.startswith("secret:"):
            return None
        if not profile_slug:
            return None
        if not allow_locked and self.is_profile_locked(profile_slug):
            if audit:
                self.record_audit(
                    "security",
                    "secret_resolve",
                    "failure",
                    "Secret resolution denied while profile is locked.",
                    profile_slug=profile_slug,
                    details={"secret_ref": secret_ref},
                )
            return None
        cache_key = (profile_slug, secret_ref)
        if cache_key in self._secret_cache:
            return self._secret_cache[cache_key]
        secret_id = secret_ref.split(":", 1)[1]
        row = self.connection.execute(
            "SELECT encrypted_value, scheme FROM profile_secrets WHERE id = ? AND profile_slug = ?",
            (secret_id, profile_slug),
        ).fetchone()
        if not row:
            if audit:
                self.record_audit(
                    "security",
                    "secret_resolve",
                    "failure",
                    "Secret reference not found.",
                    profile_slug=profile_slug,
                    details={"secret_ref": secret_ref},
                )
            return None
        scheme = str(row["scheme"] or "platform")
        if scheme == "profile":
            value = self._decrypt_with_profile_master(profile_slug, str(row["encrypted_value"]))
            if value is None:
                if audit:
                    self.record_audit(
                        "security",
                        "secret_resolve",
                        "failure",
                        "Profile secret is unavailable until the profile is unlocked.",
                        profile_slug=profile_slug,
                        details={"secret_ref": secret_ref},
                    )
                return None
        else:
            cipher = self._load_secret_cipher()
            value = cipher.decrypt(row["encrypted_value"].encode("ascii")).decode("utf-8")
        self._secret_cache[cache_key] = value
        if audit:
            self.record_audit(
                "security",
                "secret_resolve",
                "success",
                "Resolved secret reference.",
                profile_slug=profile_slug,
                details={"secret_ref": secret_ref},
            )
        return value
