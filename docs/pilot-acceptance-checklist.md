# KERN Pilot Acceptance Checklist

Use this checklist after packaging or on a fresh pilot machine. Treat each item as pass/fail.

## Install and readiness

- `install-kern.ps1 -InternalDeploy` completed without manual fixes.
- `python .\scripts\preflight-kern.py --json` does not report `not_ready`.
- `/health` responds.
- `/health/ready` does not return `503`.

## Activation and evaluation

- the license card is visible in settings
- a valid offline license imports without reinstall
- an invalid license is rejected with a clear operator message
- sample workspace remains available before activation
- the sample workspace can stage one grounded drafting prompt

## Real workflow

- after activation, one real local document uploads successfully
- KERN can answer from that document instead of generic filler
- KERN can turn the grounded answer into a usable German draft
- recent active documents do not show sample content after leaving sample mode

## Support and lifecycle

- support bundle export succeeds
- encrypted backup creation succeeds
- update/restore smoke succeeds
- default uninstall preserves `.kern`
- `-RemoveData` uninstall removes `.kern` only when explicitly requested

## Operator handoff

- the runtime zip, checksum, and manifest are present
- the operator can follow the runbook without repo exploration
- support handoff instructions are available with the package
