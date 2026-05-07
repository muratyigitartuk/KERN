# KERN Quickstart

KERN installs once, then starts directly into the local document workspace. Normal launch does not rebuild the desktop shell or reinstall Python dependencies.

## Install

```powershell
.\install-kern
```

This prepares the Python environment, command shims, desktop runtime payload, and desktop shell.

## Start

```powershell
kern
```

Open a new terminal if Windows does not recognize `kern` immediately after installation.

## Local LLM

KERN opens without an LLM. For grounded language generation, run a local OpenAI-compatible `llama-server` on `127.0.0.1` and set `KERN_LLM_ENABLED=true`, `KERN_LLAMA_SERVER_URL`, and `KERN_LLAMA_SERVER_MODEL_PATH` in `.env`.

## Repair Install

If startup says KERN is not installed yet, rerun:

```powershell
.\install-kern
```

## Validate Before Release

```powershell
python -m pytest
python .\scripts\validate-publish-hygiene.py
powershell -ExecutionPolicy Bypass -File .\scripts\package-tauri-runtime.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\run-kern-release-gate.ps1
```

## Build The Windows Installer

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-kern-desktop-release.ps1
```

This builds the Tauri NSIS installer with the Python virtual environment included in the desktop runtime payload.
