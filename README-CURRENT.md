# KERN Current-State Technical Guide

This file describes **what KERN currently is in this repository**.

It is written as a technical handoff document for another model or engineer. It is **not** marketing copy, not a roadmap, and not a speculative design note.

## Verified State

Latest verified local run in this workspace:

- `python -m compileall app tests`
- `node --check app/static/js/workbench.js`
- `python -m pytest -q`

Result from the last verified run:

- `577 passed, 1 skipped`

## Short Truth

KERN is now a **single-customer, on-prem, multi-user local workspace system** with:

- FastAPI + WebSocket control plane
- authenticated browser sessions
- OIDC as the main user login path
- loopback-only break-glass / bootstrap access
- one organization per installation
- multiple workspaces per organization
- encrypted local workspace storage and encrypted backup flows
- compliance and governance workflows
- deterministic local reasoning and prepared-work generation
- optional LLM usage as a language layer on top of local system intelligence

The most important current architectural truth is:

- **KERN is no longer just an LLM chat shell**
- **KERN now has a deterministic reasoning layer and a worker-amplifier preparation model**
- **The LLM is still present, but it is no longer supposed to be the primary brain when a local preparation path exists**

## What KERN Is Trying To Be Right Now

Current product direction:

- KERN is a **worker amplifier**
- not a fully autonomous operator
- not a system that should boss workers around
- not a product that should replace the human decision-maker by default

That means current intelligence is aimed at:

- preparing work
- gathering evidence
- finding missing context
- surfacing blockers
- building draft scaffolds
- showing why something is ready or blocked

Rather than:

- silently making important business decisions
- hiding prioritization logic
- executing material workflow decisions without the user

## Deployment Model

Current deployment assumptions:

- one organization per install
- one local deployment per customer
- multiple workspaces inside that install
- Windows desktop / managed local install remains the practical target posture
- loopback control plane remains the security boundary for admin-token and break-glass access

Important current non-goals:

- not multi-tenant SaaS
- not shared hosted cloud control plane
- not "LLM required for basic product value"

## Runtime And Workspace Model

Main runtime pieces:

- [app/main.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/main.py)
- [app/runtime.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/runtime.py)
- [app/runtime_manager.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/runtime_manager.py)

Current runtime structure:

- `RuntimeManager` owns the system platform store and lazily creates one `KernRuntime` per workspace slug.
- `KernRuntime` owns the active workspace stack:
  - platform store access
  - profile/workspace storage roots
  - identity service
  - orchestrator
  - memory repository
  - retrieval service
  - compliance/intelligence/reasoning services
  - scheduler/watchers
  - current-context service
  - backup, sync, reminders, meetings, email, retention, policy, license, and TTS services

Important current truth:

- KERN is no longer organized around one global `active_profile` for all users
- the current request/session/workspace context resolves which workspace runtime is used
- the runtime manager can switch between multiple workspace runtimes inside the same install

## Identity, Auth, Session, And Access Model

Main files:

- [app/auth.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/auth.py)
- [app/routes.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/routes.py)

Current auth model:

- OIDC is the primary user-facing login path
- a local break-glass admin path exists for recovery/operator access
- a bootstrap/admin bearer token still exists, but only for loopback-only bootstrap/recovery flows

Current public HTTP paths:

- `/`
- `/login`
- `/dashboard`
- `/health/live`
- `/health/ready`
- `/api/version`
- `/auth/break-glass/login`
- `/auth/break-glass/bootstrap`
- `/auth/oidc/login`
- `/auth/oidc/callback`

Important access rules:

- break-glass routes are loopback-only
- bootstrap/admin token usage is loopback-only
- authenticated session cookie is the normal browser auth mechanism
- WebSocket `/ws` requires either:
  - valid session cookie context, or
  - valid admin token from loopback
- WebSocket origin is checked against allowed hosts
- break-glass sessions are also loopback-limited

Current role model:

- `org_owner`
- `org_admin`
- `member`
- `auditor`
- `break_glass_admin`

Current session behavior:

- authenticated session payload includes user, roles, organization, selected workspace, and accessible workspaces
- current session state can be queried over HTTP
- sessions can be revoked by admin routes
- workspace selection is session-based, not process-global

