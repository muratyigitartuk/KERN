# Security Policy

KERN is a local-first document AI workspace. Treat reports involving local files, credentials, model prompts, retrieval leakage, unsafe exports, path traversal, audit tampering, or workspace isolation as security issues.

## Supported Versions

Security fixes target the current `main` branch until versioned release branches exist.

## Reporting

Do not open public issues for exploitable vulnerabilities or private-data exposure. Send a private report to the project maintainer through the repository security advisory flow once the public repository is created.

Include:

- affected version or commit
- operating system
- exact startup mode: desktop, local web, or server mode
- reproduction steps
- expected and actual impact
- any logs with secrets and private content removed

## Security Boundaries

The current product is designed for one local user on one controlled machine. Do not treat this release as a shared company deployment until the server runtime, identity model, access controls, audit storage, migrations, backup, rollback, and operator validation exist in the repo.

## Validation Commands

```powershell
python -m pytest
python .\scripts\validate-publish-hygiene.py
powershell -ExecutionPolicy Bypass -File .\scripts\run-kern-release-gate.ps1
```
