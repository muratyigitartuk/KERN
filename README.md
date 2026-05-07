# KERN

KERN is a controlled local work-preparation system for document-grounded enterprise workflows. It helps operators turn local files into cited answers, policy-aware drafts, governance evidence, and recoverable audit/support bundles without treating a cloud chat model as the product core.

## What It Does

- Ingests local PDFs, Office documents, spreadsheets, text files, and archives.
- Prepares grounded responses with visible source citations before optional LLM wording.
- Refuses or blocks unsafe requests when evidence is missing, conflicting, or instruction-like content appears inside retrieved documents.
- Applies policy gates for sensitive reads, exports, backups, and high-risk actions.
- Maintains local audit, retention, backup, update, and support-bundle evidence.
- Runs as a Windows-first local pilot with a browser dashboard and optional Tauri desktop shell.
- Includes a restricted server path for one-organization thread/auth workflows backed by PostgreSQL, Redis, and OIDC. Server mode is not yet the full document/evidence/compliance product path.

## Get Started

### Easiest Windows Run

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1
```

Open [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard) when the script reports that KERN is running.

The starter script creates the local environment when needed, installs the required Python package, prepares `.env` from `.env.example`, starts the loopback server, and prints the dashboard URL.

For first use, keep `KERN_LLM_ENABLED=false`. KERN will still open and let you test the workspace, governance, backup, and dashboard flows. For grounded language generation, install a local `llama-server` runtime and set these values in `.env`:

```dotenv
KERN_LLM_ENABLED=true
KERN_LLAMA_SERVER_URL=http://127.0.0.1:8080
KERN_LLAMA_SERVER_MODEL_PATH=C:\models\your-model.gguf
```

## Desktop Shell

KERN includes a Windows-first Tauri shell in `src-tauri/`. Use this when you want the dashboard in a desktop window instead of a browser tab.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-kern-desktop-dev.ps1
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
node --check app\static\js\workbench.js
python -m pytest -q
```

## Release And Demo Validation

Use the release gate before shipping a package or demoing to external reviewers:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-kern-release-gate.ps1
```

Use the enterprise acceptance harness when the local model runtime is installed:

```powershell
.\.venv\Scripts\python.exe .\scripts\enterprise_acceptance.py
```

The corporate demo script is in [docs/corporate-demo-script.md](docs/corporate-demo-script.md).

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
- [Deployment overview](docs/deployment-overview.md)
- [Server deployment](docs/server-deployment.md)
- [Security and governance](docs/security-governance.md)
- [Operator runbook](docs/operator-runbook.md)
- [Troubleshooting](docs/troubleshooting-guide.md)
