## Summary

- 

## Scope

- [ ] Local desktop runtime
- [ ] Document ingestion/intelligence
- [ ] Retrieval/evidence/citations
- [ ] Policy/audit/governance
- [ ] Packaging/installer
- [ ] Documentation only

## Validation

- [ ] `python -m compileall app tests -q`
- [ ] `node --check app\static\app.js`
- [ ] `node --check app\static\js\dashboard-renderer.js`
- [ ] `node --check app\static\js\dashboard-events.js`
- [ ] `python -m pytest -q`
- [ ] `python .\scripts\validate-publish-hygiene.py --json`
- [ ] `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-kern-desktop.ps1 -CheckOnly`
- [ ] `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\package-kern-runtime.ps1`
- [ ] `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\validate-kern-package.ps1`

## Product Truth

- [ ] This change does not claim SaaS behavior.
- [ ] This change does not claim shared enterprise deployment is complete.
- [ ] This change does not reintroduce auth, commercial licensing, or unsupported relationship-mapping surfaces.
- [ ] Privileged or risky behavior is explicit, logged, and covered by tests.
