# KERN Activation And Renewal Guide

KERN Phase 2B uses an **offline license file** for pilot activation.

## What activation does

- unlocks production drafting from real company documents
- keeps activation local to the install
- does not require a cloud callback

## What stays available without a production license

- onboarding
- settings and trust surfaces
- support bundle export
- backup and restore flows
- bundled sample workspace

## Import a license

1. Open KERN.
2. Go to `Settings > Profile`.
3. Use **Import offline license**.
4. Choose the signed license file for this install.
5. Confirm that the license card changes from `Unlicensed`, `Invalid`, or `Expired` to `Trial` or `Active`.

KERN stores the license file locally under the configured license root.

## Expired or invalid state

When the license is expired or invalid:

- KERN does not delete profile data
- support/export/backup paths remain available
- the sample workspace remains available for re-validation
- production drafting from real company documents is gated

## Renewal

Renewal is a replacement import:

1. obtain the new signed offline license file
2. open `Settings > Profile`
3. import the replacement file
4. recheck the license state

## Operator check

Use the license card to confirm:

- plan
- activation mode
- install ID
- expiry date
- current state

Related:

- [operator-runbook.md](operator-runbook.md)
- [internal-deployment.md](internal-deployment.md)
