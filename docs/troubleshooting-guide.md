# KERN Troubleshooting Guide

Use this guide when the pilot install does not behave like the normal first-run path.

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
