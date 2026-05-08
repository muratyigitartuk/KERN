# KERN Pilot Release Checklist

Use this checklist when preparing a pilot handoff from `codex/product-packaging`.

## 1. Build the handoff

```powershell
.\scripts\run-kern-release-gate.ps1
```

Release evidence should end up under:

- `output/releases/release-gate-<timestamp>/`

Required artifacts:

- `kern-internal-runtime-<timestamp>.zip`
- `kern-internal-runtime-<timestamp>.zip.sha256`
- `summary.md`
- `summary.json`
- `release-gate.json`

## 2. Confirm the release gate

Open:

- `output/releases/release-gate-<timestamp>/release-gate.json`
- `output/releases/release-gate-<timestamp>/summary.md`

Pass conditions:

- `release_ready` is `true`
- every required lane is `pass`
- advisory lanes are reviewed and understood

Required lanes:

- `shell_smoke`
- `trust_governance`
- `sample_to_real_transition`
- `package_validation`
- `package_smoke_install`
- `update_restore_smoke`
- `uninstall_smoke`
- `regression_visuals`

Advisory lane:

- `busy_day_advisory`

## 3. Manual review

Review the latest screenshots and summaries for:

- onboarding wording
- support and update cards
- sample workspace labeling
- failure-card wording
- light and dark visual drift

## 4. Handoff package

Send only:

- the runtime zip
- the `.sha256` file
- `summary.md`
- the support instructions from the operator docs

Do not send:

- raw company documents
- local profile databases
- support bundles containing customer data unless explicitly approved

## 5. Operator note

The supported pilot path remains:

- one controlled Windows machine
- one local model path
- manual stable-channel updates only
