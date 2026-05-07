# Contributing

Keep changes small, tested, and explicit about whether they affect local desktop mode, server mode, or both.

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

For server-mode changes:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-kern-server-release-gate.ps1
```

## Rules

- Do not commit `.env`, keys, databases, logs, local profile data, model files, virtual environments, build outputs, or generated runtime folders.
- Do not add cloud LLM behavior without explicit policy controls, audit events, and tests.
- Do not let private thread content appear in shared memory, shared retrieval, logs, exports, or prompt caches.
- Keep desktop mode usable without PostgreSQL or Redis.
- Keep server mode explicit; it must not silently fall back to local desktop assumptions.
