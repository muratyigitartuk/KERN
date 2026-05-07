# Security Policy

KERN is a local-first AI workspace with optional server-mode components. Treat reports involving private messages, local files, credentials, model prompts, retrieval leakage, authentication bypass, or workspace isolation as security issues.

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

Local desktop mode is designed for one local user on one machine. Corporate multi-user deployment requires server mode with PostgreSQL, Redis, OIDC, secure cookies, explicit allowed origins, and the server release gate.

## Validation Commands

```powershell
python -m pytest
python .\scripts\validate-publish-hygiene.py
powershell -ExecutionPolicy Bypass -File .\scripts\run-kern-server-release-gate.ps1
```
