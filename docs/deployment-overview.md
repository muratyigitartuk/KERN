# KERN Deployment Overview

KERN is currently packaged for **one controlled internal machine** inside a company environment.

## Supported now

- one Windows machine
- one trusted operator or a very small trusted group
- local browser access
- local-only model endpoint when LLM features are enabled
- encrypted profile storage and encrypted backup workflow
- managed startup through the Windows scheduled-task path
- optional HF adapter serving path for reference-quality tuned-model deployment

## What ships in the runtime package

- application runtime code
- install, run, update, backup, and restore scripts
- deployment, governance, validation, and operator documents
- environment template
- package manifest and package checksum

## Operator story

1. install with the internal preset
2. validate with preflight and UI checks
3. run daily from the managed task path
4. update only through the guarded update script
5. restore only through validated encrypted artifacts

## Security posture

- local-first deployment
- encrypted profile database by default
- encrypted backup and update-bundle path
- audit and retention controls enabled in the internal preset
- local-only LLM posture when enabled

## Known boundaries

- OCR fallback exists in code but is not yet a validated Windows production path
- multi-user / role-based deployment is deferred
- Linux server rollout is not the primary supported production shape for this phase
- the Windows service wrapper is optional and secondary to the managed scheduled-task path
- merged tuned-model deployment is not the reference truth yet; HF adapter serving is

## Recommended next step after packaging

Deploy this package on the real internal target machine and run the operator runbook end to end:

- [internal-deployment.md](/Users/mur4t/Desktop/claudes/skillstests/docs/internal-deployment.md)
- [operator-runbook.md](/Users/mur4t/Desktop/claudes/skillstests/docs/operator-runbook.md)
- [deployment-checklist.md](/Users/mur4t/Desktop/claudes/skillstests/docs/deployment-checklist.md)