## Current UI / Frontend Shape

Main files:

- [app/static/dashboard.html](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/static/dashboard.html)
- [app/static/dashboard.css](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/static/dashboard.css)
- [app/static/js/workbench.js](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/static/js/workbench.js)

Current UI shape:

- authenticated shell
- workspace switcher
- login page
- multi-surface workbench

Current workbench surfaces:

- `Workspace`
- `Admin`
- `Compliance`
- `Intelligence`
- `Evidence`

Current UI behavior:

- role-aware navigation
- clear denied/not-allowed states
- route-backed admin/compliance/intelligence flows
- worker-facing intelligence surface for prepared work, missing pieces, and focus hints
- governance/review surfaces for promotion candidates, training examples, exports, and regulated documents

## Deterministic Intelligence Model

Main files:

- [app/reasoning.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/reasoning.py)
- [app/intelligence.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/intelligence.py)
- [app/orchestrator.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/orchestrator.py)

Current intelligence architecture:

1. local workspace state is collected
2. workflows and obligations are derived
3. deterministic recommendations are built
4. worker-facing preparation packets are assembled
5. optional LLM wording can happen afterward

### Current reasoning service outputs

The reasoning layer currently exposes:

- `WorldStateSnapshot`
- `WorkflowRecord`
- `WorkflowEvent`
- `ObligationRecord`
- `RecommendationRecord`
- `PreparationPacket`
- `FocusHint`
- `EvidenceBundle`
- `DecisionRecord`
- `RankingExplanation`

### Current workflow families

The reasoning service currently models these workflow families:

- `correspondence_follow_up`
- `regulated_document_lifecycle`
- `review_approval_queue`
- `compliance_export_erasure`
- `scheduling_follow_through`

### Current worker-facing preparation types

The worker-amplifier pass currently uses preparation-oriented outputs like:

- `suggested_draft`
- `missing_context`
- `evidence_pack`
- `follow_up_candidate`
- `review_candidate`
- `blocked_item`
- `ready_to_finalize`

Each prepared item can carry:

- readiness status
- `why_ready`
- `why_blocked`
- `missing_inputs`
- evidence pack
- preparation scope
- worker review requirement
- optional deterministic draft scaffold
- optional focus hint

### Current deterministic transcript path

The most important runtime behavior change is:

- user transcript enters the orchestrator
- before planner/LLM analysis, KERN tries deterministic preparation routing
- if a local preparation packet can be built, KERN responds with prepared work first

Current live assistant preparation reply includes:

- "I prepared this for you"
- readiness explanation
- prepared evidence summary
- missing inputs when applicable
- deterministic draft scaffold preview when available
- explicit note that preparation came from local workflow state, memory, and ranked evidence before language generation

### Current reasoning inputs

The reasoning service currently uses:

- structured memory items
- feedback signals
- training examples
- document records
- regulated documents
- erasure requests
- data export records
- legal holds
- scheduler tasks
- saved email drafts
- retrieval hits

### Current memory/retrieval influence

The reasoning service currently does transcript-aware scoring using:

- workflow/action keyword overlap
- scoped memory retrieval hits
- document retrieval hits
- recommendation ranking score
- advisory-intent detection

This is deterministic local ranking, not model-generated reasoning.

## Worker-Amplifier Model

Current product stance:

- KERN prepares work
- KERN does not hide the basis for its suggestions
- KERN should feel like "this is already prepared for me"
- KERN should not feel like a manager issuing opaque commands

Current worker-amplifier behaviors:

- draft preparation
- evidence gathering
- missing-input detection
- blocked-state surfacing
- focus hints
- deterministic draft scaffolds
- personal/private learning first
- workspace-shared promotion only through review

Current worker-facing workbench concepts:

- `Prepared work`
- `Missing pieces`
- `Focus hints`

Current worker actions in the UI include:

- inspect preparation
- draft wording from preparation
- keep as personal pattern
- promote for workspace review
- ask for missing information
- mark not relevant

## What Is Deterministic vs What Still Uses The LLM

This distinction matters.

### Deterministic / local-first today

