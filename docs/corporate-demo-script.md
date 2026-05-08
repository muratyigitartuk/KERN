# KERN Corporate Demo Script

Use this script for a controlled local/on-prem pilot demo. Do not present server mode as the full product path.

## Pre-Demo Proof

Show the validation artifacts first:

- Release gate: `output/releases/release-gate-20260506-204151/summary.md`
- Runtime package: `output/releases/release-gate-20260506-204151/kern-internal-runtime-20260506-204151.zip`
- Package checksum: `output/releases/release-gate-20260506-204151/kern-internal-runtime-20260506-204151.zip.sha256`

State the release truth:

- Supported now: controlled local Windows pilot.
- Not claimed: SaaS, broad multi-tenant, or full 500-user shared production.

## Command Sequence

```powershell
python .\scripts\preflight-kern.py --json
powershell -ExecutionPolicy Bypass -File .\scripts\run-kern-release-gate.ps1
```

## Demo Flow

1. Open the dashboard and show readiness.
   - Show local model status.
   - Show policy mode.
   - Show audit-chain status.

2. Upload the prepared German document corpus.
   - Show accepted document count.
   - Show rejected bad-input behavior only if asked.

3. Ask a grounded document question.
   - Use the Beispiel GmbH contract question.
   - Point to cited source chunks.
   - State that deterministic evidence preparation happens before language generation.

4. Ask a missing-fact question.
   - Use the HR document salary question.
   - Show that KERN refuses to invent missing facts.

5. Show prompt-injection handling.
   - Use the hostile document case from the validation pack.
   - Show that the document instruction is treated as untrusted content.

6. Show policy gates.
   - Trigger or open a sensitive export/read path.
   - Show confirm/deny behavior.
   - Do not bypass the confirmation.

7. Show audit and exports.
   - Open governance/support export evidence.
   - Show readiness, policy state, audit verification, and deprecated-feature absence evidence.

8. Show backup/update safety.
   - Show encrypted backup/update bundle validation.
   - Show traversal rejection from the validation evidence.

## Do Not Demo

- Server mode as full product parity.
- OCR reliability.
- Removed workplace integrations.
- Broad shared production scale.

## Closing Statement

KERN is a controlled enterprise work-preparation system for document-grounded local/on-prem workflows. LLMs help with wording only after deterministic evidence preparation and policy checks.
