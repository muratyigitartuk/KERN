# KERN Sample Workspace Guide

KERN Phase 2C adds a **bundled sample workspace** inside onboarding.

## Purpose

The sample workspace lets an operator or prospect verify the core product story without using real company files:

- ask from local documents
- inspect grounded support
- draft a concise German business reply

## Where it lives

The sample path appears in the first-run onboarding flow as:

- **Use my own local documents**
- **Try sample workspace**

## What it contains

The bundled workspace ships with a small document set that models one realistic workflow:

- customer request
- service policy
- pricing addendum

All bundled content is clearly labeled as sample/demo content.

## What happens on start

When the operator starts the sample workspace:

- KERN seeds the bundled documents into the active profile
- the onboarding state switches into sample mode
- the product shell stays the same
- the user can stage one grounded drafting prompt immediately

## What happens on exit

When the operator switches back to real local documents:

- KERN exits sample mode
- the seeded sample documents are archived
- the normal local-document onboarding path becomes primary again

This avoids leaving obvious sample content mixed into the primary working surface.

## Recommended use

Use the sample workspace when:

- validating a fresh install
- demonstrating the pilot workflow
- checking the product on a machine before loading company files

Then switch to the real local-document path for actual pilot work.

Related:

- [activation-renewal-guide.md](/Users/mur4t/Desktop/claudes/skillstests/docs/activation-renewal-guide.md)
- [operator-runbook.md](/Users/mur4t/Desktop/claudes/skillstests/docs/operator-runbook.md)
