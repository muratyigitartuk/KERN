# KERN

KERN is a controlled local/on-prem pilot for document-grounded enterprise workflows. It helps operators turn local files into cited answers, policy-aware drafts, governance evidence, and recoverable audit/support bundles without treating a cloud chat model as the product core.

## What It Does

- Ingests local PDFs, Office documents, spreadsheets, text files, and archives.
- Prepares grounded responses with visible source citations before optional LLM wording.
- Refuses or blocks unsafe requests when evidence is missing, conflicting, or instruction-like content appears inside retrieved documents.
- Applies policy gates for sensitive reads, exports, backups, and high-risk actions.
- Maintains local audit, retention, backup, update, and support-bundle evidence.
- Runs as a Windows-first controlled local/on-prem pilot with a browser dashboard and optional Tauri desktop shell.

## Product Truth

Current KERN can be described as a controlled local/on-prem pilot for governed document AI work.

Do not describe this release as fully enterprise-scale. The reserved final enterprise name is:

> KERN Enterprise Workspace: a single-tenant, company-controlled document AI workspace for governed internal knowledge work.

Use that name only when the deployment path supports shared company document workflows, group-based access control, durable audit, migrations, backups, rollback, and operator observability end to end.

## Project Scope

KERN is open source document AI infrastructure for companies that want local control first. The current release focuses on:

- local document ingestion and indexing
- grounded Q&A with citations
- evidence-first drafting
- policy checks, audit evidence, backups, restore, and support bundles
- Windows-first local installation through `install-kern` and `kern`

Out of scope for the current release:

- SaaS hosting
- chatbot-only wrappers
- commercial activation or seat enforcement
- browser sign-in and recovery flows
- unsupported relationship-mapping features
- shared enterprise deployment claims before the shared runtime exists

## Roadmap

Near-term work:

- keep the local workspace fast after first install
- harden upload, evidence export, restore, and support-bundle flows
- keep documentation aligned with shipped behavior
- reduce legacy assistant-era surfaces that do not serve document work

Shared deployment work remains future architecture:

- Postgres shared state
- Redis coordination
- object storage for documents and artifacts
- background workers
- identity and group-based permissions
- durable audit, migrations, observability, backup, and rollback

## Get Started

### Install KERN

Double-click `Install KERN.cmd`.

If you prefer a terminal:

```powershell
.\install-kern
```

The installer prepares the local environment, command shims, desktop runtime payload, and desktop shell. It does not start KERN.

After the first install, start KERN with:

```powershell
kern
```

Open a new terminal after installation if Windows does not recognize `kern` immediately.

For first use, keep `KERN_LLM_ENABLED=false`. KERN will still open and let you test the workspace, governance, backup, and dashboard flows. For grounded language generation, install a local `llama-server` runtime and set these values in `.env`:

```dotenv
KERN_LLM_ENABLED=true
KERN_LLAMA_SERVER_URL=http://127.0.0.1:8080
KERN_LLAMA_SERVER_MODEL_PATH=C:\models\your-model.gguf
```

## Desktop Shell

KERN includes a Windows-first Tauri shell in `src-tauri/`. Use this when you want the dashboard in a desktop window instead of a browser tab.

```powershell
.\scripts\run-kern-desktop-dev.ps1
```

## Developer Setup

Use this path only if you are changing code or running tests manually.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .[dev,documents,scheduler,system_control]
copy .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Before opening a change:

```powershell
python -m compileall app tests -q
node --check app\static\app.js
node --check app\static\js\dashboard-renderer.js
node --check app\static\js\dashboard-events.js
python -m pytest -q
```

## Release Validation

Use the release gate before shipping a package or handing a build to reviewers:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-kern-release-gate.ps1
```

The local validation walkthrough is in [docs/validation-walkthrough.md](docs/validation-walkthrough.md).

## Desktop Packaging

To stage a desktop runtime payload:

```powershell
.\scripts\package-tauri-runtime.ps1 -IncludeVenv
cd .\src-tauri
cargo tauri build
```

## Deployment Docs

- [Quickstart](docs/quickstart.md)
- [Model setup](docs/model-setup.md)
- [Windows deployment](docs/windows-deployment.md)
- [Internal deployment](docs/internal-deployment.md)
- [Architecture](docs/architecture.md)
- [Operator runbook](docs/operator-runbook.md)
- [Troubleshooting](docs/troubleshooting-guide.md)
