# KERN Deployment Overview

KERN has two deployment shapes with different release status:

- **Supported local mode** for one controlled internal machine.
- **Restricted server mode** for a single-organization, shared thread/auth deployment. It is not yet the full document/evidence/compliance product path.

## Supported now

### Local mode

- one Windows machine
- one trusted operator or a very small trusted group
- local browser access
- local-only model endpoint when LLM features are enabled
- encrypted profile storage and encrypted backup workflow
- managed startup through the Windows scheduled-task path
- optional HF adapter serving path for reference-quality tuned-model deployment

### Restricted server mode

- PostgreSQL-backed organizations, users, workspaces, sessions, private threads, messages, memory promotion records, and audit events
- Redis-backed production rate limiting with fail-closed behavior if Redis is unavailable
- OIDC/SSO as the normal authentication path
- thread-scoped WebSocket chat requiring `thread_id`
- private user threads by default, with explicit sharing and owner/admin-only memory promotion
- server-mode route guard that blocks local-profile document, evidence, and compliance subsystems until they are migrated to authorized server persistence

## What ships in the runtime package

- application runtime code
- install, run, update, backup, and restore scripts
- deployment, governance, validation, and operator documents
- environment template
- package manifest and package checksum

## Operator story

1. install with the internal preset
2. validate with preflight and UI checks
3. run daily from the managed task path
4. update only through the guarded update script
5. restore only through validated encrypted artifacts

## Security posture

- local-first deployment
- encrypted profile database by default
- encrypted backup and update-bundle path
- audit and retention controls enabled in the internal preset
- local-only LLM posture when enabled

## Known boundaries

- OCR fallback exists in code but is not yet a validated Windows production path
- Linux server rollout is not the primary supported production shape for this phase
- the Windows service wrapper is optional and secondary to the managed scheduled-task path
- merged tuned-model deployment is not the reference truth yet; HF adapter serving is
- server mode requires external PostgreSQL, Redis, OIDC, HTTPS/proxy, and durable object storage configuration
- server mode currently enforces tenant access in the repository layer; PostgreSQL RLS policies remain a separate server release gate

## Recommended next step after packaging

Deploy this package on the real internal target machine and run the operator runbook end to end:

- [internal-deployment.md](internal-deployment.md)
- [operator-runbook.md](operator-runbook.md)
- [deployment-checklist.md](deployment-checklist.md)
