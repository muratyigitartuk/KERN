# KERN Update And Rollback Guide

KERN Phase 2B keeps updates on a **manual stable-channel only** policy.

## Supported update path

```powershell
.\scripts\update-kern.ps1
```

This flow now records update metadata for the product shell:

- last update attempt
- last successful update
- last backup before update
- last restore attempt
- last status

## What the update script does

- runs preflight before changing the install
- creates an encrypted rollback bundle when `.kern` exists
- validates that rollback bundle before proceeding
- upgrades the runtime environment
- reruns preflight after the update
- attempts rollback if the update fails

## What the UI shows

In `Settings > Profile`, KERN surfaces:

- current stable channel
- last backup before update
- last restore attempt
- plain-language update policy

## Rollback expectation

Rollback is still script-driven. This phase only makes the state visible in-product.

If the update fails:

1. review the failure card and update state
2. confirm the recorded backup/restore timestamps
3. use the rollback artifact produced by the update flow
4. rerun readiness and health checks

## Post-update validation

```powershell
python .\scripts\preflight-kern.py --json
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/ready
```

Related:

- [restore-guide.md](/Users/mur4t/Desktop/claudes/skillstests/docs/restore-guide.md)
- [operator-runbook.md](/Users/mur4t/Desktop/claudes/skillstests/docs/operator-runbook.md)
