# Contributing

Keep changes small, tested, and explicit about whether they affect the local desktop runtime, document intelligence, retrieval/evidence behavior, governance, or packaging.

## Setup

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1 -NoStart
```

For local LLM work, put a `.gguf` model under `models\` and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1 -EnableLlm
```

## Required Checks

```powershell
python -m pytest
python .\scripts\validate-publish-hygiene.py
powershell -ExecutionPolicy Bypass -File .\scripts\package-kern-runtime.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\validate-kern-package.ps1
```

For Tauri changes:

```powershell
Push-Location src-tauri
cargo check
Pop-Location
```

For the production desktop installer:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-kern-desktop-release.ps1
```

## Rules

- Do not commit `.env`, keys, databases, logs, local profile data, model files, virtual environments, build outputs, or generated runtime folders.
- Do not add cloud LLM behavior without explicit policy controls, audit events, and tests.
- Do not let private thread content appear in shared memory, shared retrieval, logs, exports, or prompt caches.
- Keep desktop mode usable without PostgreSQL or Redis.
- Do not add shared deployment claims before the repo contains the required runtime, tests, and operator validation.
