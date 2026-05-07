from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.server_schema import POSTGRES_SERVER_SCHEMA
from app.types import (
    AuthContext,
    BreakGlassAdminRecord,
    MessageRecord,
    OrganizationRecord,
    ProfileSummary,
    ThreadRecord,
    UserRecord,
    UserSessionRecord,
    WorkspaceMembershipRecord,
)

_PBKDF2_ITERATIONS = 600_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _hash_secret(secret: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return digest.hex(), salt.hex()


def _verify_secret(secret: str, stored_hash: str, salt_hex: str) -> bool:
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), bytes.fromhex(salt_hex), _PBKDF2_ITERATIONS)
    return hmac.compare_digest(digest.hex(), stored_hash)


class ServerHealthConnection:
    def __init__(self, platform: "PostgresPlatformStore") -> None:
        self.platform = platform

    def execute(self, _query: str):
        self.platform.ping()
        return self

    def fetchone(self):
        return {"ok": 1}


class ServerConnectionFacade:
    def close(self) -> None:
        return None


class PostgresPlatformStore:
    """Server-mode platform store.

    This class intentionally mirrors the subset of PlatformStore used by auth,
    workspace, thread, and message routes. Server mode blocks legacy local
    profile subsystems until they are moved onto this store.
    """

    def __init__(self, dsn: str | None = None, *, audit_enabled: bool = True) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except Exception as exc:  # pragma: no cover - dependency/env guard
            raise RuntimeError("Server mode requires psycopg[binary].") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._jsonb = Jsonb
        self.dsn = dsn or settings.postgres_dsn
        if not self.dsn:
            raise RuntimeError("KERN_POSTGRES_DSN is required in server mode.")
        self.audit_enabled = audit_enabled
        with self._connect() as connection:
            connection.execute(POSTGRES_SERVER_SCHEMA)
        self.ensure_default_organization()
        if not self.list_profiles():
            self.ensure_default_profile(settings.profile_root, settings.backup_root, settings.db_path)

    def _connect(self):
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    @property
    def connection(self):
        return ServerConnectionFacade()

    def ping(self) -> None:
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def _profile_from_workspace(self, row) -> ProfileSummary:
        root = Path(settings.object_storage_root or settings.root_path).as_posix().rstrip("/")
        org_id = str(row["organization_id"])
        workspace_id = str(row["id"])
        slug = str(row["slug"])
        base = f"{root}/org/{org_id}/workspace/{workspace_id}"
        return ProfileSummary(
            workspace_id=workspace_id,
            organization_id=org_id,
            slug=slug,
            title=str(row["title"]),
            profile_root=base,
            db_path=f"postgres://workspace/{workspace_id}",
            documents_root=f"{base}/documents",
            attachments_root=f"{base}/attachments",
            archives_root=f"{base}/archives",
            meetings_root=f"{base}/meetings",
            backups_root=f"{base}/backups",
            created_at=_as_dt(row["created_at"]),
            updated_at=_as_dt(row["updated_at"]),
        )

    def ensure_default_organization(self) -> OrganizationRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM organizations ORDER BY created_at ASC LIMIT 1").fetchone()
            if row is None:
                now = _utcnow()
                org_id = str(uuid4())
                row = connection.execute(
                    """
                    INSERT INTO organizations (id, slug, name, created_at, updated_at)
                    VALUES (%s, 'default', 'Default organization', %s, %s)
                    RETURNING *
                    """,
                    (org_id, now, now),
                ).fetchone()
        return OrganizationRecord(
            id=str(row["id"]),
            slug=str(row["slug"]),
            name=str(row["name"]),
            created_at=_as_dt(row["created_at"]),
            updated_at=_as_dt(row["updated_at"]),
        )

    def ensure_default_profile(
        self,
        _profile_root: Path,
        _backup_root: Path,
        _legacy_db_path: Path,
        title: str = "Primary profile",
        slug: str = "default",
    ) -> ProfileSummary:
        organization = self.ensure_default_organization()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE organization_id = %s AND slug = %s",
                (organization.id, slug),
            ).fetchone()
            if row is None:
                now = _utcnow()
                row = connection.execute(
                    """
                    INSERT INTO workspaces (id, organization_id, slug, title, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (str(uuid4()), organization.id, slug, title, now, now),
                ).fetchone()
        return self._profile_from_workspace(row)

    def list_profiles(self) -> list[ProfileSummary]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM workspaces ORDER BY updated_at DESC, slug ASC").fetchall()
        return [self._profile_from_workspace(row) for row in rows]

    def get_profile(self, slug: str | None) -> ProfileSummary | None:
        if not slug:
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workspaces WHERE slug = %s LIMIT 1", (slug,)).fetchone()
        return self._profile_from_workspace(row) if row else None

    def create_user(
        self,
        *,
        email: str,
        display_name: str,
        organization_id: str | None = None,
        oidc_subject: str | None = None,
        auth_source: str = "oidc",
        status: str = "pending",
    ) -> UserRecord:
        organization_id = organization_id or self.ensure_default_organization().id
        existing = self.get_user_by_email(organization_id, email)
        if existing:
            return existing
        now = _utcnow()
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO users (id, organization_id, email, display_name, status, oidc_subject, auth_source, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (str(uuid4()), organization_id, email, display_name, status, oidc_subject, auth_source, now, now),
            ).fetchone()
        return self._row_to_user(row)

    def _row_to_user(self, row) -> UserRecord:
        return UserRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            email=str(row["email"]),
            display_name=str(row["display_name"]),
            status=str(row["status"]),
            oidc_subject=str(row["oidc_subject"]) if row.get("oidc_subject") else None,
            auth_source=str(row["auth_source"]),
            created_at=_as_dt(row["created_at"]),
            updated_at=_as_dt(row["updated_at"]),
            deleted_at=_as_dt(row["deleted_at"]) if row.get("deleted_at") else None,
        )

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_email(self, organization_id: str, email: str) -> UserRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE organization_id = %s AND email = %s",
                (organization_id, email),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self, organization_id: str | None = None) -> list[UserRecord]:
        organization_id = organization_id or self.ensure_default_organization().id
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users WHERE organization_id = %s ORDER BY updated_at DESC",
                (organization_id,),
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def bind_user_oidc_identity(self, user_id: str, *, oidc_subject: str, display_name: str) -> UserRecord:
        now = _utcnow()
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE users SET oidc_subject = %s, display_name = %s, updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (oidc_subject, display_name, now, user_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("User not found.")
        return self._row_to_user(row)

    def set_user_status(self, user_id: str, status: str) -> UserRecord:
        now = _utcnow()
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE users SET status = %s, updated_at = %s WHERE id = %s RETURNING *",
                (status, now, user_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("User not found.")
        return self._row_to_user(row)

    def upsert_workspace_membership(self, *, user_id: str, workspace_slug: str, role: str) -> WorkspaceMembershipRecord:
        profile = self.get_profile(workspace_slug)
        if profile is None or not profile.workspace_id or not profile.organization_id:
            raise RuntimeError("Unknown workspace.")
        now = _utcnow()
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO workspace_memberships (id, organization_id, workspace_id, user_id, role, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (workspace_id, user_id, role)
                DO UPDATE SET updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (str(uuid4()), profile.organization_id, profile.workspace_id, user_id, role, now, now),
            ).fetchone()
        return self._row_to_membership(row, profile.slug)

    def _row_to_membership(self, row, workspace_slug: str | None = None) -> WorkspaceMembershipRecord:
        if workspace_slug is None:
            with self._connect() as connection:
                workspace = connection.execute("SELECT slug FROM workspaces WHERE id = %s", (str(row["workspace_id"]),)).fetchone()
            workspace_slug = str(workspace["slug"]) if workspace else ""
        return WorkspaceMembershipRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            workspace_id=str(row["workspace_id"]),
            workspace_slug=workspace_slug,
            user_id=str(row["user_id"]),
            role=str(row["role"]),
            created_at=_as_dt(row["created_at"]),
            updated_at=_as_dt(row["updated_at"]),
        )

    def list_workspace_memberships(self, user_id: str) -> list[WorkspaceMembershipRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT wm.*, w.slug AS workspace_slug
                FROM workspace_memberships wm
                JOIN workspaces w ON w.id = wm.workspace_id
                WHERE wm.user_id = %s
                ORDER BY wm.updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_membership(row, str(row["workspace_slug"])) for row in rows]

    def has_workspace_access(self, user_id: str | None, workspace_slug: str, *roles: str) -> bool:
        if not user_id:
            return False
        profile = self.get_profile(workspace_slug)
        if profile is None:
            return False
        params: list[object] = [profile.workspace_id, user_id]
        role_sql = ""
        if roles:
            role_sql = " AND role = ANY(%s)"
            params.append(list(roles))
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT 1 FROM workspace_memberships WHERE workspace_id = %s AND user_id = %s{role_sql} LIMIT 1",
                tuple(params),
            ).fetchone()
        return bool(row)

    def list_workspace_users(self, workspace_slug: str) -> list[UserRecord]:
        profile = self.get_profile(workspace_slug)
        if profile is None:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT u.*
                FROM users u
                JOIN workspace_memberships wm ON wm.user_id = u.id
                WHERE wm.workspace_id = %s
                ORDER BY u.updated_at DESC
                """,
                (profile.workspace_id,),
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def create_session(
        self,
        *,
        organization_id: str,
        auth_method: str,
        user_id: str | None = None,
        workspace_slug: str | None = None,
        ttl_seconds: int = 8 * 60 * 60,
        metadata: dict[str, object] | None = None,
    ) -> UserSessionRecord:
        workspace = self.get_profile(workspace_slug) if workspace_slug else None
        now = _utcnow()
        session_id = str(uuid4())
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO user_sessions (id, user_id, organization_id, workspace_id, auth_method, issued_at, expires_at, last_activity_at, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    session_id,
                    user_id,
                    organization_id,
                    workspace.workspace_id if workspace else None,
                    auth_method,
                    now,
                    now + timedelta(seconds=ttl_seconds),
                    now,
                    self._jsonb(metadata or {}),
                ),
            ).fetchone()
        return self._row_to_session(row)

    def _row_to_session(self, row) -> UserSessionRecord:
        workspace_slug = None
        if row.get("workspace_id"):
            with self._connect() as connection:
                workspace = connection.execute("SELECT slug FROM workspaces WHERE id = %s", (str(row["workspace_id"]),)).fetchone()
            workspace_slug = str(workspace["slug"]) if workspace else None
        metadata = row.get("metadata_json") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata or "{}")
        return UserSessionRecord(
            id=str(row["id"]),
            user_id=str(row["user_id"]) if row.get("user_id") else None,
            organization_id=str(row["organization_id"]),
            workspace_id=str(row["workspace_id"]) if row.get("workspace_id") else None,
            workspace_slug=workspace_slug,
            auth_method=str(row["auth_method"]),
            issued_at=_as_dt(row["issued_at"]),
            expires_at=_as_dt(row["expires_at"]),
            last_activity_at=_as_dt(row["last_activity_at"]),
            revoked_at=_as_dt(row["revoked_at"]) if row.get("revoked_at") else None,
            metadata=metadata,
        )

    def get_session(self, session_id: str, *, touch: bool = False) -> UserSessionRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM user_sessions WHERE id = %s", (session_id,)).fetchone()
            if row is None:
                return None
            now = _utcnow()
            expires_at = _as_dt(row["expires_at"])
            last_activity_at = _as_dt(row["last_activity_at"])
            if row.get("revoked_at") or expires_at <= now:
                return None
            if settings.session_idle_minutes > 0 and last_activity_at + timedelta(minutes=settings.session_idle_minutes) <= now:
                return None
            if touch:
                row = connection.execute(
                    "UPDATE user_sessions SET last_activity_at = %s WHERE id = %s RETURNING *",
                    (now, session_id),
                ).fetchone()
        return self._row_to_session(row)

    def revoke_session(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE user_sessions SET revoked_at = %s WHERE id = %s", (_utcnow(), session_id))

    def revoke_user_sessions(self, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE user_sessions SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL", (_utcnow(), user_id))

    def create_break_glass_admin(self, username: str, password: str) -> BreakGlassAdminRecord:
        normalized = username.strip().lower()
        if not normalized or not password:
            raise RuntimeError("Username and password are required.")
        now = _utcnow()
        password_hash, password_salt = _hash_secret(password)
        admin_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO break_glass_admins (id, username, password_hash, password_salt, enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, true, %s, %s)
                ON CONFLICT (username) DO UPDATE SET
                    password_hash = EXCLUDED.password_hash,
                    password_salt = EXCLUDED.password_salt,
                    enabled = true,
                    updated_at = EXCLUDED.updated_at
                """,
                (admin_id, normalized, password_hash, password_salt, now, now),
            )
        admin = self.get_break_glass_admin(normalized)
        if admin is None:
            raise RuntimeError("Failed to store break-glass admin.")
        return admin

    def get_break_glass_admin(self, username: str) -> BreakGlassAdminRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM break_glass_admins WHERE username = %s",
                (username.strip().lower(),),
            ).fetchone()
        if not row:
            return None
        return BreakGlassAdminRecord(
            id=str(row["id"]),
            username=str(row["username"]),
            created_at=_as_dt(row["created_at"]),
            updated_at=_as_dt(row["updated_at"]),
            last_login_at=_as_dt(row["last_login_at"]) if row["last_login_at"] else None,
            enabled=bool(row["enabled"]),
        )

    def authenticate_break_glass_admin(self, username: str, password: str) -> BreakGlassAdminRecord | None:
        normalized = username.strip().lower()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM break_glass_admins WHERE username = %s AND enabled = true",
                (normalized,),
            ).fetchone()
            if not row or not _verify_secret(password, str(row["password_hash"]), str(row["password_salt"])):
                return None
            now = _utcnow()
            connection.execute(
                "UPDATE break_glass_admins SET last_login_at = %s, updated_at = %s WHERE id = %s",
                (now, now, row["id"]),
            )
        return self.get_break_glass_admin(normalized)

    def list_sessions(self, organization_id: str, user_id: str | None = None) -> list[UserSessionRecord]:
        if user_id:
            sql = "SELECT * FROM user_sessions WHERE organization_id = %s AND user_id = %s ORDER BY last_activity_at DESC"
            params = (organization_id, user_id)
        else:
            sql = "SELECT * FROM user_sessions WHERE organization_id = %s ORDER BY last_activity_at DESC"
            params = (organization_id,)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [session for row in rows if (session := self.get_session(str(row["id"]))) is not None]

    def set_session_workspace(self, session_id: str, workspace_slug: str | None) -> UserSessionRecord | None:
        workspace = self.get_profile(workspace_slug) if workspace_slug else None
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE user_sessions SET workspace_id = %s, last_activity_at = %s WHERE id = %s RETURNING *",
                (workspace.workspace_id if workspace else None, _utcnow(), session_id),
            ).fetchone()
        return self._row_to_session(row) if row else None

    def build_auth_context(self, session_id: str) -> AuthContext | None:
        session = self.get_session(session_id, touch=True)
        if session is None:
            return None
        roles: list[str] = []
        user_email = None
        is_break_glass = session.auth_method == "break_glass"
        if is_break_glass:
            roles = ["break_glass_admin"]
        elif session.user_id:
            user = self.get_user(session.user_id)
            if user is None or user.status != "active":
                return None
            user_email = user.email
            memberships = self.list_workspace_memberships(session.user_id)
            if session.workspace_slug:
                memberships = [membership for membership in memberships if membership.workspace_slug == session.workspace_slug]
            roles = [membership.role for membership in memberships]
        return AuthContext(
            authenticated=True,
            auth_method=session.auth_method,
            organization_id=session.organization_id,
            user_id=session.user_id,
            user_email=user_email,
            workspace_id=session.workspace_id,
            workspace_slug=session.workspace_slug,
            roles=roles,
            session_id=session.id,
            is_break_glass=is_break_glass,
            is_bootstrap_token=session.auth_method in {"break_glass", "admin_token"},
        )

    def _row_to_thread(self, row) -> ThreadRecord:
        workspace = self.get_profile_by_id(str(row["workspace_id"]))
        return ThreadRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            workspace_id=str(row["workspace_id"]),
            workspace_slug=workspace.slug if workspace else "",
            owner_user_id=str(row["owner_user_id"]),
            title=str(row["title"]),
            visibility=str(row["visibility"]),
            created_at=_as_dt(row["created_at"]),
            updated_at=_as_dt(row["updated_at"]),
        )

    def get_profile_by_id(self, workspace_id: str) -> ProfileSummary | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workspaces WHERE id = %s", (workspace_id,)).fetchone()
        return self._profile_from_workspace(row) if row else None

    def create_thread(self, *, organization_id: str, workspace_slug: str, owner_user_id: str, title: str = "New thread", visibility: str = "private") -> ThreadRecord:
        if visibility not in {"private", "shared", "system_audit"}:
            raise RuntimeError("Invalid thread visibility.")
        profile = self.get_profile(workspace_slug)
        if profile is None or profile.organization_id != organization_id:
            raise RuntimeError("Unknown workspace.")
        if not self.has_workspace_access(owner_user_id, workspace_slug):
            raise PermissionError("User does not have workspace access.")
        now = _utcnow()
        thread_id = str(uuid4())
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO threads (id, organization_id, workspace_id, owner_user_id, title, visibility, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (thread_id, organization_id, profile.workspace_id, owner_user_id, title, visibility, now, now),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO thread_participants (id, thread_id, organization_id, workspace_id, user_id, role, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, 'owner', %s, %s)
                """,
                (str(uuid4()), thread_id, organization_id, profile.workspace_id, owner_user_id, now, now),
            )
        return self._row_to_thread(row)

    def get_thread_for_user(self, thread_id: str, *, user_id: str, organization_id: str) -> ThreadRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM threads WHERE id = %s AND organization_id = %s", (thread_id, organization_id)).fetchone()
            if row is None or row["visibility"] == "system_audit":
                return None
            workspace = self.get_profile_by_id(str(row["workspace_id"]))
            if row["visibility"] == "shared" and workspace and self.has_workspace_access(user_id, workspace.slug):
                return self._row_to_thread(row)
            participant = connection.execute(
                "SELECT 1 FROM thread_participants WHERE thread_id = %s AND user_id = %s",
                (thread_id, user_id),
            ).fetchone()
        return self._row_to_thread(row) if participant else None

    def list_threads_for_user(self, *, organization_id: str, workspace_slug: str, user_id: str, limit: int = 50) -> list[ThreadRecord]:
        profile = self.get_profile(workspace_slug)
        if profile is None or not self.has_workspace_access(user_id, workspace_slug):
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT t.*
                FROM threads t
                LEFT JOIN thread_participants tp ON tp.thread_id = t.id AND tp.user_id = %s
                WHERE t.organization_id = %s
                  AND t.workspace_id = %s
                  AND t.visibility != 'system_audit'
                  AND (t.visibility = 'shared' OR tp.user_id IS NOT NULL)
                ORDER BY t.updated_at DESC, t.id DESC
                LIMIT %s
                """,
                (user_id, organization_id, profile.workspace_id, limit),
            ).fetchall()
        return [self._row_to_thread(row) for row in rows]

    def _row_to_message(self, row) -> MessageRecord:
        workspace = self.get_profile_by_id(str(row["workspace_id"]))
        metadata = row.get("metadata_json") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata or "{}")
        return MessageRecord(
            id=str(row["id"]),
            thread_id=str(row["thread_id"]),
            organization_id=str(row["organization_id"]),
            workspace_id=str(row["workspace_id"]),
            workspace_slug=workspace.slug if workspace else "",
            actor_user_id=str(row["actor_user_id"]) if row.get("actor_user_id") else None,
            role=str(row["role"]),
            content=str(row["content"]),
            metadata=metadata,
            created_at=_as_dt(row["created_at"]),
        )

    def append_message(self, *, thread_id: str, actor_user_id: str | None, role: str, content: str, metadata: dict[str, object] | None = None, acting_user_id: str | None = None, organization_id: str) -> MessageRecord:
        requester = acting_user_id or actor_user_id
        if not requester or self.get_thread_for_user(thread_id, user_id=requester, organization_id=organization_id) is None:
            raise PermissionError("Thread is not available to the active user.")
        thread = self.get_thread_for_user(thread_id, user_id=requester, organization_id=organization_id)
        now = _utcnow()
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO messages (id, thread_id, organization_id, workspace_id, actor_user_id, role, content, metadata_json, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (str(uuid4()), thread.id, thread.organization_id, thread.workspace_id, actor_user_id, role, content, self._jsonb(metadata or {}), now),
            ).fetchone()
            connection.execute("UPDATE threads SET updated_at = %s WHERE id = %s", (now, thread.id))
        return self._row_to_message(row)

    def list_messages_for_user(self, *, thread_id: str, user_id: str, organization_id: str, limit: int = 100) -> list[MessageRecord]:
        if self.get_thread_for_user(thread_id, user_id=user_id, organization_id=organization_id) is None:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE thread_id = %s AND organization_id = %s ORDER BY created_at ASC, id ASC LIMIT %s",
                (thread_id, organization_id, limit),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def share_thread(self, *, thread_id: str, user_id: str, organization_id: str) -> ThreadRecord:
        thread = self.get_thread_for_user(thread_id, user_id=user_id, organization_id=organization_id)
        if thread is None or thread.owner_user_id != user_id:
            raise PermissionError("Only the owner can share this thread.")
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE threads SET visibility = 'shared', updated_at = %s WHERE id = %s RETURNING *",
                (_utcnow(), thread_id),
            ).fetchone()
        return self._row_to_thread(row)

    def promote_thread_memory(self, *, thread_id: str, user_id: str, organization_id: str, key: str, value: str, metadata: dict[str, object] | None = None) -> str:
        thread = self.get_thread_for_user(thread_id, user_id=user_id, organization_id=organization_id)
        if thread is None:
            raise PermissionError("Thread is not available to the active user.")
        if thread.owner_user_id != user_id and not self.has_workspace_access(user_id, thread.workspace_slug, "org_owner", "org_admin"):
            raise PermissionError("Only the thread owner or workspace admin can promote thread memory.")
        memory_id = str(uuid4())
        now = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workspace_memory_items (id, organization_id, workspace_id, source_thread_id, promoted_by_user_id, key, value, metadata_json, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (memory_id, thread.organization_id, thread.workspace_id, thread.id, user_id, key, value, self._jsonb(metadata or {}), now, now),
            )
        return memory_id

    def record_audit(self, category: str, action: str, status: str, message: str, profile_slug: str | None = None, details: dict[str, object] | None = None) -> None:
        if not self.audit_enabled:
            return
        details = details or {}
        profile = self.get_profile(profile_slug) if profile_slug else None
        organization_id = str(details.get("organization_id") or (profile.organization_id if profile else "") or "") or None
        workspace_id = str(details.get("workspace_id") or (profile.workspace_id if profile else "") or "") or None
        actor_user_id = str(details.get("actor_user_id") or details.get("user_id") or "") or None
        payload = json.dumps(details, sort_keys=True)
        with self._connect() as connection:
            previous = connection.execute("SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = previous["event_hash"] if previous else None
            created_at = _utcnow()
            event_hash = hashlib.sha256("|".join([prev_hash or "", created_at.isoformat(), profile_slug or "", category, action, status, message, payload]).encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO audit_events (
                    created_at, profile_slug, organization_id, workspace_id, actor_user_id,
                    category, action, status, message, details_json, prev_hash, event_hash
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    created_at,
                    profile_slug,
                    organization_id,
                    workspace_id,
                    actor_user_id,
                    category,
                    action,
                    status,
                    message,
                    self._jsonb(details),
                    prev_hash,
                    event_hash,
                ),
            )

    def is_profile_locked(self, _slug: str) -> bool:
        return False

    def list_backup_targets(self, _slug: str):
        raise RuntimeError(
            "Server mode backup targets are unsupported until object-storage backup inventory is implemented."
        )

    def verify_audit_chain(self, *_args, **_kwargs):
        raise RuntimeError(
            "Server mode audit-chain verification is unsupported until durable audit storage is implemented."
        )
