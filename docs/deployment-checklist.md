# KERN Deployment Checklist

Use this checklist before every internal managed deployment.

---

## 1. Pre-Deployment

- [ ] Targeted release checks pass: `python -m pytest tests/test_config.py tests/test_release_truth.py tests/test_health_routes.py tests/test_trust_hardening.py tests/test_verification.py tests/test_powershell_scripts.py -q`
- [ ] Version bumped in `pyproject.toml` and `app/__init__.py`
- [ ] CHANGELOG.md updated with release notes
- [ ] Internal packaging branch or release checkpoint pushed to GitHub
- [ ] `.env` file created from `.env.example` with all required values set
- [ ] `KERN_PRODUCT_POSTURE=production` and `KERN_POLICY_MODE=corporate` configured
- [ ] `KERN_DB_ENCRYPTION_MODE=fernet` remains enabled unless a deliberate plaintext test is required
- [ ] Profile secret resolution is working for any `_REF`-style secrets in use, for example `KERN_EMAIL_PASSWORD_REF`
- [ ] Local LLM endpoint configured (`KERN_LLAMA_SERVER_URL`) and `KERN_LLM_LOCAL_ONLY=true`
- [ ] `KERN_ALLOW_CLOUD_LLM=false` in production/corporate rollout
- [ ] Data directories exist and have correct permissions
- [ ] Runtime package builds: `powershell -ExecutionPolicy Bypass -File scripts/package-kern-runtime.ps1`
- [ ] Package validates: `powershell -ExecutionPolicy Bypass -File scripts/validate-kern-package.ps1 output/packages/kern-internal-runtime-<timestamp>.zip`
- [ ] Extracted package smoke install passes: `powershell -ExecutionPolicy Bypass -File scripts/smoke-kern-runtime-package.ps1 output/packages/kern-internal-runtime-<timestamp>.zip`
- [ ] Update/restore smoke passes on the extracted install: `powershell -ExecutionPolicy Bypass -File scripts/smoke-kern-update-restore.ps1 -InstallRoot output/package-smoke/kern-runtime-smoke-<timestamp>`
- [ ] Package manifest and `.sha256` reviewed under `output/packages/`
- [ ] Validation pack completed: `python .\scripts\validate-kern-ui.py --launch-local`
- [ ] Validation artifacts reviewed under `output/playwright/<timestamp>/summary.md` and `output/playwright/<timestamp>/summary.json`

## 2. Docker Deployment

- [ ] Treat Docker as a staging/dev smoke target, not the primary release target
- [ ] Build image: `docker compose build`
- [ ] Verify `.env` file is bind-mounted (not baked into image)
- [ ] Verify data volume is persistent (`kern-data`)
- [ ] Configure external port via compose override or host port mapping if needed
- [ ] Verify health check passes: `docker inspect --format='{{.State.Health.Status}}' kern-workspace`
- [ ] Check `requirements.lock` inside container for dependency audit trail
- [ ] Review `pip-audit` output in build logs for known vulnerabilities

## 3. Linux (systemd) Deployment

- [ ] Treat Linux/systemd as non-primary and non-release-blocking for this hardening pass
- [ ] Create system user: `useradd --system --shell /usr/sbin/nologin kern`
- [ ] Install to `/opt/kern` with venv: `python -m venv /opt/kern/venv && /opt/kern/venv/bin/pip install ".[documents,scheduler,system_control,vector]"`
- [ ] Create env file at `/etc/kern/kern.env` (required by `ConditionPathExists`)
- [ ] Create data directories: `mkdir -p /var/lib/kern/{profiles,backups,documents} /var/log/kern`
- [ ] Set ownership: `chown -R kern:kern /var/lib/kern /var/log/kern /opt/kern`
- [ ] Copy `deploy/kern.service` to `/etc/systemd/system/`
- [ ] Reload and enable: `systemctl daemon-reload && systemctl enable --now kern`
- [ ] Verify status: `systemctl status kern`

## 4. Linux (supervisord) Deployment

- [ ] Treat Linux/supervisord as non-primary and non-release-blocking for this hardening pass
- [ ] Install supervisor: `apt install supervisor` or `pip install supervisor`
- [ ] Copy `deploy/supervisord.conf` to `/etc/supervisor/conf.d/kern.conf`
- [ ] Create log directory: `mkdir -p /var/log/kern`
- [ ] Reload: `supervisorctl reread && supervisorctl update`
- [ ] Verify: `supervisorctl status kern`

## 5. Windows Managed Task Deployment

- [ ] Treat Windows local corporate deployment with the scheduled-task path as the primary release target
- [ ] Run the internal preset on the target machine: `.\scripts\install-kern.ps1 -InternalDeploy -RegisterTask`
- [ ] Verify the managed task is registered: `.\scripts\status-kern-task.ps1`
- [ ] Verify `.\scripts\run-kern.ps1` starts the runtime cleanly when run manually
- [ ] Verify logon-start behavior through the managed task path
- [ ] Verify install path with spaces and German/Umlaut filenames
- [ ] Verify locked-profile startup and unlock/rebind flow on the target machine

## 6. Optional Windows Service Deployment

- [ ] Treat the Windows service wrapper as optional advanced supervision, not the primary internal rollout path
- [ ] Install Python 3.10+ and add to PATH
- [ ] Install pywin32: `pip install pywin32`
- [ ] Run installer as Administrator: `powershell -File scripts/install-kern-service.ps1`
- [ ] Verify service registered: `sc query KERNWorkspace`
- [ ] Check logs at `<project-dir>/.kern/kern-service.log`
- [ ] Verify install/update/remove flow for the service wrapper
- [ ] Verify restart/backoff behavior after terminating the child uvicorn process

## 7. Post-Deployment Verification

- [ ] Health endpoint responds: `curl http://localhost:8000/health`
- [ ] `/health` reports one of `ok`, `warning`, `degraded`, or `error`
- [ ] `/health/live` and `/health/ready` reflect the same runtime truth
- [ ] Metrics endpoint responds: `curl http://localhost:8000/metrics`
- [ ] WebSocket connects: open dashboard in browser, verify chat works
- [ ] Audit trail active: check Audit tab shows network monitor entries
- [ ] Structured logs flowing: verify log format matches `KERN_LOG_FORMAT` setting (text or json)
- [ ] Request IDs present in logs (look for `X-Request-ID` header in responses)

## 8. Rollback Plan

1. Stop the service (`docker compose down` / `systemctl stop kern` / `sc stop KERNWorkspace`)
2. Restore previous image or code version
3. Validate the latest `.kernbundle` or `.kernbak`: `python .\scripts\restore-kern.py <artifact> --password "<password>" --validate-only --json`
4. If database migrations ran, restore from backup (see backup path in config)
5. Run `python .\scripts\preflight-kern.py --json` after restore
6. Restart the service
7. Verify health endpoint responds

## 9. Operator Readiness

- [ ] `docs/operator-runbook.md` reviewed by the operator who will own the machine
- [ ] Install, update, and restore commands copied into the internal handoff note
- [ ] Restore smoke report reviewed under `output/package-smoke/restore-smoke/`
- [ ] Latest package checksum stored alongside the handoff bundle

## 10. Security Reminders

- Never commit `.env` to version control
- Rotate profile encryption keys only through the supported key-rotation workflow
- Run `pip-audit` periodically against installed packages
- Review `/metrics` output - it is unauthenticated by default
- In corporate mode, verify network monitor is active (shield badge = green)
