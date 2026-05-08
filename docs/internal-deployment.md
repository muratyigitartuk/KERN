# KERN Internal Deployment

KERN's recommended local-mode shape is **one controlled internal machine** focused on one clear workflow:

**private German business drafting grounded in local company documents**

This guide is for local Windows deployment. Shared multi-user deployment is future architecture, not the current release path.

## Deployment shape

- one Windows machine inside the office or your internal environment
- KERN runs locally on that machine
- the same operator or a very small trusted group uses it
- local-only model endpoint when LLM is enabled
- profile, backups, and logs stay on trusted local storage

This is the closest practical shape to a future KERN Box without forcing hardware productization yet.

## Blessed install path

```powershell
.\scripts\install-kern.ps1 -InternalDeploy -RegisterTask
```

This preset is intentionally opinionated:

- `KERN_PRODUCT_POSTURE=production`
- `KERN_POLICY_MODE=corporate`
- `KERN_LLM_LOCAL_ONLY=true`
- `KERN_AUDIT_ENABLED=true`
- `KERN_RETENTION_ENFORCEMENT_ENABLED=true`
- stable update channel
- runtime-only dependencies by default

The **blessed product path** for pilots and internal installs is the simpler local GGUF route on one local model endpoint.

If you want the **reference-quality tuned model path** instead of that default route, install with:

```powershell
.\scripts\install-kern.ps1 -InternalDeploy -IncludeHfAdapter
```

That adds the optional HF adapter serving stack. See [hf-adapter-serving.md](hf-adapter-serving.md).

Use `-IncludeDev` only on development machines.

## What this machine should do

- run the FastAPI runtime
- hold the active KERN profile data
- store encrypted backups
- run the local model endpoint when enabled
- expose the browser UI locally
- let one operator reach a first grounded German draft in the first session

## What this machine should not be yet

- not a general public installer target
- not the host for shared multi-user deployment
- not the final OCR validation target for PaddleOCR on Windows
- not yet the simplest host for heavyweight HF adapter runtime on Windows; treat that as the reference-quality path, not the easiest one

## Operator routine

1. install with `-InternalDeploy`
2. run `python .\scripts\preflight-kern.py --json`
3. open `http://127.0.0.1:8000`
4. complete the first-run flow:
   - confirm the local profile roots
   - confirm the recommended local model path
   - upload one real document
   - ask KERN for one grounded German reply
5. verify `/health` and `/health/ready`
6. rerun the readiness view until the machine is not `not_ready`
7. validate the UI with `python .\scripts\validate-kern-ui.py --launch-local`
8. create an encrypted backup before upgrades
9. update with `.\scripts\update-kern.ps1`

## Phase 2A operator surfaces

For pilot-hardening, this branch now treats the following as product surfaces, not side notes:

- readiness summary and detailed readiness checks
- failure cards with next-step wording
- support bundle export
- explicit uninstall/data deletion behavior

Use the related operator docs for those flows:

- [operator-runbook.md](operator-runbook.md)
- [backup-guide.md](backup-guide.md)
- [restore-guide.md](restore-guide.md)
- [uninstall-data-deletion.md](uninstall-data-deletion.md)
- [troubleshooting-guide.md](troubleshooting-guide.md)

## Phase 2B and 2C product surfaces

This branch now also exposes:

- update / rollback visibility inside settings
- bundled sample workspace inside onboarding

That means a pilot operator can:

1. install KERN
2. verify readiness
3. validate the workflow with the sample workspace if needed
5. move to real local documents

Use these docs alongside the deployment guide:

- [update-rollback-guide.md](update-rollback-guide.md)
- [sample-workspace-guide.md](sample-workspace-guide.md)

## Packaging

To create a runtime-only handoff bundle from the repo:

```powershell
.\scripts\package-kern-runtime.ps1
```

This creates a zip under `output\packages\` containing:

- runtime app code
- install/run/update scripts
- package validation script
- deployment and governance docs
- environment template

It intentionally excludes training/eval clutter and large local artifacts.

The generated zip includes a `package-manifest.json` with:

- app version
- source branch
- source commit
- deployment profile

Use that manifest as the operator checkpoint before handing the bundle to another internal machine.

For the repeatable pilot release path, prefer:

```powershell
.\scripts\run-kern-release-gate.ps1
```

That routine preserves the package, checksum, validation summary, and release report under `output\releases\release-gate-<timestamp>\`.

Validate the package after building it:

```powershell
.\scripts\validate-kern-package.ps1 output\packages\kern-internal-runtime-<timestamp>.zip
```

The package builder also writes a sibling `.sha256` file for operator handoff verification.

For a stronger repo-side deployment proof, run an extracted-package smoke install:

```powershell
.\scripts\smoke-kern-runtime-package.ps1 output\packages\kern-internal-runtime-<timestamp>.zip
```

Then prove the update/restore path against that extracted install:

```powershell
.\scripts\smoke-kern-update-restore.ps1 -InstallRoot output\package-smoke\kern-runtime-smoke-<timestamp>
.\scripts\smoke-kern-uninstall.ps1 -InstallRoot output\package-smoke\kern-runtime-smoke-<timestamp>
```

## Update and rollback discipline

For internal deployment, updates should always create and validate an encrypted rollback artifact before changing the install.

Use:

```powershell
.\scripts\update-kern.ps1
```

This now:

- creates an encrypted `.kernbundle`
- validates that bundle before installing updates
- keeps a requirements snapshot for dependency rollback
- attempts preflight again after rollback if the update fails
- relies on `scripts\create-kern-update-bundle.py` as the shared bundle builder instead of inline packaging logic

## Operator runbook

Use [operator-runbook.md](operator-runbook.md) as the single operational sequence for:

- install
- startup
- validation
- packaging
- update
- restore
- uninstall

## Current boundaries

- uploaded-document QA is productized
- OCR exists in code but is not validated on this Windows Paddle runtime
- multi-user and role separation belong to future shared deployment, not this local Windows path
- dataset v3 and further model work are no longer first-priority deployment blockers
- the blessed pilot story is one local drafting workflow, even though broader KERN features remain available
