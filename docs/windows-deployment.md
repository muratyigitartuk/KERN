# KERN Windows Deployment

KERN currently targets `local Windows deployment` with a strong single-user posture, a growing corporate mode, and a `production` product posture that defaults to the workspace-first shell.

Supported topology matrix:

- `Supported`: Windows internal-machine runtime, per-user managed installs, scheduled-task startup
- `Limited`: Docker/dev container use for development and API smoke testing
- `Future topology`: shared deployment with PostgreSQL, Redis, HTTPS/proxy, durable object storage, background workers, identity, and migrations

For this Windows guide, the release-blocking target is one controlled internal Windows machine, not a broad desktop rollout.

Rollout defaults:

- `KERN_PRODUCT_POSTURE=production`
- `KERN_POLICY_MODE=corporate` for managed installs unless a personal rollout is intended
- `KERN_LLM_LOCAL_ONLY=true` when local inference is enabled
- media features remain optional personal-posture features

## Managed local install

```powershell
.\install-kern
.\scripts\install-kern.ps1 -Managed -Corporate -RegisterTask
```

Recommended internal preset:

```powershell
.\scripts\install-kern.ps1 -InternalDeploy -RegisterTask
```

This:

- creates `.venv`
- installs the runtime extras needed for internal deployment
- copies `.env.example` to `.env` if missing
- prepares `.kern\profiles` and `.kern\backups`
- runs an import smoke check
- runs post-install preflight
- optionally registers the Windows startup task
- applies production-posture and corporate-policy defaults in `.env`

This scheduled-task path is the **blessed supervision route** for the current internal deployment shape.

## Update procedure

```powershell
.\scripts\update-kern.ps1
```

This:

- runs a preflight check before update
- creates an encrypted self-contained update bundle under `.kern\upgrade-backups`
- updates the editable install with required extras
- runs `compileall`
- runs an import smoke check
- runs a post-update preflight check
- attempts dependency and `.kern` rollback if the update fails after the bundle is created

## Preflight

```powershell
python .\scripts\preflight-kern.py --json
```

This emits status, warnings, errors, schema visibility, encryption posture, and deployment-path checks.

## Restore

```powershell
python .\scripts\restore-kern.py .\.kern\backups\default\default-YYYYMMDD-HHMMSS.kernbak --password "<password>" --restore-root .\.kern\restores\default --json
python .\scripts\restore-kern.py .\.kern\upgrade-backups\update-YYYYMMDD-HHMMSS.kernbundle --password "<password>" --restore-root .\.kern\restores\default --json
```

Use `--validate-only` to verify a backup without restoring it. `.kernbak` is the profile backup format used by the runtime; `.kernbundle` is the encrypted self-contained update bundle created by `scripts/update-kern.ps1`.

## Runtime supervision

Start KERN with:

```powershell
kern
```

Normal launch must use the installed desktop shell. It must not compile Rust, rebuild the desktop runtime payload, or reinstall Python packages. If the desktop launcher is missing, rerun `install-kern`.

Monitor:

- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /governance/export`

For local inference, run `llama-server` on `127.0.0.1` only and point `KERN_LLAMA_SERVER_URL` at that localhost endpoint.

For tuned-model fidelity, there is now a second operator path:

```powershell
.\scripts\install-kern.ps1 -InternalDeploy -IncludeHfAdapter
.\scripts\run-kern-hf-adapter-server.ps1
```

This keeps the same local endpoint shape for KERN, but serves a HF + PEFT adapter behind it. Treat it as the **reference-quality path** for tuned models, with merged GGUF remaining the later simplification target.

## Start At Logon

```powershell
.\scripts\register-kern-task.ps1
.\scripts\status-kern-task.ps1
.\scripts\unregister-kern-task.ps1
```

This registers a per-user Windows scheduled task that starts the local runtime at logon. Use `-AtStartup` if you want boot-start semantics instead.

For the current product phase, prefer this route over the Windows service wrapper.

## Optional Windows service wrapper

`scripts/install-kern-service.ps1` remains available for advanced supervision scenarios, but it is **not** the primary internal rollout target.

Recommended supervision signals:

- `audit_chain_ok`
- `runtime_degraded_reasons`
- `background_components`
- `network_status`
- `pending_alert_count`
- `read_only` preflight status

Recommended release-blocking checks on the target machine:

- install/update/remove service wrapper successfully
- terminate uvicorn once and confirm restart/backoff behavior
- run from a path with spaces
- ingest files with German/Umlaut names such as `Prüfbericht.pdf`
- confirm locked-profile startup fails clearly until the local profile can be opened

Runbook:

- `green`: `/health/ready` returns `200`, `audit_chain_ok=true`, no degraded reasons
- `yellow`: `/health` returns `200` with `status=warning`; investigate task registration, monitoring, or locked-profile state
- `red`: `/health` returns `503` or `500`; stop rollout, inspect preflight and governance export, then restore from the latest `.kernbak` or `.kernbundle`

## Local data layout

- `.kern\profiles\...`
- `.kern\backups\...`
- `.kern\upgrade-backups\...`

Keep profile and backup roots on local trusted storage. For shared corporate rollouts, document these roots explicitly and back them up before upgrades.

## Operational notes

- WebDAV/Nextcloud remains encrypted export/upload-oriented, not conflict-aware full sync.
- Review policy mode and retention settings in `.env`.
- For corporate local installs, prefer `KERN_POLICY_MODE=corporate`.
- OCR is implemented in KERN but PaddleOCR is not yet a validated Windows production path; treat OCR as deferred on Windows unless you validate the exact target machine.
- HF adapter serving exists as the highest-fidelity tuned-model path, but Windows-native GPU/runtime compatibility is still a host-specific validation concern.
