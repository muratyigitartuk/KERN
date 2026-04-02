# KERN Local AI Workspace

A Windows desktop-first KERN runtime focused on privacy-first workspace intelligence: documents, memory, retrieval, governance, recovery, and local operator trust.

Production topology:

- `supported`: local Windows deployment, single-user or managed local corporate installs
- `limited`: Docker/dev container use for development or API smoke checks only

## Features

- `Workspace-first shell`: FastAPI dashboard, WebSocket runtime view, conversation search, settings, utility surfaces, and local operator endpoints
- `Voice output`: optional offline TTS with `Piper` as the preferred local voice path, secondary to the text/UI workflow
- `Cognition`: deterministic rules, semantic paraphrase matching, `llama.cpp` planning, and optional local **llama-server** inference with function calling for tool dispatch
- `Policy`: risk-gated plans with allow / confirm / deny decisions
- `Profiles and security`: per-profile storage roots, lock/unlock session state, audit events, background jobs, encrypted backups, encrypted secret store, encrypted artifact storage, and encrypted profile DB mode
- `Local data`: notes, tasks, calendar, reminders, preferences, facts, open loops, and execution receipts in SQLite
- `Documents and archives`: single-file and bulk ingestion, watch-folder ingestion, archive import, indexed chunks, spreadsheet parsing, lexical retrieval fallback, TF-IDF vector index, and optional **sqlite-vec** KNN retrieval backed by real embeddings
- `Email and admin loop`: local email indexing, invite-draft flow, email-to-reminder hooks, and ntfy notification support
- `Corporate helpers`: scheduled tasks, proactive alerts, conversation history search, knowledge graph exploration, and runtime health reporting
- `Current context`: foreground-window awareness, optional clipboard context, Windows media-session context, and live local context summaries
- `Meetings and German workflows`: meeting recording/review workflows plus Angebot, Rechnung, Behörde, DSGVO, and tax-support helpers
- `Routines`: local morning, focus, and shutdown routines built on the same SQLite data model
- `Dashboard`: live runtime state, diagnostics, plans, receipts, capability status, reminders, and profile security actions
- `Optional personal posture`: media-style assistant controls remain available only when `KERN_PRODUCT_POSTURE=personal`

## Recommended Internal Deployment

For the current product phase, the recommended shape is a **single controlled internal machine**:

```powershell
.\scripts\install-kern.ps1 -InternalDeploy -RegisterTask
```

Then:

```powershell
python .\scripts\preflight-kern.py --json
.\scripts\run-kern.ps1
```

This is the preferred path over treating KERN as a general public installer right now. See `docs/internal-deployment.md`.

For the operator sequence after install, use `docs/operator-runbook.md`.

For a concise company-facing summary of the current supported deployment shape, use `docs/deployment-overview.md`.

## Developer Quick Start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[dev,documents,scheduler,system_control]
copy .env.example .env
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) for the KERN dashboard. [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard) remains available as the same runtime view.

## Local runtime options

