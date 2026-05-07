# Changelog

All notable changes to KERN are documented in this file.

## [1.0.0-rc1] — 2026-03-25

### Added
- **Product posture system** — `production` / `personal` modes controlling feature visibility and security defaults
- **Policy mode** — `corporate` / `personal` governing data handling, encryption, and compliance behavior
- **Platform store** — multi-tenant org/user metadata with Fernet-encrypted fields
- **Audit trail** — network monitor (psutil-based outbound connection tracking), dashboard audit viewer with export
- **Bulk document ingestion** — folder/batch ingest with SHA-256 deduplication, drag-drop upload UI
- **Cross-document reasoning** — RAG-based comparative queries across selected documents
- **Spreadsheet support** — CSV and Excel parsing via SpreadsheetParser
- **Scheduled tasks** — cron-like scheduler backed by SQLite, with file/calendar/document watchers
- **Proactive alerts** — watcher-driven alert cards with dismiss-all
- **Conversation memory** — lexical search over conversation history with date range filters and topic timeline
- **Export to action** — ActionPlanner maps alerts to contextual local actions such as reminders and document preparation
- **Knowledge graph** — offline entity extraction (person, company, date, amount) with co-occurrence edges, force-directed canvas visualization
- **Structured logging** — JSON or text format via `KERN_LOG_FORMAT`, request ID tracing
- **In-memory metrics** — lightweight counters/histograms exposed at `/metrics`
- **Health endpoint** — per-component health status at `/health`
- **Docker deployment** — multi-stage build, entrypoint with DB migration, health checks, pip-audit, configurable port
- **systemd service** — security-hardened unit file with `ConditionPathExists` for env file
- **Windows service** — pywin32 wrapper with log file redirect and exponential restart backoff
- **supervisord config** — production-ready process management configuration
- **Deployment checklist** — step-by-step guide for Docker, Linux, and Windows deployments

### Changed
- All silent `except Exception: pass` replaced with logged exceptions across 25+ modules
- Configuration migrated from `JARVIS_*` to `KERN_*` environment variables
- Docker health check start-period increased to 60s (kern) / 120s (llama-server)
- Database operations wrapped with `db_retry()` exponential backoff for SQLite lock handling
- LLM failures return graceful canned response instead of crashing

### Security
- Input validation on all API boundaries (file uploads, WebSocket messages, config values)
- Fernet encryption for sensitive platform store fields
- `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp` in systemd unit
- Network monitor flags unexpected outbound connections

### Fixed
- Windows service stdout pipe deadlock (replaced `subprocess.PIPE` with log file)
- PowerShell installer path quoting for directories with spaces
- Docker missing database initialization on first run
- supervisord missing environment variables for data paths
