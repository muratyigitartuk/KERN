# KERN Troubleshooting Guide

Use this guide when the pilot install does not behave like the normal first-run path.

## One-command start fails

Run from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1
```

For LLM mode:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1 -EnableLlm
```

## No GGUF model found

Put a model in `models\`, `%USERPROFILE%\Models`, or `%USERPROFILE%\.cache\kern\models`, or pass:

```powershell
-LlmModelPath "C:\path\to\model.gguf"
```

## Port is busy

The Tauri backend chooses a free loopback port. The LLM launcher checks the requested LLM port; if it is busy and not already serving llama.cpp, it chooses a free port and passes that to KERN.

## GPU is not used

Build a fresh Vulkan llama.cpp server:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-llama-cpp.ps1 -Vulkan
```

Then start:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1 -EnableLlm
```

Check `llama-server.err.log` or `llama-server.out.log` under the desktop log root. The log should mention Vulkan devices and offloaded layers.

## Publishing hygiene fails

Run:

```powershell
python .\scripts\validate-publish-hygiene.py --json
```

Remove or ignore the reported file. Release packages must not contain `.env`, keys, databases, logs, model files, virtual environments, build outputs, or local user data.

## 1. Readiness is not clean

Run:

```powershell
python .\scripts\preflight-kern.py --json
```

Check:

- local model path
- runtime reachability
- profile and backup roots
- schema compatibility
- missing runtime extras

## 2. The UI opens but drafting does not work

Check:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/health/ready`
- the readiness panel in the UI
- the current model path in settings

## 3. Upload fails

Typical causes:

- unsupported file type
- file too large
- local ingest failure
- storage path not writable

Next step:

- retry after a readiness rerun
- if it still fails, export a support bundle

## 4. Backup or restore fails

Check:

- backup root is writable
- password is correct
- restore target is valid
- the backup validates before restore

## 5. Support escalation

Export:

- support bundle from the UI

It includes:

- health
- readiness
- config summary
- failure summary
- runtime logs

It excludes raw documents and generated business drafts by default.
