# KERN Restore Guide

Use restore only from a trusted backup and only with the correct backup password.

## Validate first

```powershell
python .\scripts\restore-kern.py <artifact> --password "<password>" --validate-only --json
```

Do not restore a bundle that fails validation.

## Restore into a target root

```powershell
python .\scripts\restore-kern.py <artifact> --password "<password>" --restore-root .\.kern\restores\default --json
```

## After restore

Run:

```powershell
python .\scripts\preflight-kern.py --json
curl http://127.0.0.1:8000/health/ready
```

Pass conditions:

- readiness is not `not_ready`
- the restored root exists
- the active runtime can open the restored profile data

## Recovery behavior

KERN restore uses staged restore + rollback handling so a failed restore does not silently replace the last good state.