- `KERN_PRODUCT_POSTURE`: `production` or `personal`, default `production` on the rollout branch
- `KERN_PROFILE_ROOT`: profile root folder used for isolated per-user storage
- `KERN_BACKUP_ROOT`: default encrypted backup export root
- `KERN_DOCUMENT_ROOT`: reserved shared document root for future ingestion workflows
- `KERN_AUDIT_ENABLED`: enable audit-event collection for profile and backup actions
- `KERN_POLICY_MODE`: `personal` or `corporate`, default `personal`
- `KERN_POLICY_ALLOW_EXTERNAL_NETWORK`: allow browser/network actions without extra corporate confirmation, default `false`
- `KERN_COGNITION_BACKEND`: `hybrid` by default, or `llama_cpp` if you install the local planner extra and provide a model path
- `KERN_COGNITION_MODEL`: optional path to a `llama.cpp` GGUF model used for local plan generation
- `KERN_LLM_ENABLED`: set `true` to enable llama-server inference (default `false`)
- `KERN_LLM_LOCAL_ONLY`: require the LLM endpoint to remain on `127.0.0.1` / `localhost`
- `KERN_ALLOW_CLOUD_LLM`: reserved future opt-in, default `false`
- `KERN_LLAMA_SERVER_URL`: base URL for the local llama-server process, default `http://127.0.0.1:8080`
- `KERN_LLAMA_SERVER_BINARY`: optional explicit path to `llama-server.exe`
- `KERN_LLAMA_SERVER_MODEL_PATH`: GGUF file or folder path used by the local launcher script
- `KERN_LLAMA_SERVER_LORA_PATH`: optional GGUF LoRA adapter path for llama-server experiments
- `KERN_LLM_MODEL`: explicit model alias requested over the API
- `KERN_LLAMA_SERVER_TIMEOUT`: HTTP timeout in seconds for llama-server requests, default `30.0`
- `KERN_LLM_MAX_TOKENS`: max tokens per LLM response, default `1024`
- `KERN_LLM_TEMPERATURE`: LLM sampling temperature, default `0.3`
- `KERN_LLM_CONTEXT_WINDOW`: LLM context window hint, default `8192`
- `KERN_HF_ADAPTER_MODEL`: base HF model ID for adapter serving
- `KERN_HF_ADAPTER_PATH`: PEFT adapter directory path
- `KERN_HF_ADAPTER_ALIAS`: served alias, for example `KERN-qwen`
- `KERN_HF_ADAPTER_PORT`: local port for the HF adapter server
- `KERN_HF_ADAPTER_TRUST_REMOTE_CODE`: optional model-family toggle for the HF adapter launcher
- `KERN_HF_ADAPTER_LOAD_IN_4BIT`: optional 4-bit adapter serving toggle when the environment supports it
- `KERN_EMBED_MODEL_PATH`: path to a GGUF model loaded in embedding-only mode via `llama-cpp-python`
- `KERN_VEC_ENABLED`: set `true` to use sqlite-vec KNN retrieval backed by real embeddings (default `false`)
- `KERN_RAG_ENABLED`: enable local TF-IDF indexed retrieval (default `false`)
- `KERN_RAG_EMBED_MODEL`: reserved embedding model path for TF-IDF index metadata
- `KERN_RAG_INDEX_VERSION`: rebuild marker for the local retrieval index
- `KERN_DB_ENCRYPTION_MODE`: `fernet` by default for encrypted profile database persistence
- `KERN_KEY_DERIVATION_VERSION`: security metadata version for encrypted profile data
- `KERN_PROFILE_KEY_ROTATION_REQUIRED`: advisory rotation flag exposed in settings
- `KERN_PROACTIVE_ENABLED`: enable proactive prompts from local signals such as focus windows and open commitments
- `KERN_TTS_PREFERENCE`: `piper` by default, or `pyttsx3` if you want the pure-Python fallback path
- `KERN_MONITOR_INTERVAL_SECONDS`: runtime monitor cadence, default `0.35`
- `KERN_SNAPSHOT_DIRTY_DEBOUNCE_MS`: minimum time between dirty snapshot pushes, default `120`
- `KERN_CONTEXT_REFRESH_SECONDS`: max age for cached active-context rebuilds, default `1.5`
- `KERN_CAPABILITY_REFRESH_SECONDS`: max age for cached capability refreshes, default `3.0`
- `KERN_SEED_DEFAULTS`: when `true`, seed demo tasks/events/reminders into a fresh database
- `KERN_MODEL_MODE`: local LLM routing mode, `off`, `fast`, `deep`, or `auto`
- `KERN_FAST_MODEL_PATH`, `KERN_DEEP_MODEL_PATH`: optional local model aliases or GGUF paths used for routed llama-server requests
- `KERN_PROMPT_CACHE_ENABLED`, `KERN_PROMPT_CACHE_SIZE`: enable and size the local prompt-response cache for repeated LLM requests
- `KERN_CONTEXT_WINDOW_ENABLED`, `KERN_CONTEXT_MEDIA_ENABLED`: enable live foreground-window and media-session context collection
- `KERN_CONTEXT_CLIPBOARD_ENABLED`: opt in to clipboard context collection, default `false`
- `KERN_CONTEXT_CLIPBOARD_MAX_CHARS`: max clipboard excerpt length when clipboard context is enabled
- `KERN_IMAP_HOST`, `KERN_SMTP_HOST`, `KERN_EMAIL_USERNAME`, `KERN_EMAIL_PASSWORD`, `KERN_EMAIL_ADDRESS`: local email configuration
- `KERN_NTFY_TOPIC`, `KERN_NTFY_BASE_URL`: self-hosted push notifications
- `KERN_RETENTION_DOCUMENTS_DAYS`, `KERN_RETENTION_EMAIL_DAYS`, `KERN_RETENTION_TRANSCRIPTS_DAYS`, `KERN_RETENTION_AUDIT_DAYS`, `KERN_RETENTION_BACKUPS_DAYS`: retention windows enforced and surfaced in runtime and governance export
- `KERN_RETENTION_ENFORCEMENT_ENABLED`, `KERN_RETENTION_RUN_INTERVAL_HOURS`: automatic retention controls
- `KERN_NEXTCLOUD_URL`: optional WebDAV/Nextcloud sync target
- `KERN_PWA_ENABLED`: reserved mobile companion toggle for the local PWA shell

Some legacy `JARVIS_*` env names are still accepted for backward compatibility on older core settings, but `KERN_*` is the primary and complete surface.

Runtime optimization notes:

- snapshot updates are now dirty-driven instead of rebuilding every panel on every monitor tick
- active context, capability status, and recent receipts are cached and refreshed on bounded intervals
- SQLite runs with `WAL`, `foreign_keys=ON`, and `synchronous=NORMAL` for smoother always-on local use
- demo seed data is opt-in via `KERN_SEED_DEFAULTS=true`
- active profile state, audit events, backup targets, and background jobs are exposed in the dashboard snapshot
- retrieval health, recent knowledge hits, email reminder suggestions, meeting review queue, sync health, and profile DB encryption state are exposed in the dashboard snapshot

Storage truth:

