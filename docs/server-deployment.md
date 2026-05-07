# KERN Server Deployment

Server mode is for a single organization with many authenticated users and shared workspaces. It is not the same release target as the local desktop app.

## Required Infrastructure

- PostgreSQL 16 or newer for durable data.
- Redis 7 or newer for rate limits and ephemeral coordination.
- OIDC/SSO provider for normal user login.
- HTTPS reverse proxy with trusted proxy headers.
- Durable object storage or mounted server storage for documents and artifacts.
- A secrets backend for session, encryption, OIDC, and break-glass secrets.

Redis must not be used as the source of truth for messages, permissions, documents, or audit logs.

## Compose Profiles

Local container smoke:

```powershell
docker compose --profile local up --build
```

Server-mode dependency smoke:

```powershell
$env:KERN_POSTGRES_PASSWORD = "<strong password>"
$env:KERN_POSTGRES_DSN = "postgresql://kern:<strong password>@postgres:5432/kern"
$env:KERN_NETWORK_ALLOWED_HOSTS = "kern.example.com"
$env:KERN_PUBLIC_BASE_URL = "https://kern.example.com"
$env:KERN_SESSION_SECRET = "<secret from vault>"
$env:KERN_ENCRYPTION_KEY_PROVIDER = "vault"
$env:KERN_OIDC_ENABLED = "true"
$env:KERN_OIDC_ISSUER_URL = "https://idp.example.com"
$env:KERN_OIDC_CLIENT_ID = "kern"
$env:KERN_OIDC_CLIENT_SECRET = "<secret from vault>"
$env:KERN_OIDC_REDIRECT_URI = "https://kern.example.com/auth/oidc/callback"
docker compose --profile server up --build
```

Do not mount a local `.env` file into the server container. Compose may read `.env` for variable substitution, but production secrets should come from the deployment platform or a secrets manager.

## Release Gate

Run this only against a real server-mode environment:

```powershell
$env:KERN_SERVER_MODE = "true"
$env:KERN_POSTGRES_DSN = "postgresql://..."
$env:KERN_REDIS_URL = "redis://..."
.\scripts\run-kern-server-release-gate.ps1
```

The gate must pass configuration validation, PostgreSQL connectivity, Redis connectivity, and the server authorization/security tests.

## Current Server Boundary

The code includes the server-mode persistence layer for organizations, users, workspaces, sessions, private threads, messages, promoted workspace memory, and audit events. Legacy local-profile subsystems are blocked in server mode until each subsystem is migrated to authorized server persistence.

Authorization is enforced in the server repository layer before thread, message, and memory reads/writes. PostgreSQL RLS policies are not currently the enforcement mechanism; adding database-enforced tenant policies is a separate server release gate.
