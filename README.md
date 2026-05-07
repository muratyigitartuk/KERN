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

## Supported Deployment Truth

- Supported now: controlled local Windows pilot.
- Restricted: one-organization server thread/auth mode after the server release gate passes.
- Not claimed: SaaS, broad multi-tenant hosting, or full shared enterprise production parity.

## Get Started

### 1. Create the Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .[dev,documents,scheduler,system_control]
```

### 2. Configure local settings

```powershell
copy .env.example .env
```

For a basic local run, keep `KERN_LLM_ENABLED=false`. For grounded language generation, install a local `llama-server` runtime and set:

```powershell
$env:KERN_LLM_ENABLED = "true"
$env:KERN_LLAMA_SERVER_URL = "http://127.0.0.1:8080"
$env:KERN_LLAMA_SERVER_MODEL_PATH = "C:\models\your-model.gguf"
```

### 3. Validate the install

```powershell
python .\scripts\preflight-kern.py --json
python -m compileall app tests -q
python -m pytest -q
```

### 4. Run KERN locally

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard).

## Desktop Shell

KERN includes a Windows-first Tauri shell in `src-tauri/`. It launches the FastAPI runtime on loopback, waits for readiness, and opens the dashboard in a desktop WebView.

```powershell
.\scripts\run-kern-desktop-dev.ps1
```

To stage a desktop runtime payload:

```powershell
.\scripts\package-tauri-runtime.ps1 -IncludeVenv
cd .\src-tauri
cargo tauri build
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

## Deployment Docs

- [Quickstart](docs/quickstart.md)
- [Model setup](docs/model-setup.md)
- [Windows deployment](docs/windows-deployment.md)
- [Deployment overview](docs/deployment-overview.md)
- [Server deployment](docs/server-deployment.md)
- [Security and governance](docs/security-governance.md)
- [Operator runbook](docs/operator-runbook.md)
- [Troubleshooting](docs/troubleshooting-guide.md)

## Contributing

Before opening a change:

```powershell
python -m compileall app tests -q
node --check app\static\js\workbench.js
python -m pytest -q
```

Keep product claims tied to implemented behavior. Removed workplace integrations must not reappear in runtime capabilities, docs, tests, dashboard controls, or environment templates.
