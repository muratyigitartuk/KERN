# KERN Architecture

KERN has two release shapes.

## Local Desktop Product

The desktop product is a Tauri shell around a local FastAPI runtime:

- Tauri starts the backend on a free loopback port.
- The frontend is served from the local backend.
- Runtime data is stored under the desktop data root, not inside the repo.
- Optional LLM generation uses a loopback llama.cpp server.
- Redis and PostgreSQL are not required for local desktop mode.

## Local LLM Runtime

The launcher starts llama.cpp separately when `-EnableLlm` is used:

- model: local `.gguf`
- server: `llama-server.exe`
- API: OpenAI-compatible loopback HTTP
- default GPU backend: Vulkan
- CPU fallback: explicit diagnostics only

## Server / Multi-User Deployment

The server architecture is a separate release gate:

- PostgreSQL is the durable source of truth.
- Redis is ephemeral coordination only.
- private user threads are scoped by user and workspace.
- authorization is required on every read, write, WebSocket command, retrieval, export, and admin path.
- OIDC/SSO is the normal production auth path.
- legacy local-profile subsystems are blocked until migrated to authorized server persistence.

Desktop release validation and corporate server validation must be run separately.

Run the server release gate only in an environment configured for server mode:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-kern-server-release-gate.ps1
```

See [server-deployment.md](server-deployment.md) for the required infrastructure and current server-mode boundary.