- auth/session/workspace resolution
- role gating
- compliance request state
- legal hold blocking
- export job/state creation
- regulated document finalization and version chain
- workbench/world-state/recommendation/preparation packet generation
- focus hints
- memory retrieval and scoped ranking
- deterministic draft scaffold generation
- upload validation and staging
- scheduler action validation
- policy allow/confirm/deny gating
- WebSocket command gating

### LLM still used today

- freeform chat fallback when no deterministic preparation route exists
- natural-language phrasing / summarization
- planner/tool routing for requests outside the deterministic prep path
- post-tool natural-language summaries

Current intended boundary:

- KERN should do the grounding and preparation
- LLM should do wording, summarization, rewriting, and clarification

It is no longer accurate to describe KERN as "LLM-first".

## Memory Model

Main files:

- [app/memory.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/memory.py)
- [app/intelligence.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/intelligence.py)

Current memory behavior:

- structured memory items are stored and queried locally
- memory carries organization/workspace/user scope
- user-private memory is restricted
- promotion state is tracked
- approvals/rejections feed back into memory scoring
- contradiction detection exists for conflicting fact values

Current memory-related concepts present in the repo:

- facts
- patterns
- feedback signals
- promotion candidates
- training examples
- decision records
- workflow-related snapshot records

Current promotion behavior:

- one user can keep a pattern private
- workspace promotion requires explicit review
- rejected patterns are tracked
- conflicting facts block promotion review

### Current intelligence feedback signals

The system already records structured feedback such as:

- `use_again`
- `promote_workspace`
- `personal_only`
- `reject_pattern`

These update local scoring and promotion state before they ever become training data.

## Retrieval And Evidence

Main files:

- [app/retrieval.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/retrieval.py)
- [app/reasoning.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/reasoning.py)

Current retrieval behavior:

- local retrieval service exists
- document retrieval is used by the reasoning service for evidence enrichment
- evidence bundles are attached to recommendations and preparation packets
- evidence metadata includes provenance-like fields such as source refs, title, reason, classification, score, and policy-safe flags

Current evidence bundle behavior:

- bundles are inspectable over HTTP
- recommendation detail returns evidence bundle and ranking explanation
- preparation packets include evidence packs directly

## Compliance / Governance / Regulated Documents

Main files:

- [app/compliance.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/compliance.py)
- [app/routes.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/routes.py)

Current compliance capabilities:

- retention policy records
- legal holds
- erasure requests
- user/workspace data export
- canonical data inventory map
- deletion tombstones
- regulated document finalization
- regulated document version chain
- support bundle export
- governance bundle export

### Current data inventory classes

The compliance service currently exposes handling metadata for:

- users
- workspace memberships
- sessions
- documents
- email drafts
- mailbox messages
- meetings
- schedules
- knowledge graph
- structured memory
- audit events
- training examples
- feedback signals
- deletion tombstones

### Current erasure behavior

Current erasure execution:

- checks active legal holds first
- blocks if hold is active
- revokes sessions
- deletes memberships and user-private local memory/training state
- pseudonymizes immutable user references
- records deletion tombstones
- persists step-by-step erasure progress

### Current workspace export behavior

Workspace exports currently gather:

- workspace metadata
- documents
- business documents
- regulated documents
- regulated document versions
- email drafts
- meetings
- structured memory
- feedback signals
- training examples
- audit events
- background jobs
- retention policies
- legal holds
- deletion tombstones

### Current user export behavior

User exports currently gather:

- user record
- memberships
- sessions
- legal holds
- erasure requests
- prior exports
- workspace-linked documents authored by or attributed to the user
- structured memory for the user
- feedback signals
- training examples
- deletion tombstones

### Current regulated document behavior

Current GoBD-style regulated document handling includes:

- finalize route
- candidate listing
- immutable finalized state
- retention state
- version list route
- content digest
- version-chain digest
- finalization actor metadata

## Training / Intelligence Review / Offline Dataset Export

Main files:

- [app/intelligence.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/intelligence.py)
- [app/routes.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/routes.py)

Current training-related truth:

- no live runtime fine-tuning
- training data export is offline-only
- examples must be approved
- personal data and legal-hold content are filtered out of dataset export