- `secrets + encrypted backups` are protected at rest
- profile SQLite data is encrypted at rest by default
- profile artifacts are stored through the encrypted artifact layer when artifact encryption is enabled
- update bundles produced by `scripts/update-kern.ps1` are encrypted and self-contained for local restore workflows

If you want the pure-Python base tier, install the project normally and set `KERN_TTS_PREFERENCE=pyttsx3`.

Optional local upgrades:

```powershell
pip install -e .[local_brain]       # llama-cpp-python for embedding-only mode
pip install -e .[vector]            # sqlite-vec for KNN vector retrieval
pip install -e .[system_control]
```

### LLM inference via llama-server

KERN's inference backend connects to a running local **llama-server** process (llama.cpp's OpenAI-compatible HTTP server) rather than loading a model in-process. "OpenAI-compatible" here refers only to the request format. The traffic stays local when the endpoint is `127.0.0.1`.

Start the local server with:

```powershell
.\scripts\run-llama-server.ps1
```

Then set:

```powershell
$env:KERN_LLM_ENABLED = "true"
$env:KERN_LLM_LOCAL_ONLY = "true"
$env:KERN_LLAMA_SERVER_URL = "http://127.0.0.1:8080"
$env:KERN_LLM_MODEL = "eurollm9b"
```

The LLM powers:
- Conversational replies with streaming token output in the dashboard thread
- Function calling for tool dispatch (replaces keyword-based intent matching when confidence is low)
- Natural language summarization of multi-step tool execution results

If llama-server is unavailable, KERN falls back to the rule engine and persona patterns with no errors.

### HF adapter serving

When KERN needs to stay closest to the behavior validated in the HF / PEFT fine-tuning stack, use the packaged HF adapter server instead of the GGUF path:

```powershell
.\scripts\install-kern.ps1 -InternalDeploy -IncludeHfAdapter
.\scripts\run-kern-hf-adapter-server.ps1
```

This keeps KERN talking to the same local OpenAI-compatible endpoint shape, but serves a Transformers + PEFT model behind it.

Use this as the **reference-quality deployment path** for tuned models. Use merged GGUF artifacts later only after they are compared back against this path.

See [hf-adapter-serving.md](/Users/mur4t/Desktop/claudes/skillstests/docs/hf-adapter-serving.md).

### Vector retrieval via sqlite-vec

When `KERN_VEC_ENABLED=true` and `KERN_EMBED_MODEL_PATH` points to a GGUF embedding model, KERN switches from TF-IDF cosine similarity to KNN search via the **sqlite-vec** extension:

```powershell
$env:KERN_VEC_ENABLED = "true"
$env:KERN_EMBED_MODEL_PATH = "C:\models\nomic-embed-text-v1.5.Q4_K_M.gguf"
```

Falls back to TF-IDF or lexical retrieval if unavailable.

Productization helpers:

- `Dockerfile`: basic containerized KERN runtime
- `scripts/install-kern.ps1`: install helper with an internal deployment preset, managed/corporate switches, and post-install preflight
- `scripts/package-kern-runtime.ps1`: create a runtime-only internal deployment zip under `output\packages`
- `scripts/validate-kern-package.ps1`: verify a packaged runtime zip, its manifest, and its checksum
- `scripts/update-kern.ps1`: local update helper that creates an encrypted self-contained upgrade bundle and runs smoke verification
- `scripts/preflight-kern.py`: read-only local deployment/schema preflight report
- `scripts/restore-kern.py`: local encrypted-backup validate/restore helper for `.kernbak` and self-contained update bundles
- `scripts/register-kern-task.ps1`: register a Windows logon or startup task for the local runtime
- `scripts/status-kern-task.ps1`, `scripts/unregister-kern-task.ps1`: inspect or remove the managed Windows runtime task
- `scripts/run-kern.ps1`: stable launcher used by the scheduled task path
- `scripts/run-kern-hf-adapter-server.ps1`: operator launcher for the reference-quality HF adapter server path
- `scripts/run-hf-adapter-server.py`: packaged OpenAI-compatible HF + PEFT adapter server
- `scripts/install-kern-service.ps1`: optional advanced Windows service wrapper, not the primary internal rollout path
- `scripts/run-kern-evals.py`: local eval runner for routing, retrieval, memory recall, model routing, knowledge graph, prompt-cache, policy truth, proactive ranking, and cross-document reasoning
- `scripts/validate-kern-ui.py`: advisory Playwright CLI harness for rollout posture, screenshots, and browser-visible validation
- `GET /health`, `GET /health/live`, `GET /health/ready`: machine-readable runtime supervision endpoints
- `GET /governance/export`: review bundle for policy, retention, security, audit, backup inventory, and document classification

Operator guides:

- `docs/deployment-overview.md`
- `docs/internal-deployment.md`
- `docs/operator-runbook.md`
- `docs/security-governance.md`
- `docs/windows-deployment.md`
- `docs/hf-adapter-serving.md`
- `docs/validation-pack.md`

Eval usage:

```powershell
python .\scripts\run-kern-evals.py --json --compare-baseline
python .\scripts\run-kern-evals.py --json --include-optional
```
