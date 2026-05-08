# KERN Operator Runbook

Use this runbook for the current **internal managed deployment** shape: one controlled machine, one trusted operator, one local model path, and one primary product workflow:

**private German business drafting grounded in local company documents**

## 1. Install

```powershell
.\scripts\install-kern.ps1 -InternalDeploy -RegisterTask
python .\scripts\preflight-kern.py --json
```

Pass conditions:

- preflight returns `status: ok`
- `deployment_profile` is `internal_managed`
- no missing runtime extras

## 2. Start

Blessed supervision route:

```powershell
.\scripts\status-kern-task.ps1
```

Manual launcher:

```powershell
.\scripts\run-kern.ps1
```

Reference-quality tuned model path:

```powershell
.\scripts\run-kern-hf-adapter-server.ps1
```

Then verify:

```powershell
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/ready
```

Pass conditions:

- `/health` responds
- `/health/ready` is not `503`
- the browser UI opens on `http://127.0.0.1:8000`
- the scheduled task path is registered if you are using the internal preset
- the default product path remains the local GGUF route unless you are intentionally validating the reference HF adapter path

## 2A. Confirm Readiness

Use one readiness source of truth before the pilot session:

```powershell
python .\scripts\preflight-kern.py --json
```

Pass conditions:

- `readiness_status` is not `not_ready`
- the local model path check is green for the normal pilot path
- profile and backup roots are writable

## 3. Reach First Value

In the same first session:

1. confirm the local profile roots in the onboarding flow
2. confirm the recommended local model path
4. either:
   - start the bundled sample workspace for a safe first validation, or
   - upload one real local document
5. ask for one grounded German business reply based on that material
6. review the cited basis before treating the output as usable

Pass conditions:

- the document is indexed into the active profile
- KERN answers from the uploaded material instead of generic assistant filler
- KERN can turn the grounded answer into a short German business reply
- the operator can identify the local storage roots and backup location from the UI

## 4. Validate

UI/product validation:

```powershell
python .\scripts\validate-kern-ui.py --launch-local
```

Review:

- `output/playwright/<timestamp>/summary.md`
- `output/playwright/<timestamp>/summary.json`

If you are validating a tuned model, prefer the HF adapter path first and only compare merged GGUF artifacts back against it later.

## 5. Package A Runtime Handoff

```powershell
.\scripts\package-kern-runtime.ps1
.\scripts\validate-kern-package.ps1 output\packages\kern-internal-runtime-<timestamp>.zip
```

Optional stronger proof before handoff:

```powershell
.\scripts\smoke-kern-runtime-package.ps1 output\packages\kern-internal-runtime-<timestamp>.zip
```

Operational proof of rollback/update after the smoke install:

```powershell
.\scripts\smoke-kern-update-restore.ps1 -InstallRoot output\package-smoke\kern-runtime-smoke-<timestamp>
.\scripts\smoke-kern-uninstall.ps1 -InstallRoot output\package-smoke\kern-runtime-smoke-<timestamp>
```

Artifacts:

- `kern-internal-runtime-<timestamp>.zip`
- `kern-internal-runtime-<timestamp>.zip.sha256`

Review the embedded `package-manifest.json` for:

- app version
- source branch
- source commit
- deployment profile

For the repeatable pilot release path, run:

```powershell
.\scripts\run-kern-release-gate.ps1
```

This preserves the package, checksum, validation summary, and release report under `output\releases\release-gate-<timestamp>\`.

## 6. Update Safely

```powershell
.\scripts\update-kern.ps1
```

This should:

- create an encrypted `.kernbundle`
- validate it before installing updates
- keep a requirements snapshot for dependency rollback
- write update-state metadata for the product shell
- rerun preflight after rollback attempts if the update fails
- use the shared bundle builder `scripts\create-kern-update-bundle.py`

## 7. Restore Or Roll Back

Validate first:

```powershell
python .\scripts\restore-kern.py <artifact> --password "<password>" --validate-only --json
```

Restore:

```powershell
python .\scripts\restore-kern.py <artifact> --password "<password>" --restore-root .\.kern\restores\default --json
```

After restore:

```powershell
python .\scripts\preflight-kern.py --json
curl http://127.0.0.1:8000/health/ready
```

## 8. Export A Support Bundle

Use the settings panel to export the support bundle when a pilot issue needs operator support.

The support bundle includes:

- health
- readiness
- config summary
- failure summary
- runtime logs

It excludes raw documents and generated business drafts by default.

Support handoff:

- send the support bundle zip
- include the package smoke, restore smoke, or uninstall smoke report if the issue came from that path
- do not send raw company documents unless the operator has explicitly approved a local diagnostic export

## 9. Sample Evaluation

Use the product shell for:

- validating the bundled sample workspace before loading real company documents
- switching from sample mode back to the real local-document path

## 10. Daily Triage

Check:

- `/health`
- `/health/ready`
- `.kern\kern-service.log` if using service mode
- `.\scripts\status-kern-task.ps1` for the primary internal supervision path
- governance export for audit and retention posture

## 11. Uninstall Or Remove Runtime

Runtime-only removal:

```powershell
.\scripts\uninstall-kern.ps1
```

Full local data removal:

```powershell
.\scripts\uninstall-kern.ps1 -RemoveData
```

Default uninstall preserves `.kern` unless the operator explicitly removes data.

## 12. Known Current Boundaries

- OCR fallback is implemented in code but not validated as a Windows production path
- this runbook covers the local Windows deployment path; shared multi-user deployment is future architecture
- local LLM/model runtime remains your responsibility on the target machine
- HF adapter serving is the current reference-quality tuned-model path; merged GGUF remains the later deployment optimization
- the Windows service wrapper is optional, not the primary internal deployment path
- KERN still contains broader utilities, but pilots should be judged first on the local document-grounded drafting workflow

Related guides:

- [backup-guide.md](backup-guide.md)
- [restore-guide.md](restore-guide.md)
- [uninstall-data-deletion.md](uninstall-data-deletion.md)
- [troubleshooting-guide.md](troubleshooting-guide.md)
- [update-rollback-guide.md](update-rollback-guide.md)
- [sample-workspace-guide.md](sample-workspace-guide.md)
- [release-checklist.md](release-checklist.md)
