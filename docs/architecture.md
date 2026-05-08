# KERN Architecture

KERN currently ships as a local desktop product. Shared company deployment is a target architecture, not a released runtime.

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

## Shared Deployment Direction

The shared deployment direction is:

- PostgreSQL is the durable source of truth.
- Redis is ephemeral coordination only.
- object storage holds documents, generated artifacts, and backups.
- background workers handle ingestion, OCR, embeddings, exports, and maintenance jobs.
- identity, group-based permissions, migrations, observability, backups, and rollback must be explicit before calling it enterprise-scale.

Do not describe the current release as shared enterprise infrastructure.
