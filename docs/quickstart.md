# KERN Quickstart

KERN desktop starts from one PowerShell command. The launcher prepares the Python environment, Tauri/Rust tooling, desktop runtime payload, and optionally a local llama.cpp LLM server.

## Start Without LLM

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1
```

## Start With Local LLM

Put a `.gguf` model in `models\`, then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1 -EnableLlm
```

The launcher searches `.\models`, `%USERPROFILE%\Models`, and `%USERPROFILE%\.cache\kern\models`.

You can also pass the model explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1 -EnableLlm -LlmModelPath "C:\path\to\model.gguf"
```

## Fast Restart

After first setup:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1 -EnableLlm -SkipToolInstall -SkipPythonInstall
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
