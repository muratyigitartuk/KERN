# KERN Security And Governance

KERN is designed for local Windows deployment with a `production` or `personal` product posture and a separate `personal` or `corporate` policy mode.

In server mode, KERN uses PostgreSQL for durable tenant data and Redis for ephemeral coordination. Redis must not be treated as a source of truth for messages, permissions, audit events, or documents.

## Core controls

- Profile DB encryption defaults to `fernet`.
- Artifact encryption is enabled by default for profile-owned document artifacts.
- Audit events are chained and verified at startup and before sensitive exports.
- Server-mode conversations are thread-scoped; private threads are visible only to the owner unless explicitly shared.
- Shared workspace memory can only be promoted explicitly by the thread owner or a workspace admin.
- Server-mode WebSocket chat requires an authenticated session and a `thread_id`.
- Server mode disables bootstrap/admin-token authentication and blocks unmigrated local-profile routes.
- Server break-glass requires explicit enablement, an IP allowlist, a configured password, a short TTL, and audit logging.
- `/health` exposes degraded reasons, background component state, and audit-chain status.
- `/governance/export` emits a review bundle containing health, security, policy, retention, audit, backup inventory, and document-classification summaries.
- `scripts/preflight-kern.py` is read-only and inspects the system DB plus the active profile DB without mutating either.
- `scripts/update-kern.ps1` creates an encrypted self-contained update bundle instead of a plaintext ZIP.
- `scripts/restore-kern.py` can validate/restore both `.kernbak` profile backups and self-contained update bundles.

## Health interpretation

- `green`: health is `ok`, audit verification is passing, and there are no runtime degraded reasons
- `yellow`: health is `warning`; investigate monitoring gaps, task registration, or locked-scaffold state before rollout
- `red`: health is `degraded` or `error`; stop sensitive operations, export governance state, and prepare restore/rollback

## Policy modes

- `KERN_POLICY_MODE=personal`
  - standard allow / confirm / deny behavior
- `KERN_POLICY_MODE=corporate`
  - stricter confirmation for bulk ingest, backup operations, audit access, and other higher-risk actions
  - external browser/network actions require confirmation unless `KERN_POLICY_ALLOW_EXTERNAL_NETWORK=true`
  - sensitive document and spreadsheet reads are restricted unless the request explicitly opts in

## Product posture

- `KERN_PRODUCT_POSTURE=production`
  - rollout default
  - workspace-first shell and documentation
  - removed workplace integrations are not part of the product surface
- `KERN_PRODUCT_POSTURE=personal`
  - optional assistant-style compatibility posture for local personal use

`product_posture` changes what is emphasized and exposed. `policy_mode` changes what is allowed, confirmed, or denied.

## Retention controls

These settings are enforced automatically and surfaced in runtime snapshot and governance export:

- `KERN_RETENTION_DOCUMENTS_DAYS`
- Deprecated legacy email data is exported/pruned only when old local databases already contain legacy tables; no active email retention setting is exposed.
- Deprecated legacy meeting data is exported/pruned only when old local databases already contain legacy tables; no active meeting-recording retention setting is exposed.
- `KERN_RETENTION_AUDIT_DAYS`
- `KERN_RETENTION_BACKUPS_DAYS`
- `KERN_RETENTION_ENFORCEMENT_ENABLED`
- `KERN_RETENTION_RUN_INTERVAL_HOURS`

The runtime records the last retention run and the most recent deletion counts.

## Document sensitivity

Ingested documents are classified heuristically into:

- `public`
- `internal`
- `confidential`
- `finance`
- `legal`
- `hr`

Classification is included in document metadata, retrieval provenance, governance export, and recent document views.

## Recovery workflow

Recommended operator sequence:

1. Run `python .\scripts\preflight-kern.py --json`.
2. Validate a backup with `python .\scripts\restore-kern.py <bundle> --password "<password>" --validate-only`.
3. If update verification fails, prefer the latest self-contained `.kernbundle` for rollback.
4. Re-check `/health/ready` and `/governance/export` before resuming use.

## Review workflow

Recommended recurring checks for corporate local deployments:

1. Export and review `/governance/export`.
2. Verify `/health` is free of degraded reasons.
3. Review recent audit events for backup, restore, sync, export, and policy confirmations.
4. Confirm upgrade backups exist before applying updates.
