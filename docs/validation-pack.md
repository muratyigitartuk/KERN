# KERN Validation Pack

KERN ships with an advisory Playwright validation pack for browser-visible product truth, operator endpoints, screenshot-based review, and the rollout branch's `production` posture.

The pack does not use `@playwright/test`. It uses a repo-owned CLI harness that drives `@playwright/cli`, captures artifacts, and writes a human-review summary.


## Prerequisites

- Windows local environment
- Node.js and `npx`
- Python environment that can run KERN
- a browser that Playwright CLI can launch

Recommended repo setup:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[dev,documents,scheduler,system_control]
```

## Run The Full Pack

```powershell
python .\scripts\validate-kern-ui.py --launch-local
```

PowerShell convenience wrapper:

```powershell
.\scripts\validate-kern-ui.ps1
```

## Run Against An Existing Runtime

```powershell
python .\scripts\validate-kern-ui.py --base-url http://127.0.0.1:8000
```

## Run A Single Lane

```powershell
python .\scripts\validate-kern-ui.py --launch-local --lane shell_smoke
python .\scripts\validate-kern-ui.py --launch-local --lane trust_governance
python .\scripts\validate-kern-ui.py --launch-local --lane sample_to_real_transition
python .\scripts\validate-kern-ui.py --launch-local --lane busy_day_advisory
python .\scripts\validate-kern-ui.py --launch-local --lane package_validation
python .\scripts\validate-kern-ui.py --launch-local --lane package_smoke_install
python .\scripts\validate-kern-ui.py --launch-local --lane update_restore_smoke
python .\scripts\validate-kern-ui.py --launch-local --lane uninstall_smoke
python .\scripts\validate-kern-ui.py --launch-local --lane regression_visuals
```

## Artifact Layout

Artifacts are written under:

- `output/playwright/<timestamp>/`

Each run writes:

- `summary.json`
- `summary.md`
- lane directories with screenshots, snapshot YAML, console logs, and network logs
- `health.json`, `health-live.json`, `health-ready.json`, and `governance.json` in the trust/governance lane

Use `summary.md` and `summary.json` from this directory as the release review artifacts referenced by the deployment checklist and staging validation plan.

The validation summary includes a `release_gate` section:

- required lanes must be `pass`
- advisory lanes may remain `warn`
- `release_ready: true` is the pilot release signal

## Lane Meaning

- `shell_smoke`
  - app boot
  - connected WebSocket state
  - shell controls
  - production-posture gating for media-style personal controls
  - starter prompt drafting
  - settings modal
  - utility modal
  - conversation search modal
  - dark/light workspace captures
- `trust_governance`
  - health endpoints
  - governance export
  - profile/security rendering
  - corporate-mode confirmation behavior for protected actions
  - personal-posture compatibility for optional assistant controls
  - support bundle availability while expired
- `sample_to_real_transition`
  - sample workspace entry
  - staged sample drafting prompt
  - transition back to real local documents
  - document upload
  - sample documents no longer appearing as active primary content
- `busy_day_advisory`
  - fixture upload
  - document totals
  - knowledge search
  - memory search
  - schedule creation
  - audit surface presence
- `package_validation`
  - package manifest
  - checksum verification
  - required pilot docs and scripts
- `package_smoke_install`
  - packaged install
  - readiness
  - first-run UI
- `update_restore_smoke`
  - rollback bundle creation
  - restore validation
  - restored sentinel proof
- `uninstall_smoke`
  - default uninstall preserves `.kern`
  - explicit `-RemoveData` uninstall removes data
- `regression_visuals`
  - fixed screenshot set for the main workspace and important modal surfaces

## Required vs Advisory

Required release lanes:

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

## Status Model

The pack is advisory and reports:

- `pass`
- `warn`
- `fail`

Exit codes:

- non-zero for environment or catastrophic harness failure
- lane warnings and normal scenario failures are encoded in `summary.json` and `summary.md` for review

## What To Review Manually

After each run, review:

- dark/light screenshots for clipping, spacing drift, and modal polish
- trust/governance screenshots for truthful states and confirmation behavior
- rollout posture screenshots for the absence of media-first personal controls in production posture
- utility screenshots for data-density readability
- console/network logs for repeated noisy client errors
- warnings in `summary.md` for data-dependent surfaces that did not fully materialize

## One-Command Release Gate

Use the repeatable pilot handoff routine when you want the package, checksum, validation summary, and release report preserved together:

```powershell
.\scripts\run-kern-release-gate.ps1
```

Review:

- `output\releases\release-gate-<timestamp>\release-gate.json`
- `output\releases\release-gate-<timestamp>\summary.md`