Current intelligence review surfaces:

- memory listing and detail
- feedback capture
- memory promotion
- promotion candidate listing/detail/review
- training example listing/detail/review
- training export creation/list/detail

Current training export behavior:

- collects approved training examples
- excludes personal-class examples
- excludes legal-hold examples
- deduplicates by input/output hash
- writes `train.jsonl`
- writes `validation.jsonl`
- writes `manifest.json`

## Admin And Workspace Management

Current admin surfaces include:

- workspace listing
- workspace creation
- workspace user listing
- user listing
- user creation
- user approval
- user suspension
- membership assignment
- session listing
- session revocation

Current workspace/user model:

- users belong to one organization
- users can have memberships in multiple workspaces
- roles are stored per membership
- workspace selection is an active session choice

## HTTP Route Inventory

This is the current route shape at a high level.

### Public / bootstrap / auth

- `GET /`
- `GET /dashboard`
- `GET /login`
- `POST /auth/break-glass/bootstrap`
- `POST /auth/break-glass/login`
- `GET /auth/oidc/login`
- `GET /auth/oidc/callback`
- `POST /auth/logout`
- `GET /auth/session`
- `GET /auth/session/workspaces`
- `POST /auth/session/select-workspace`

### Admin

- `GET /admin/workspaces`
- `POST /admin/workspaces`
- `GET /admin/workspaces/{workspace_slug}/users`
- `GET /admin/users`
- `POST /admin/users`
- `POST /admin/users/{user_id}/approve`
- `POST /admin/users/{user_id}/suspend`
- `POST /admin/memberships`
- `GET /admin/sessions`
- `POST /admin/sessions/{session_id}/revoke`

### Compliance

- `GET /compliance/retention-policies`
- `POST /compliance/retention-policies`
- `GET /compliance/legal-holds`
- `POST /compliance/legal-holds`
- `GET /compliance/erasure-requests`
- `GET /compliance/erasure-requests/{request_id}`
- `POST /compliance/erasure-requests`
- `POST /compliance/erasure-requests/{request_id}/execute`
- `GET /compliance/data-exports`
- `GET /compliance/data-exports/{export_id}`
- `GET /compliance/data-inventory`
- `GET /compliance/exports/user/{user_id}`
- `POST /compliance/exports/user/{user_id}/generate`
- `GET /compliance/exports/workspace/{workspace_slug}`
- `POST /compliance/exports/workspace/{workspace_slug}/generate`
- `GET /compliance/regulated-documents`
- `GET /compliance/regulated-documents/candidates`
- `POST /compliance/regulated-documents/finalize`
- `GET /compliance/regulated-documents/{regulated_id}/versions`

### Intelligence / worker amplifier / reasoning

- `GET /intelligence/world-state`
- `GET /intelligence/workbench`
- `GET /intelligence/workflows`
- `GET /intelligence/workflows/{workflow_id}`
- `GET /intelligence/obligations`
- `GET /intelligence/recommendations`
- `GET /intelligence/recommendations/{recommendation_id}`
- `GET /intelligence/focus-hints`
- `GET /intelligence/preparation`
- `GET /intelligence/preparation/{recommendation_id}`
- `POST /intelligence/preparation/{recommendation_id}/draft`
- `GET /intelligence/evidence/{bundle_id}`
- `GET /intelligence/decisions`
- `GET /intelligence/memory`
- `GET /intelligence/memory/{memory_item_id}`
- `POST /intelligence/feedback`
- `POST /intelligence/memory/promote`
- `GET /intelligence/promotion-candidates`
- `GET /intelligence/promotion-candidates/{memory_item_id}`
- `POST /intelligence/promotion-candidates/{memory_item_id}/review`
- `GET /intelligence/training-examples`
- `GET /intelligence/training-examples/{example_id}`
- `POST /intelligence/training-examples/{example_id}/review`
- `POST /intelligence/training-exports`
- `GET /intelligence/training-exports`
- `GET /intelligence/training-exports/{export_id}`

### Health / license / metrics / exports

- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /api/readiness`
- `GET /api/license`
- `POST /api/license/import`
- `GET /metrics`
- `GET /api/version`
- `POST /logs/export`
- `POST /governance/export`
- `POST /support/export`

### Upload

- `POST /upload`

Current upload behavior includes:

- filename safety validation
- no path separators
- no null bytes
- double-extension blocking for executable-like payloads
- extension allowlist
- per-file max MB
- per-batch max MB
- production/license gating
- locked-workspace rejection
- deferred retrieval refresh after successful ingestion

## WebSocket Control Plane

Main file:

- [app/ws_handlers.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/ws_handlers.py)

Current WebSocket facts:

- endpoint: `/ws`
- auth checked before accept
- loopback restriction for admin-token control plane
- origin checking
- connection rate limiting
- unlock attempt rate limiting
- submitted text size limit: `32 KiB`

### Main WebSocket command families

Current dashboard commands include:

- `submit_text`
- `confirm_action`
- `cancel_action`
- `update_settings`
- `rerun_readiness`
- `rerun_license_check`
- `start_sample_workspace`
- `start_real_workspace`
- `retry_failure_action`
- `toggle_runtime_mute`
- `reset_conversation`
- `lock_profile`
- `unlock_profile`
- `set_profile_pin`
- `create_backup`
- `restore_backup`
- `sync_mailbox`
- `save_email_draft`
- `send_email_draft`
- `search_knowledge`
- `review_action_item`
- `apply_email_reminder_suggestion`
- `reminder_action`
- `create_schedule`
- `delete_schedule`
- `toggle_schedule`
- `retry_failed_task`
- `search_memory_history`
- `dismiss_all_alerts`
- `dismiss_alert`
- `execute_suggested_action`
- `get_knowledge_graph`
- `search_knowledge_graph`
- `set_tts_speed`
- `set_tts_voice`

### Current WebSocket enforcement details

- profile lock blocks a large set of mutating commands
- production access can block selected commands based on license state
- command role requirements are enforced for sensitive actions
- schedule action types are restricted
- dashboard actions still go through policy gating where applicable
- client-visible error bodies are redacted

## Tool / Capability Inventory

Main file:

- [app/orchestrator.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/orchestrator.py)

The orchestrator currently registers capabilities in these broad groups.

### System / desktop / runtime

- open app
- open website
- browser search
- system status
- current context
- runtime snapshot
- profile security

### Tasks / notes / reminders / routines

- create task
- complete task
- pending tasks
- create note
- list notes
- create reminder
- snooze reminder
- dismiss reminder
- run routine
- focus mode
- generate morning brief
- read status
- set preference

### Memory / conversation / knowledge

- remember fact
- recall memory
- search conversation history
- build topic timeline
- build knowledge graph
- query knowledge graph

### Documents / retrieval / workspace files

- ingest document
- bulk ingest
- import conversation archive
- search documents
- summarize document
- compare documents
- query spreadsheet
- set memory scope
- search files
- read file excerpt

### Email / meeting / notifications

- read email
- read mailbox summary
- sync mailbox
- compose email
- create email reminder
- schedule meeting invite
- send ntfy notification
- start meeting recording
- stop meeting recording

### German business helpers

- create Angebot
- create Rechnung
- draft Behoerde letter
- create DSGVO reminders
- tax support query

### Scheduler / watcher / sync / backup / audit

- create schedule
- list schedules
- manage schedule
- watch folder
- sync profile data
- create backup
- list backups
- restore backup
- read audit events
- export audit trail

## Scheduler And Background Automation

Current scheduler-related truth:

- scheduler service exists in runtime
- WebSocket UI can create/delete/toggle/retry tasks
- schedule action types are currently constrained
- persisted tasks are part of runtime snapshot and admin surfaces

Current allowed action types exposed in the WebSocket layer:

- `custom_prompt`
- `summarize_emails`
- `generate_report`

Important current rule:

- scheduler is not an unrestricted "run arbitrary tool forever" engine

## License / Readiness / Health

Current system health model includes:

- `/health`
- `/health/live`
- `/health/ready`
- `/api/readiness`
- `/api/license`
- `/metrics`

Current readiness/license behavior:

- production-only actions can be blocked by license state
- readiness state is part of support bundles and runtime snapshot
- license import is a file upload flow
- locked workspace blocks license import

## Support / Governance / Evidence Exports

Current export surfaces:

- runtime logs export
- governance bundle export
- support bundle export
- user data export
- workspace data export
- training dataset export

Current support bundle contents include:

- manifest
- health
- readiness
- license summary
- update state
- config summary
- failures
- governance data
- runtime logs

Current governance bundle includes:

- policy summary
- product posture
- retention policies and status
- audit retention anchors
- health
- security
- backup inventory
- scheduled tasks
- document classifications
- audit events

## Storage / Encryption / Safety

Current storage-related truth:

- each workspace has its own storage roots
- runtime uses a workspace/profile DB plus system/platform DB
- artifact storage exists
- encrypted backup and restore flow exists
- profile lock/unlock is real
- empty-PIN unlock is not allowed for locked profiles
- locking without a configured PIN is rejected

## LLM / Model Runtime

Main runtime pieces:

- [app/runtime.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/runtime.py)
- [app/orchestrator.py](/C:/Users/mur4t/Desktop/kern-ai-codex-product-packaging/app/orchestrator.py)

Current model runtime truth:

- KERN can run without LLM support
- local llama-server is the main current LLM backend shape
- model routing modes still exist
- planner/tool-routing logic still exists for requests outside deterministic preparation

Current important boundary:

- if deterministic preparation is available, that path should win first
- if not, KERN can still fall back to planner/chat/LLM behavior

## Scripts / Operational Entry Points

Current top-level scripts include:

- `install-kern.ps1`
- `update-kern.ps1`
- `uninstall-kern.ps1`
- `register-kern-task.ps1`
- `unregister-kern-task.ps1`
- `status-kern-task.ps1`
- `run-kern.ps1`
- `preflight-kern.py`
- `restore-kern.py`
- `create-kern-update-bundle.py`
- `package-kern-runtime.ps1`
- `validate-kern-package.ps1`
- `smoke-kern-runtime-package.ps1`
- `smoke-kern-update-restore.ps1`
- `run-llama-server.ps1`
- `run-kern-hf-adapter-server.ps1`
- `run-hf-adapter-server.py`
- `run-kern-evals.py`
- `validate-kern-ui.py`
- `validate-kern-ui.ps1`
- `run-kern-release-gate.ps1`

## What Another AI Should Assume

If another model reads this repo, the safest current assumptions are:

- KERN is **session-authenticated and workspace-aware**
- KERN is **single-customer multi-user**, not public multi-tenant SaaS
- KERN has a **real compliance surface**
- KERN has a **real deterministic reasoning / worker-amplifier surface**
- KERN still has **bounded LLM fallback paths**
- KERN's current product value should be described as:
  - local reasoning
  - memory
  - retrieval
  - workflow/context preparation
  - worker assistance
  - evidence-backed drafting support

The safest summary sentence is:

> KERN is a local, authenticated, workspace-aware work-preparation system with compliance and evidence tooling, where deterministic reasoning prepares the work and LLMs optionally help express it.

## What Another AI Should Not Assume

Another model should **not** assume:

- KERN is just a chat wrapper
- KERN is purely single-user now
- KERN is autonomous by default
- KERN is trying to replace the worker
- KERN requires a cloud model to be useful
- KERN's "recommendations" are meant to be managerial orders
- KERN is event-sourced in every domain
- KERN is already a polished SaaS platform

## Current Practical Limitations

These are not necessarily bugs, but they are current reality:

- some legacy naming still uses `recommendation` internally even when the UX meaning is "prepared work"
- freeform chat still exists and can hit LLM/planner fallback
- deterministic intelligence is strongest when the workspace already has useful local state
- some workflow derivation is still heuristic/snapshot-driven rather than deep event-sourced business process modeling
- Windows/local deployment assumptions are still strong throughout the repo

## Bottom Line

KERN today is best described as:

- a local multi-user workspace system
- with real auth, session, workspace, compliance, and evidence surfaces
- with deterministic reasoning that prepares work for humans
- with worker-amplifier UX instead of hidden autonomous operator behavior
- and with LLMs positioned as optional language amplification on top of local system intelligence

That is the current code truth of this repository.

