from __future__ import annotations

POSTGRES_SERVER_SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    email text NOT NULL,
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    oidc_subject text,
    auth_source text NOT NULL DEFAULT 'oidc',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    UNIQUE (organization_id, email)
);

CREATE TABLE IF NOT EXISTS workspaces (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    slug text NOT NULL,
    title text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, slug)
);

CREATE TABLE IF NOT EXISTS workspace_memberships (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    user_id uuid NOT NULL REFERENCES users(id),
    role text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, user_id, role)
);
CREATE INDEX IF NOT EXISTS idx_workspace_memberships_user ON workspace_memberships(user_id, workspace_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS user_sessions (
    id uuid PRIMARY KEY,
    user_id uuid REFERENCES users(id),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    workspace_id uuid REFERENCES workspaces(id),
    auth_method text NOT NULL,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    last_activity_at timestamptz NOT NULL,
    revoked_at timestamptz,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id, expires_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_user_sessions_org ON user_sessions(organization_id, expires_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS break_glass_admins (
    id uuid PRIMARY KEY,
    username text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    password_salt text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

CREATE TABLE IF NOT EXISTS threads (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    owner_user_id uuid NOT NULL REFERENCES users(id),
    title text NOT NULL,
    visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'shared', 'system_audit')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_threads_owner ON threads(organization_id, workspace_id, owner_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_workspace_visibility ON threads(organization_id, workspace_id, visibility, updated_at DESC);

CREATE TABLE IF NOT EXISTS thread_participants (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    user_id uuid NOT NULL REFERENCES users(id),
    role text NOT NULL DEFAULT 'owner',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (thread_id, user_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    actor_user_id uuid REFERENCES users(id),
    role text NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content text NOT NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_thread_created ON messages(thread_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_actor ON messages(organization_id, workspace_id, actor_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS workspace_memory_items (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    source_thread_id uuid REFERENCES threads(id) ON DELETE SET NULL,
    promoted_by_user_id uuid REFERENCES users(id),
    key text NOT NULL,
    value text NOT NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_workspace_memory_scope ON workspace_memory_items(organization_id, workspace_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS private_memory_items (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    user_id uuid NOT NULL REFERENCES users(id),
    source_thread_id uuid REFERENCES threads(id) ON DELETE SET NULL,
    key text NOT NULL,
    value text NOT NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_private_memory_scope ON private_memory_items(organization_id, workspace_id, user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    profile_slug text,
    organization_id uuid,
    workspace_id uuid,
    actor_user_id uuid,
    category text NOT NULL,
    action text NOT NULL,
    status text NOT NULL,
    message text NOT NULL,
    details_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    prev_hash text,
    event_hash text
);
CREATE INDEX IF NOT EXISTS idx_audit_events_scope ON audit_events(organization_id, workspace_id, actor_user_id, created_at DESC, id DESC);

-- Server-mode access control is currently enforced in PostgresPlatformStore
-- before every thread, message, and memory operation. Do not enable partial
-- PostgreSQL RLS here until connection-scoped tenant settings and tested
-- policies are added; RLS without policies is either bypassed by table owners
-- or breaks later privilege changes unpredictably.
"""
