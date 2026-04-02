# Kern AI Codex — Exhaustive Security & Code Audit Report

> **Audit Date:** 2026-03-31
> **Auditor:** Claude Opus 4.6 (Automated Deep Audit)
> **Scope:** Full codebase — every `.py`, `.js`, `.ps1`, `.sh`, config, and infrastructure file
> **Methodology:** Adversarial read + maintainer read across 5 dimensions: Security, Bugs, Performance, Architecture, Subtle issues

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Critical Findings](#critical-findings)
3. [High Severity Findings](#high-severity-findings)
4. [Medium Severity Findings](#medium-severity-findings)
5. [Low Severity Findings](#low-severity-findings)
6. [Top 3 Highest-Priority Fixes](#top-3-highest-priority-fixes)
7. [Systemic Observation](#systemic-observation)
8. [Deployment Readiness Assessment — German Corporate Context](#deployment-readiness-assessment--german-corporate-context)
9. [Next Steps — Actionable Roadmap](#next-steps--actionable-roadmap)

---

## Executive Summary

This audit identified **~110 unique findings** across 100+ files. The codebase implements a local-first AI assistant with LLM orchestration, RAG retrieval, document management, email integration, and a WebSocket-based dashboard.

**The most dangerous pattern:** The application has **zero authentication** on any endpoint or WebSocket connection. Combined with multiple path traversal vulnerabilities, command injection vectors, and a license system that fails open, any attacker who can reach the server has full control over the system — including reading arbitrary files, executing commands, and exfiltrating data via email.

### Severity Distribution

| Severity | Count | Description |
|----------|-------|-------------|
| Critical | 7     | Immediate exploitation possible; full system compromise |
| High     | 18    | Serious vulnerabilities requiring prompt remediation |
| Medium   | 42    | Significant issues that compound or enable escalation |
| Low      | ~43   | Minor issues, defense-in-depth gaps, code quality |

---

## Critical Findings

These findings can be exploited immediately to achieve system compromise, data exfiltration, or arbitrary code execution.

---

### C-01: Zero Authentication on All Endpoints and WebSocket

- **Location:** `app/main.py:17,40,44-45` — FastAPI app creation; `app/ws_handlers.py:113-114` — WebSocket endpoint
- **Category:** Security
- **What the code does:** The FastAPI application is created without any authentication middleware. All HTTP routes and the WebSocket endpoint accept connections from any client without credentials.
- **What the problem is:** Every operation is publicly accessible to anyone who can reach the server:
  - `/health` — system internals
  - `/upload` — file upload
  - `/governance/export`, `/support/export`, `/logs/export` — full audit trail and configuration export
  - `/license/import` — license manipulation
  - WebSocket `/ws` — the most privileged endpoint: backup/restore, email sending, file ingestion, PIN management, profile unlocking, schedule deletion, action execution

  The config defines `admin_auth_token` (`app/config.py:196`) via `KERN_ADMIN_AUTH_TOKEN`, but this token is **never checked anywhere** in the codebase. An operator who sets this variable believes they are protected — they are not.
- **Recommended fix:**
  1. Implement authentication middleware that validates the admin auth token on all non-health endpoints.
  2. For the WebSocket endpoint, require a token in the connection handshake (query parameter or first message).
  3. At minimum, bind to `127.0.0.1` only and verify this is enforced (not `0.0.0.0`).

---

### C-02: Cross-Site WebSocket Hijacking (CSWSH)

- **Location:** `app/ws_handlers.py:113-114` — no Origin validation; `app/csrf.py:27` — CSRF only checks POST/PUT/DELETE/PATCH
- **Category:** Security
- **What the code does:** The WebSocket endpoint accepts connections without checking the `Origin` header. CSRF middleware only validates state-changing HTTP methods (POST/PUT/DELETE/PATCH), but WebSocket upgrades use GET.
- **What the problem is:** A malicious webpage can open a WebSocket connection to the Kern server from any origin. Since WebSocket upgrade requests carry cookies automatically, the attacker's page can:
  - Create and restore backups (with arbitrary filesystem paths)
  - Send emails via the user's configured SMTP
  - Set/clear the profile PIN (locking out or unlocking the user)
  - Ingest arbitrary files from the local filesystem
  - Execute any tool the system supports
  - Delete schedules and modify preferences

  The victim only needs to visit the attacker's webpage while the Kern server is running.
- **Recommended fix:**
  1. Validate the `Origin` header in the WebSocket handler before calling `websocket.accept()`. Reject connections from origins that don't match the expected host.
  2. Require a per-session token in the WebSocket connection URL.

---

### C-03: Command Injection via Spotify Tool

- **Location:** `app/tools/spotify.py:_open_spotify_target` (line 83)
- **Category:** Security
- **What the code does:** Passes an attacker-controlled `target_uri` string directly to `subprocess.Popen(["cmd", "/c", "start", "", target_uri])`.
- **What the problem is:** `cmd /c start` interprets shell metacharacters (`&`, `|`, `&&`, etc.) embedded in the URI string. Even though `shell=False`, `cmd.exe` itself is a shell and interprets these characters. An attacker supplying a query like `foo & calc` through the LLM tool call would cause arbitrary command execution. The `urllib.parse.quote` applied in the search case does not cover all code paths (e.g., playlist mode).
- **Recommended fix:**
  1. Use `os.startfile(target_uri)` on Windows or `webbrowser.open()` instead of `cmd /c start`.
  2. Validate that the URI strictly matches `spotify:` scheme before passing to any subprocess.
  3. Strip all shell metacharacters from the URI.

---

### C-04: CSRF Protection Fully Disableable in Production

- **Location:** `app/csrf.py:16-17` — `_is_enabled` reads `KERN_CSRF_ENABLED` env var
- **Category:** Security
- **What the code does:** CSRF protection can be completely disabled by setting `KERN_CSRF_ENABLED=false`.
- **What the problem is:** There is no guard preventing this in production or corporate mode. No audit log entry, no warning, no check against `product_posture` or `policy_mode`. When disabled, every state-changing endpoint (license import, file upload, backup create/restore, email sending) is vulnerable to cross-site request forgery from any webpage the user visits.
- **Recommended fix:**
  1. Force CSRF to be enabled when `product_posture == "production"` or `policy_mode == "corporate"`, regardless of the environment variable.
  2. Log a prominent WARNING on startup if CSRF is disabled.

---

### C-05: License Validation Fails Open — All Failure Modes Grant Full Access

- **Location:** `app/license_service.py:86-154`
- **Category:** Security
- **What the code does:** The `evaluate()` method checks license existence, signature, install binding, and expiry. Every single failure path returns `production_access=True, sample_access=True`:
  - No license file → `production_access=True`
  - Invalid JSON → `production_access=True`
  - Invalid signature → `production_access=True`
  - Wrong install ID → `production_access=True`
  - Expired (even past grace period) → `production_access=True`
- **What the problem is:** The entire license system is a no-op. It changes a status string for display but never actually restricts any functionality. An attacker can delete the license file, provide a tampered file, or use a mismatched install — full access is always granted. Additionally, `app/runtime.py:ensure_production_access` (line 415-417) unconditionally returns `True` and clears the "license_required" failure, making this a double fail-open.
- **Recommended fix:**
  1. At least one failure mode (expired, invalid, unlicensed) must set `production_access=False`.
  2. Remove `ensure_production_access()` or implement real license checking in it.
  3. If the license system is intentionally informational-only, remove the `production_access` field to avoid giving operators a false sense of enforcement.

---

### C-06: Prompt Injection in LLM Intent Fallback Allows Arbitrary Tool Execution

- **Location:** `app/cognition.py:LocalIntentFallback.match` (lines 596-626)
- **Category:** Security
- **What the code does:** Sends raw user text to a local LLM asking it to return JSON with `tool_name` and `arguments`. The returned `arguments` dict is passed directly to tool execution without validation.
- **What the problem is:** A user can craft text that manipulates the LLM into returning any `tool_name` from the available capabilities with arbitrary `arguments`. While `tool_name` is checked against `available_capabilities` (line 611), the `arguments` dict is completely unvalidated (line 622). This means an attacker can:
  - Set arbitrary file paths for ingestion/backup/restore tools
  - Compose and send emails with arbitrary content
  - Execute any available tool with crafted parameters
- **Recommended fix:**
  1. Validate the `arguments` dict against each tool's expected schema before execution.
  2. Never trust LLM-generated argument dictionaries for security-sensitive operations.
  3. Require human confirmation for tools that perform irreversible actions.

---

### C-07: `KERN_SKIP_VALIDATION` Disables All Safety Checks

- **Location:** `app/config.py:13` — `KERN_SKIP_VALIDATION` env var
- **Category:** Security
- **What the code does:** When `KERN_SKIP_VALIDATION=1`, all configuration validation is bypassed, including: `KERN_LLM_LOCAL_ONLY` enforcement, corporate policy constraints, path existence checks, and encryption mode validation.
- **What the problem is:** An attacker or misconfigured environment can silently disable every safety check. There is no audit log, no runtime warning, and no restriction on using this in production. Combined with the lack of authentication (C-01), an attacker who can set environment variables can disable all safety rails.
- **Recommended fix:**
  1. Remove `KERN_SKIP_VALIDATION` entirely, or restrict it to development-only postures.
  2. Never allow it to skip security-critical checks (`llm_local_only`, encryption mode, policy mode).
  3. Emit a loud runtime warning on every request when active.

---

## High Severity Findings

These findings represent serious vulnerabilities that require prompt remediation but may need additional conditions for exploitation.

---

### H-01: Rate Limiting Bypassed via Spoofable User-Agent Header

- **Location:** `app/rate_limit.py:59-60`
- **Category:** Security
- **What the code does:** Requests with `User-Agent` header starting with `"kern-scheduler"` bypass all rate limiting.
- **What the problem is:** Any external attacker can set `User-Agent: kern-scheduler` and completely bypass rate limiting for all endpoints, including `/upload` (normally 10 req/min). The User-Agent header is trivially spoofable by any HTTP client.
- **Recommended fix:** Remove the User-Agent-based bypass. Use a shared secret/internal token for internal requests, or only exempt requests from loopback addresses (`127.0.0.1`, `::1`).

---

### H-02: Path Traversal via WebSocket — Backup/Restore/Ingest

- **Location:** `app/ws_handlers.py:389-391` (restore), `app/ws_handlers.py:292` (backup target), `app/ws_handlers.py:644-670` (ingest)
- **Category:** Security
- **What the code does:** The WebSocket commands `create_backup`, `restore_backup`, and `ingest_files` accept arbitrary filesystem paths from the client without validation.
- **What the problem is:**
  - **Backup:** An attacker can write backup files to any filesystem location, potentially overwriting critical files.
  - **Restore:** An attacker can read backup files from any path and restore them to any directory.
  - **Ingest:** An attacker can read and index the contents of any file on the filesystem, including `/etc/shadow`, private keys, and config files with credentials.
- **Recommended fix:** For all three operations, validate that paths resolve (via `Path.resolve()`) to locations under the configured roots (`backup_root`, `document_root`). Reject any path that escapes the allowed boundary.

---

### H-03: Profile PIN Can Be Set/Cleared Without Current PIN

- **Location:** `app/ws_handlers.py:278-279` — `set_profile_pin` handler
- **Category:** Security
- **What the code does:** Allows setting or clearing the profile PIN via WebSocket without requiring the current PIN first.
- **What the problem is:** An attacker who connects to the unauthenticated WebSocket can set a new PIN (locking out the legitimate user) or clear the PIN (removing profile protection). No verification of the current PIN is required.
- **Recommended fix:** Require the current PIN to be provided and verified before allowing a PIN change. If no PIN is set, require an alternative verification.

---

### H-04: Path Traversal in Document Ingestion Tools

- **Location:** `app/tools/documents.py:IngestDocumentTool.run` (line 36), `BulkIngestTool.run` (line 246), `ImportConversationArchiveTool.run` (line 126)
- **Category:** Security
- **What the code does:** Takes file paths from LLM tool call arguments and passes them directly to `DocumentService.ingest_file()` without boundary validation.
- **What the problem is:** An LLM-generated tool call could specify `path: "/etc/passwd"` or `path: "C:\Windows\System32\config\SAM"` and the system would read and index the file contents. The path is resolved via `Path.expanduser().resolve()` but never checked against allowed directories.
- **Recommended fix:** Add path boundary validation. Restrict ingestion to an allowed set of directories (e.g., the profile's document root).

---

### H-05: Command Injection via OpenAppTool

- **Location:** `app/tools/system.py:OpenAppTool.run` (line 65)
- **Category:** Security
- **What the code does:** Passes `app_name` to `subprocess.Popen(["cmd", "/c", "start", "", app_name])` after checking against a whitelist.
- **What the problem is:** The whitelist includes `cmd` and `powershell`, which are full shells enabling arbitrary command execution. Even for non-shell apps, `cmd /c start` interprets shell metacharacters in the argument. An attacker who can influence tool arguments can achieve arbitrary command execution.
- **Recommended fix:**
  1. Remove `cmd` and `powershell` from `_DEFAULT_ALLOWED_APPS`.
  2. Use `os.startfile()` or `shutil.which()` to locate and launch executables directly.
  3. Validate `app_name` contains no shell metacharacters.

---

### H-06: SSRF via ntfy Notification Endpoint

- **Location:** `app/email_service.py:send_ntfy_notification` (line 534-535)
- **Category:** Security
- **What the code does:** Constructs a URL from `ntfy_base_url` and `ntfy_topic`, then issues an HTTP POST via `urllib.request.urlopen`.
- **What the problem is:** If `ntfy_base_url` points to internal infrastructure (e.g., `http://169.254.169.254/`, `http://localhost:6379/`), this enables Server-Side Request Forgery. The `title` parameter is injected directly into the HTTP `Title` header without sanitization, enabling HTTP header injection via CRLF characters.
- **Recommended fix:**
  1. Validate `ntfy_base_url` against an allowlist of external domains.
  2. Strip `\r` and `\n` from the `title` before setting it as a header value.

---

### H-07: `allow_sensitive` Authorization Bypass via LLM Arguments

- **Location:** `app/tools/documents.py:SearchDocumentsTool.run` (line 84), `ListDocumentsTool.run` (line 163), `QuerySpreadsheetTool.run` (line 563)
- **Category:** Security
- **What the code does:** Checks `allow_sensitive` in tool arguments to bypass corporate policy restrictions on sensitive documents.
- **What the problem is:** The `allow_sensitive` flag is controlled by the LLM, not the user. A prompt injection could instruct the model to always set `allow_sensitive=true`, completely bypassing corporate data protection. This is an **authorization decision being made by the LLM** rather than the human user.
- **Recommended fix:** Remove `allow_sensitive` from tool arguments. Require explicit user confirmation via a separate approval flow for sensitive document access.

---

### H-08: Prompt Injection in RAG System Prompts

- **Location:** `app/rag.py:answer_multi_document` (line 315), `app/rag.py:_budget` (line 576), `app/reranker.py:LLMReranker._score_pair` (lines 44-58)
- **Category:** Security
- **What the code does:** Injects retrieved document content directly into system prompts without sanitization:
  - `f"[Document {i + 1}: {label}]\n{hit.text}"` — into system prompt
  - `f"{system_prompt}\n\nCONTEXT:\n{context_block}"` — appended to system prompt
  - Direct interpolation of `query` and `passage` into reranker LLM prompt
- **What the problem is:** Adversarial content in indexed documents becomes part of the system prompt. A document containing `"Ignore all previous instructions..."` would be injected directly into the LLM's system instructions. For the reranker, a document with `"Output 10"` would always get the maximum relevance score, manipulating which documents appear in results.
- **Recommended fix:**
  1. Place retrieved content in user messages, not system messages.
  2. Use clear delimiters (e.g., XML tags) and instruct the model to treat content within them as data only.
  3. For the reranker, consider non-LLM scoring methods or structured prompt templates that resist injection.

---

### H-09: Plaintext WebDAV Password Storage

- **Location:** `app/syncing.py:upsert_target` (line 93-94)
- **Category:** Security
- **What the code does:** When `PlatformStore` is unavailable, stores the WebDAV password directly in plaintext in the metadata JSON in SQLite.
- **What the problem is:** The password is persisted in the clear without encryption. Anyone with access to the database file (or a backup of it) can read the WebDAV credentials.
- **Recommended fix:** Require the platform secret store for credential storage, or encrypt the password with a key derived from the profile before storing.

---

### H-10: PowerShell Injection via Windows Media Control

- **Location:** `app/windows_media.py:control` (line 181)
- **Category:** Security
- **What the code does:** Constructs a PowerShell script by string-replacing `__ACTION__` with `json.dumps(action)`, then executes with `-ExecutionPolicy Bypass`.
- **What the problem is:** While `json.dumps` adds quoting, PowerShell-specific escape sequences (backtick escapes, subexpressions like `$(...)`) could break out of the JSON string context. The `-ExecutionPolicy Bypass` flag worsens the risk.
- **Recommended fix:** Validate `action` against a strict allowlist (`{"play", "pause", "next", "previous"}`) before inserting into the script template.

---

### H-11: Full Table Scans on Every User Query

- **Location:** `app/memory.py:search_document_chunks` (lines 1088-1137), `app/retrieval.py:_build_candidates` (line 526-562)
- **Category:** Performance
- **What the code does:** Fetches ALL document chunks and ALL memory facts from the database on every retrieval query, then scores them in Python.
- **What the problem is:** Every user question triggers:
  1. A full table scan of `document_chunks` (no WHERE clause, no LIMIT)
  2. A full table scan of `structured_memory_items` / `memory_entries`
  3. In-memory dict construction of the entire knowledge base
  4. O(N) cosine similarity computation over all candidates

  With 10,000+ chunks, this will cause multi-second query latency and high memory usage, growing linearly with data volume.
- **Recommended fix:**
  1. Add FTS5 full-text search on `document_chunks` for pre-filtering.
  2. Use the existing sqlite-vec path instead of brute-force cosine over all embeddings.
  3. Pre-build and cache the candidate map, invalidating on writes.

---

### H-12: SQL Injection Pattern in Memory Module

- **Location:** `app/memory.py:_trim_table` (line 52), `list_document_records` (line 1017), `search_document_chunks` (line 1089)
- **Category:** Security
- **What the code does:** Builds SQL statements by interpolating table names, column names, and WHERE clause fragments via f-strings.
- **What the problem is:** While current callers use hardcoded values, this pattern is one caller change away from SQL injection. New code that passes user-influenced data for `table`, `order_column`, or filter clauses would create an exploitable vulnerability.
- **Recommended fix:** Validate all dynamically-inserted identifiers against an allowlist. Use parameterized queries for all values.

---

### H-13: CSRF Token Comparison Vulnerable to Timing Attack

- **Location:** `app/csrf.py:29`
- **Category:** Security
- **What the code does:** Compares CSRF cookie and header values with `csrf_cookie != csrf_header` (Python string `!=`).
- **What the problem is:** This is not a constant-time comparison. A timing side-channel attack could allow an attacker to brute-force the CSRF token character by character.
- **Recommended fix:** Use `hmac.compare_digest(csrf_cookie, csrf_header)` for constant-time comparison.

---

### H-14: Rate Limit Buckets Grow Without Bound (Memory Exhaustion DoS)

- **Location:** `app/rate_limit.py:38` — `_buckets` global defaultdict
- **Category:** Security / Performance
- **What the code does:** Rate limit state is stored in `defaultdict(_RateBucket)` keyed by `f"{client_ip}:{path}"` with no eviction.
- **What the problem is:** An attacker can exhaust server memory by sending requests from many distinct IPs or to many distinct URL paths. Each unique IP-path pair creates a permanent bucket entry that is never cleaned up.
- **Recommended fix:** Add a maximum bucket count with LRU eviction, or periodically prune stale buckets.

---

### H-15: Resource-Intensive GET Endpoints Not Rate Limited

- **Location:** `app/rate_limit.py:64`; `app/routes.py` — `/logs/export`, `/governance/export`, `/support/export`
- **Category:** Security
- **What the code does:** Rate limiting only applies to POST/PUT/DELETE/PATCH methods. GET requests are unlimited.
- **What the problem is:** Several GET endpoints build ZIP files, run audit chain verification, and query databases extensively. An attacker can repeatedly hammer these endpoints without rate limiting to cause denial of service.
- **Recommended fix:** Rate limit the export GET endpoints specifically, or change them to POST.

---

### H-16: Encrypted Database Persistence Not Thread-Safe

- **Location:** `app/encrypted_db.py:41-72` — `persist_encrypted`
- **Category:** Security
- **What the code does:** Uses a simple boolean `_persist_guard` flag (without locking) to guard concurrent persistence.
- **What the problem is:** The database connection is created with `check_same_thread=False`, explicitly allowing multi-threaded access. If two threads call `commit()` simultaneously, both could see `_persist_guard == False` and write to the encrypted file concurrently, corrupting it.
- **Recommended fix:** Use `threading.Lock` to protect the `persist_encrypted` method.

---

### H-17: Non-Atomic Encrypted Database Write (Data Loss Risk)

- **Location:** `app/encrypted_db.py:63`
- **Category:** Bug
- **What the code does:** Writes the encrypted payload directly to the target file via `write_text`.
- **What the problem is:** If the process crashes or loses power during `write_text`, the encrypted database file will be partially written and corrupted, causing complete data loss. The `rewrite_encrypted_database` method (line 179) correctly uses atomic rename, but `persist_encrypted` does not.
- **Recommended fix:** Use the same atomic write pattern: write to a temp file, then `temp_path.replace(encrypted_path)`.

---

### H-18: Predictable Install ID Enables License Forgery

- **Location:** `app/license_service.py:62-71`
- **Category:** Security
- **What the code does:** Generates install ID by SHA-256 hashing `COMPUTERNAME`, `USERNAME`, `system_db_path`, and `profile_root`, truncated to 16 hex chars.
- **What the problem is:** All inputs are easily discoverable. An attacker who knows the target machine can compute the install_id and forge a license file bound to that machine (if they also possess the signing key). Even without the signing key, the deterministic install_id provides no defense-in-depth.
- **Recommended fix:** Include a randomly generated secret (stored on first run) in the install_id computation.

---

## Medium Severity Findings

These findings represent significant issues that compound with other vulnerabilities or cause reliability problems under real conditions.

---

### M-01: Plaintext Database Auto-Migration Without Integrity Check

- **Location:** `app/encrypted_db.py:91-98`
- **Category:** Security
- **What the code does:** If the encrypted database file starts with `"SQLite format 3"`, it silently treats it as plaintext and migrates it.
- **What the problem is:** An attacker who can write a plaintext SQLite file to the encrypted DB path could inject arbitrary data. No integrity check or warning is raised.
- **Recommended fix:** Log a security warning when encountering a plaintext database file in Fernet encryption mode. Require explicit operator action for migration.

---

### M-02: Backup Passwords Logged in Audit Records

- **Location:** `app/ws_handlers.py:291-298` (create_backup), `app/ws_handlers.py:393-395` (restore_backup)
- **Category:** Security
- **What the code does:** Backup passwords are included in the `arguments` dict passed to `_policy_gate_dashboard_action`, which logs policy decisions and may broadcast them to all WebSocket clients.
- **Recommended fix:** Redact passwords from the arguments dict before passing to the policy gate: `{"password": "[REDACTED]"}`.

---

### M-03: Email Header Injection

- **Location:** `app/email_service.py:send_email` (lines 317-321)
- **Category:** Security
- **What the code does:** Constructs email `To`, `Cc`, and `Subject` headers directly from user-supplied `EmailDraft` fields.
- **What the problem is:** A recipient string containing `\r\nBcc: evil@attacker.com` could inject additional headers.
- **Recommended fix:** Validate all email addresses using `email.utils.parseaddr()`. Reject any address or subject containing `\r` or `\n`.

---

### M-04: Email Attachment Path Traversal (Data Exfiltration)

- **Location:** `app/email_service.py:send_email` (lines 323-332)
- **Category:** Security
- **What the code does:** Reads attachment files from paths in `EmailDraft.attachments` and attaches them to outgoing emails.
- **What the problem is:** An attacker could craft a tool call with `attachments: ["/etc/shadow"]` to exfiltrate arbitrary files via email.
- **Recommended fix:** Validate attachment paths against an allowed directory before reading.

---

### M-05: Sync Credentials Passed via LLM Tool Arguments

- **Location:** `app/tools/sync_tools.py:SyncToTargetTool.run` (lines 27-28)
- **Category:** Security
- **What the code does:** Accepts `username` and `password` as plain-text tool call arguments for sync target configuration.
- **What the problem is:** Credentials appear in conversation logs, tool call history, and audit trails.
- **Recommended fix:** Never accept credentials via tool call arguments. Use environment variables or a secrets manager.

---

### M-06: WatchFolderTool Accepts Arbitrary Paths

- **Location:** `app/tools/scheduler_tools.py:WatchFolderTool.run` (lines 185-190)
- **Category:** Security
- **What the code does:** Adds any absolute directory path to the file watcher if it exists, without boundary validation.
- **What the problem is:** An LLM could watch `C:\Windows\System32` or `/etc/`, triggering massive file events and potential ingestion of sensitive files.
- **Recommended fix:** Validate the folder path against allowed directories.

---

### M-07: RestoreBackupTool Path Traversal

- **Location:** `app/tools/system_state.py:RestoreBackupTool.run` (lines 153-174)
- **Category:** Security
- **What the code does:** Accepts `backup_path` and `restore_root` from tool arguments without path boundary validation.
- **What the problem is:** An attacker could restore a crafted backup to an arbitrary directory, overwriting system files.
- **Recommended fix:** Validate both paths against the profile's backup root.

---

### M-08: SetPreferenceTool Allows Arbitrary Key Injection

- **Location:** `app/tools/local_runtime.py:SetPreferenceTool.run` (line 37)
- **Category:** Security
- **What the code does:** Stores an arbitrary key-value pair in preferences based on LLM tool call arguments.
- **What the problem is:** An LLM could set security-sensitive preference keys like `memory_scope` or `policy_mode`.
- **Recommended fix:** Validate the `key` against an allowlist of user-settable preferences.

---

### M-09: Browser Search URL Injection

- **Location:** `app/tools/runtime_control.py:BrowserSearchTool.run` (line 38)
- **Category:** Security
- **What the code does:** Opens `https://www.google.com/search?q={query.replace(' ', '+')}` — only spaces are replaced, not other URL-special characters.
- **What the problem is:** Characters like `&`, `#`, `=` pass through verbatim, allowing URL structure manipulation.
- **Recommended fix:** Use `urllib.parse.quote_plus(query)`.

---

### M-10: OpenWebsiteTool Lacks URL Validation

- **Location:** `app/tools/system.py:OpenWebsiteTool.run` (lines 110-113)
- **Category:** Security
- **What the code does:** Opens any URL in the default browser, auto-prepending `https://` if no scheme is present.
- **What the problem is:** Internal network addresses (`http://169.254.169.254/`) pass validation. An LLM prompt injection could direct the browser to phishing sites.
- **Recommended fix:** Parse with `urllib.parse.urlparse()`, validate scheme is `http`/`https`, block private IP ranges.

---

### M-11: Clipboard Contents Exposed to LLM

- **Location:** `app/current_context.py:WindowsClipboardClient.read_text` (lines 61-81)
- **Category:** Security
- **What the code does:** Reads clipboard contents via PowerShell and includes them in the context summary sent to LLM prompts.
- **What the problem is:** Passwords, API keys, or other sensitive data on the clipboard would be sent to the LLM.
- **Recommended fix:** Add sanitization to detect and redact potential secrets. Consider making clipboard reading opt-in.

---

### M-12: X-Request-ID Header Injection (Log Injection)

- **Location:** `app/tracing.py:dispatch` (line 33)
- **Category:** Security
- **What the code does:** Accepts and propagates the `X-Request-ID` header from incoming requests without validation.
- **What the problem is:** Arbitrary strings (including CRLF, ANSI escape codes, extremely long strings) can be injected as request IDs, enabling log injection attacks.
- **Recommended fix:** Validate against `^[a-zA-Z0-9_-]{1,64}$` and generate a new ID if validation fails.

---

### M-13: Backup Creation Follows Symlinks

- **Location:** `app/backup.py:_zip_directory` (line 240-243)
- **Category:** Security
- **What the code does:** Archives all files under a profile root via `root.rglob("*")`, which follows symbolic links.
- **What the problem is:** A symlink inside the profile root could cause the backup to include arbitrary files from elsewhere on the filesystem. The restore path checks for symlinks (line 175), but the creation path does not.
- **Recommended fix:** Add `if file_path.is_symlink(): continue` before `archive.write(...)`.

---

### M-14: Retention Service Deletes Paths Without Boundary Check

- **Location:** `app/retention.py:_delete_path` (line 250-266)
- **Category:** Security
- **What the code does:** Deletes files at paths from database records without validating they're within expected directories.
- **What the problem is:** If the database is corrupted to contain a path like `/etc/passwd` or `C:\Windows\System32\important.dll`, the retention service will attempt to delete it.
- **Recommended fix:** Validate resolved paths are under expected profile roots before deletion.

---

### M-15: Timezone Mismatch — Naive vs. Aware Datetimes

- **Location:** `app/attention.py:30,329` — `app/local_data.py:44,105,334` — `app/reminders.py:22,25` — `app/german_business.py:74,166,201` — `app/types.py:104,133,215` (and many more)
- **Category:** Bug
- **What the code does:** Uses `datetime.now()` (naive, no timezone) throughout, while other parts store and parse timezone-aware datetimes.
- **What the problem is:** Comparing naive and aware datetimes raises `TypeError` in Python. This will cause runtime crashes in calendar watchers, reminder scheduling, and business document date calculations. `datetime.utcnow()` is also used (deprecated since Python 3.12) and returns naive datetimes.
- **Recommended fix:** Globally replace `datetime.now()` with `datetime.now(timezone.utc)` and `datetime.utcnow()` with `datetime.now(timezone.utc)`.

---

### M-16: `create_local_event` Defined Three Times

- **Location:** `app/memory.py:470-485`, `495-515`, `935`
- **Category:** Bug
- **What the code does:** The method `create_local_event` is defined three times with identical signatures.
- **What the problem is:** In Python, each redefinition silently shadows the previous one. Only the last definition is used. This is dead code and a maintenance hazard.
- **Recommended fix:** Remove the duplicate definitions.

---

### M-17: LLM Health Check Treats 404 as Healthy

- **Location:** `app/llm_client.py:health` (line 47)
- **Category:** Bug
- **What the code does:** Returns `True` if a health check returns HTTP 404.
- **What the problem is:** 404 means the endpoint doesn't exist — this is not a healthy state. A misconfigured server will be reported as healthy, then fail on actual inference calls. This is a fail-open pattern.
- **Recommended fix:** Only treat HTTP 200 as healthy.

---

### M-18: LLM Stream Errors Silently Swallowed

- **Location:** `app/llm_client.py:chat_stream` (lines 56-81)
- **Category:** Bug
- **What the code does:** Catches `httpx.HTTPError` and `httpx.StreamError` and silently returns without yielding any tokens.
- **What the problem is:** The caller cannot distinguish between "LLM returned no content" and "the connection failed." Errors are completely invisible.
- **Recommended fix:** Log the exception and yield a sentinel/error indicator so the caller knows the stream failed.

---

### M-19: RAG `answer()` Wastes LLM Call Before Extractive Check

- **Location:** `app/rag.py:answer` (line 258-288)
- **Category:** Bug / Performance
- **What the code does:** Calls the LLM for a full inference, then checks for an extractive answer and returns that instead, discarding the LLM result.
- **What the problem is:** The full cost of an LLM call (latency + tokens) is paid and then thrown away. The `answer_stream()` method correctly checks the extractive path first.
- **Recommended fix:** Move `_extractive_explicit_document_answer` check before the LLM call, matching `answer_stream()` behavior.

---

### M-20: RAG Errors Swallowed at DEBUG Level

- **Location:** `app/llm.py:generate_rag_reply` (line 174)
- **Category:** Bug
- **What the code does:** Catches all exceptions from `self._rag.answer()`, logs at DEBUG level, and returns `(None, [])`.
- **What the problem is:** Configuration errors, network failures, and bugs are all silently swallowed. In production, operators will see silent answer quality degradation with no log evidence.
- **Recommended fix:** Log at WARNING level and differentiate expected failures from unexpected errors.

---

### M-21: TTS Worker Thread Data Race

- **Location:** `app/tts.py:TTSService._run` (line 201-223)
- **Category:** Bug
- **What the code does:** The background worker thread reads/writes `self.enabled`, `self._active_adapter`, `self.backend_name`, and `self.status` without synchronization.
- **What the problem is:** `set_enabled()` modifies these attributes from the main thread while `_run()` reads them from the worker thread. Partially-updated state can be observed.
- **Recommended fix:** Add a `threading.Lock` to protect shared mutable state.

---

### M-22: Scheduler Task Execution Race (TOCTOU)

- **Location:** `app/scheduler.py:tick` (lines 196-227)
- **Category:** Bug
- **What the code does:** Fetches due tasks with SELECT, then marks them as "running" with UPDATE in a loop.
- **What the problem is:** Between SELECT and UPDATE, another process/thread could mark the same task as running. This TOCTOU race can cause duplicate task execution.
- **Recommended fix:** Use `UPDATE ... WHERE run_status != 'running' RETURNING *` (optimistic locking) and check `rowcount`.

---

### M-23: Cron Day-of-Week Wrap-Around Bug

- **Location:** `app/scheduler.py:_expand_cron_field` (line 57)
- **Category:** Bug
- **What the code does:** After `_normalize_dow`, a range like `6-7` becomes `6-0`. The check `start > end` rejects this.
- **What the problem is:** "Saturday through Sunday" (`6-7` in cron) is a valid wrap-around range but is rejected as invalid.
- **Recommended fix:** Handle wrap-around ranges when `normalize_dow` is True.

---

### M-24: Excel Parsing Vulnerable to XML Bombs

- **Location:** `app/spreadsheet.py:parse_excel` (line 33)
- **Category:** Security
- **What the code does:** Opens Excel files with `openpyxl.load_workbook(path, data_only=True)` without file size or content validation.
- **What the problem is:** A crafted XLSX with a "billion laughs" XML entity expansion attack could cause memory exhaustion. No file size limit is enforced before parsing.
- **Recommended fix:** Validate file size before parsing. Add a max row/column limit. Consider using `defusedxml`.

---

### M-25: Knowledge Graph Entity Dedup is O(N) per Upsert

- **Location:** `app/knowledge_graph.py:upsert_entity` (lines 100-161)
- **Category:** Performance
- **What the code does:** For every entity upsert, fetches ALL entities of that type and performs two O(N) passes (exact + fuzzy match).
- **What the problem is:** Processing a document with many entities causes quadratic behavior. Each sentence may produce multiple entities, each triggering a full table scan + Levenshtein distance computation.
- **Recommended fix:** Use database-level indexing on canonical names. Cache recent lookups.

---

### M-26: Knowledge Graph Search Loads Entire Entity Table

- **Location:** `app/knowledge_graph.py:search_entities` (line 328-336)
- **Category:** Performance
- **What the code does:** Fetches ALL entities for a profile to perform in-memory string matching.
- **What the problem is:** No pagination, no LIMIT, no database-level filtering. O(N) for every search.
- **Recommended fix:** Add SQL `LIKE` pre-filter or FTS.

---

### M-27: `_try_uploaded_document_answer` Loads All Chunks

- **Location:** `app/orchestrator.py:_try_uploaded_document_answer` (lines 1081-1115)
- **Category:** Performance
- **What the code does:** Calls `list_all_document_chunks(include_archived=True)` to load the entire chunk table, then filters in Python for ~8 recent documents.
- **What the problem is:** Fetches potentially thousands of chunks to filter for a small set.
- **Recommended fix:** Add `list_document_chunks_by_ids(document_ids)` with a SQL WHERE clause.

---

### M-28: OCR Backend Construction Race Condition

- **Location:** `app/ocr.py:get_ocr_backend` (line 64-69)
- **Category:** Bug
- **What the code does:** Uses `@lru_cache(maxsize=4)` to cache OCR backend instances.
- **What the problem is:** `lru_cache` is not thread-safe for the creation path. Two concurrent calls could construct heavy ML models twice.
- **Recommended fix:** Add a threading lock around cache creation.

---

### M-29: Event Queue Silently Drops Non-Snapshot Events

- **Location:** `app/events.py:_drop_stale_item` (line 50-68)
- **Category:** Bug
- **What the code does:** Drains the queue to evict one stale snapshot, then re-enqueues remaining items.
- **What the problem is:** If the queue fills during re-enqueuing, non-snapshot events are silently dropped via `break` without incrementing any counter or logging.
- **Recommended fix:** Log dropped items. Consider a more efficient eviction strategy.

---

### M-30: WebDAV Upload Loads Entire File Into Memory

- **Location:** `app/syncing.py:upload_webdav` (line 305)
- **Category:** Performance
- **What the code does:** Calls `source_path.read_bytes()` to load the entire backup file into memory for HTTP PUT.
- **What the problem is:** Multi-GB backup files will cause out-of-memory crashes.
- **Recommended fix:** Stream the file upload instead of reading entirely.

---

### M-31: `_delete_indexed_attachment` May Use Wrong DB Connection

- **Location:** `app/email_service.py:_delete_indexed_attachment` (line 795)
- **Category:** Bug
- **What the code does:** Deletes document records using `self.connection` instead of `self.documents.memory.connection`.
- **What the problem is:** If these are different connection objects, the delete operates on the wrong database.
- **Recommended fix:** Use `self.documents.memory.connection` or delegate to `DocumentService`.

---

### M-32: Validation Pack Auto-Installs Unverified npm Packages

- **Location:** `app/validation_pack.py:PlaywrightCliSession._cmd` (line 127-146)
- **Category:** Security
- **What the code does:** Runs `npx --yes @playwright/cli` which auto-confirms package installation.
- **What the problem is:** The `--yes` flag bypasses confirmation. If the npm registry is compromised or a typo-squatting attack targets `@playwright/cli`, malicious code would be silently installed and executed.
- **Recommended fix:** Pin the exact version and verify integrity, or remove `--yes`.

---

### M-33: `current_context.py` Crashes on Non-Windows

- **Location:** `app/current_context.py` (lines 16-18)
- **Category:** Bug
- **What the code does:** Tries to access `ctypes.windll.user32` at module import time.
- **What the problem is:** `ctypes.windll` doesn't exist on Linux/macOS. The ternary guard evaluates too late — `ctypes.windll` itself raises `AttributeError` before the condition is checked.
- **Recommended fix:** Wrap in `try/except AttributeError` or `hasattr(ctypes, 'windll')`.

---

### M-34: Policy Confirmation Auto-Approved in Personal Mode

- **Location:** `app/routes.py:108` — `_http_policy_gate`; `app/policy.py:65-160`
- **Category:** Security
- **What the code does:** In personal mode, all `"confirm"` policy verdicts are automatically approved without user interaction.
- **What the problem is:** Tools with `confirmation_rule="always"` are never confirmed in personal mode. The policy engine's confirmation mechanism is effectively disabled for the majority of operations.
- **Recommended fix:** Even in personal mode, `"always"` confirmation rules should still require confirmation.

---

### M-35: Middleware Ordering Allows CSRF-Blocked Requests to Consume Rate Limit

- **Location:** `app/main.py:27-36`
- **Category:** Architecture
- **What the code does:** Middleware is added in order: RequestTracingMiddleware, CSRFMiddleware, RateLimitMiddleware. Due to Starlette's LIFO stack, execution order is: RateLimit → CSRF → Tracing.
- **What the problem is:** Rate limiting runs before CSRF validation. An attacker's CSRF-failing requests still consume the legitimate user's rate limit slots.
- **Recommended fix:** Add CSRF middleware last so it runs first: rate limiting should only count requests that pass CSRF validation.

---

### M-36: `db_encryption_mode` Accepts Arbitrary Values When Validation Skipped

- **Location:** `app/config.py:109`
- **Category:** Security
- **What the code does:** `_normalize_db_encryption_mode` normalizes `"none"` to `"off"` but passes through any other string as-is.
- **What the problem is:** With `KERN_SKIP_VALIDATION=1`, a value like `KERN_DB_ENCRYPTION_MODE=aes256` would be accepted. Since `encryption_mode != "off"` evaluates to `True`, the Fernet encryption path would be taken, but the user would believe they have AES-256.
- **Recommended fix:** Reject values not in `{"fernet", "off", "none"}` and raise an error.

---

### M-37: `Fernet.generate_key()` Used as PBKDF2 Salt

- **Location:** `app/backup.py:_derive_key` (line 302-312)
- **Category:** Security
- **What the code does:** Uses `Fernet.generate_key()` (a 44-char base64 string) as the "salt" for PBKDF2 key derivation.
- **What the problem is:** While functionally random, this misuses the Fernet abstraction. The salt is then base64-encoded again when stored, creating unnecessary confusion. The semantic mismatch suggests a misunderstanding of the cryptographic primitive.
- **Recommended fix:** Use `os.urandom(16)` for the salt.

---

### M-38: Recursive Archive Flattening Can Stack Overflow

- **Location:** `app/documents.py:_flatten_archive_payload` (line 844)
- **Category:** Security / Bug
- **What the code does:** Recursively flattens a JSON archive payload with no depth limit.
- **What the problem is:** A deeply nested payload (10,000+ levels) causes `RecursionError`.
- **Recommended fix:** Add a `max_depth` parameter or rewrite iteratively.

---

### M-39: LLM Reranker Fires Unbounded Concurrent Requests

- **Location:** `app/reranker.py:LLMReranker.rerank` (lines 25-42)
- **Category:** Performance
- **What the code does:** Creates an async task for each retrieval hit (up to `top_k=12`) for parallel LLM scoring.
- **What the problem is:** Against a local llama-server supporting only 1 concurrent request, this creates 12 HTTP connections competing for one slot. Against a rate-limited API, this triggers throttling.
- **Recommended fix:** Add `asyncio.Semaphore` for concurrency limiting.

---

## Low Severity Findings

These are defense-in-depth gaps, minor bugs, and code quality issues that don't directly enable exploitation but reduce the system's resilience.

---

### L-01: Config Summary Exposes Internal Paths in Support Bundle
**Location:** `app/routes.py:308-324`
The support bundle includes `llama_server_url`, `llama_server_model_path`, `profile_root`, `backup_root`, and `license_root`. If shared externally, these leak internal infrastructure details. **Fix:** Redact full paths.

### L-02: Exception Details Leaked to WebSocket Clients
**Location:** `app/ws_handlers.py:885`
`response_text = f"{type(exc).__name__}: {exc}"` is broadcast to all clients, leaking internal error types, file paths, and database messages. **Fix:** Send generic errors to clients; log details server-side.

### L-03: Health Endpoint Leaks Database Error Messages
**Location:** `app/routes.py:147-148`
Database error messages in `/health` response can leak paths and internal state to unauthenticated callers. **Fix:** Return generic `"error"` status.

### L-04: Email Password Stored in Memory for Process Lifetime
**Location:** `app/config.py:117`
`KERN_EMAIL_PASSWORD` is stored in the `Settings` dataclass for the entire process lifetime. Any memory dump or debug serialization would expose it. **Fix:** Read on demand or use SecretStr.

### L-05: `_get_runtime` Unguarded Against None Call
**Location:** `app/routes.py:77`
If any route is called before `register_routes()`, `_get_runtime()` raises unhelpful `TypeError: 'NoneType' is not callable`. **Fix:** Add a guard with a descriptive error.

### L-06: `_enum` Helper Silently Falls Back on Typos
**Location:** `app/config.py:73`
Typos in `KERN_PRODUCT_POSTURE`, `KERN_LOG_FORMAT`, `KERN_OCR_ENGINE` are silently normalized to defaults without warning. **Fix:** Log a warning when the value doesn't match.

### L-07: `policy_mode` Not Normalized via `_enum`
**Location:** `app/config_validation.py:99` vs `app/config.py:105`
`product_posture` uses `_enum` for early normalization, but `policy_mode` does not. Arbitrary strings are accepted for `policy_mode`. **Fix:** Use `_enum` for `policy_mode`.

### L-08: `UPLOAD_MAX_FILE_MB` Crashes on Non-Integer Env Var
**Location:** `app/routes.py:29`
`int(os.environ.get(...))` without try/except crashes at import time on non-integer values. **Fix:** Add try/except with default.

### L-09: WebSocket Connection Count Race Condition
**Location:** `app/ws_handlers.py:117-119`
`_ws_connection_count` incremented/decremented without synchronization. Only used for metrics. **Fix:** Use `asyncio.Lock` or atomic counter.

### L-10: No Input Length Validation on WebSocket `submit_text`
**Location:** `app/ws_handlers.py:158-160`
No maximum length check on submitted text. Multi-GB payloads could exhaust memory. **Fix:** Add max length check.

### L-11: Verification Tool Probes Arbitrary File Paths
**Location:** `app/verification.py:171-178`
`file_path` from tool arguments is used without path restriction to check file existence and size. **Fix:** Restrict to expected directories.

### L-12: Metrics Counter Read Not Lock-Protected
**Location:** `app/metrics.py:counter_value` (line 31)
Reads counter without acquiring `self._lock`, while `inc` uses the lock. Minor data race. **Fix:** Wrap read in `with self._lock`.

### L-13: Metrics Histogram Uses Expensive List Slice
**Location:** `app/metrics.py:observe` (line 37-41)
`bucket[:] = bucket[-1000:]` creates a copy on every call after 1000 observations. **Fix:** Use `collections.deque(maxlen=1000)`.

### L-14: Network Monitor Returns Status Outside Lock
**Location:** `app/network_monitor.py:check` (line 107)
`return self._status` is outside the `with self._lock` block, unlike the `status` property. **Fix:** Return within the lock.

### L-15: FileWatcher `_known` Dict Grows Without Bound
**Location:** `app/attention.py:FileWatcher._known` (line 92)
Tracks every file path ever seen with no eviction. Deleted files remain forever. **Fix:** Periodically reconcile against filesystem.

### L-16: CalendarWatcher/DocumentWatcher Alert Sets Grow Forever
**Location:** `app/attention.py:326,375`
`_alerted_keys` and `_alerted_ids` accumulate indefinitely. **Fix:** Use LRU-bounded set.

### L-17: Embedding Batch Processes Sequentially
**Location:** `app/embeddings.py:embed_batch` (lines 53-59)
Calls `self.embed(text)` in a loop instead of batching. **Fix:** Use batch embedding if the model supports it.

### L-18: `assert` Used for Runtime Validation
**Location:** `app/planning.py:_single_step_plan` (line 88)
`assert parsed.tool_request is not None` — removed with `-O` flag. **Fix:** Use `if ... raise ValueError`.

### L-19: `_preserve_single_hit` Ignores `min_score`
**Location:** `app/rag.py:_preserve_single_hit` (lines 536-542)
Preserves a single hit if score > 0, ignoring `min_score`. A hit with 0.001 passes even if `min_score` is 0.5. **Fix:** Check against `min_score * 0.5`.

### L-20: Cache Key Hit Never Returned
**Location:** `app/model_router.py:cache_lookup` (lines 208-229)
Creates a copy of `route` with `cache_hit=True` via `model_copy`, but the copy is never returned. The mutation is lost. **Fix:** Return the updated route.

### L-21: SHA-1 Used for Content IDs
**Location:** `app/retrieval.py:_build_candidates` (line 532)
SHA-1 is cryptographically broken. SHA-256 is used elsewhere. **Fix:** Use SHA-256 consistently.

### L-22: Cosine Similarity Silently Truncates Mismatched Vectors
**Location:** `app/cognition.py:CapabilityClassifierMatcher._cosine` (line 500-503)
`zip` silently truncates to the shorter list if vector lengths differ. **Fix:** Assert `len(left) == len(right)`.

### L-23: Intent Parser Operator Precedence Bug
**Location:** `app/intent.py:parse` (line 948)
`lowered.startswith("play ") or lowered.startswith("put on ") or lowered.startswith("start ") and ("music" in lowered ...)` — due to `and` binding tighter than `or`, "play some tennis" matches as media intent. **Fix:** Add parentheses.

### L-24: Hardcoded English Reply Ignores Preferred Title
**Location:** `app/orchestrator.py:process_transcript` (line 498-506)
Returns `"One moment, sir."` ignoring the user's configured `preferred_title`. **Fix:** Use `self.local_data.preferred_title()`.

### L-25: 12-Second Hardcoded Tool Execution Timeout
**Location:** `app/orchestrator.py:_execute_plan` (line 836)
Some tools (bulk ingestion, meeting recording, knowledge graph building) need much longer. **Fix:** Per-tool timeout configuration.

### L-26: Orchestrator Violates Brain Encapsulation
**Location:** `app/orchestrator.py:__init__` (line 274)
Reaches into `self.brain._cognition.server_planner` (private attribute). **Fix:** Add public setter on `Brain`.

### L-27: Tool Schema Cache Never Auto-Invalidated
**Location:** `app/tool_calling.py:build_tool_schemas` (lines 17-38)
Cached schemas become stale if tool availability changes. No auto-invalidation. **Fix:** Invalidate on capability changes.

### L-28: `import json` Inside Loop Body
**Location:** `app/tool_calling.py:parse_tool_calls` (line 56)
Minor overhead from repeated module lookup. **Fix:** Move to top of file.

### L-29: `psutil.cpu_percent(interval=0.0)` Returns 0 on First Call
**Location:** `app/tools/runtime_control.py:SystemStatusTool.run` (line 98)
Returns `0.0` on first call because no baseline exists. **Fix:** Use `interval=0.1`.

### L-30: Duplicate `_resolve_document_by_name` Methods
**Location:** `app/tools/documents.py` (lines 370-408 and 462-485)
Identical methods in `CompareDocumentsTool` and `SummarizeDocumentTool`. **Fix:** Extract to shared utility.

### L-31: Document Name Regex Preserves Backslashes
**Location:** `app/tools/documents.py` lines 407, 484
`re.sub(r"[^0-9a-z_\\-]+", ...)` includes literal backslash in allowed set. **Fix:** Change to `[^0-9a-z_-]+`.

### L-32: Tool Integer Arguments Not Validated
**Location:** `app/tools/email_tools.py:ReadEmailTool.run` (line 20) and multiple other tools
`int(request.arguments.get("limit", 5))` raises `ValueError` on non-numeric input. **Fix:** Add try/except and clamp to reasonable maximum.

### L-33: SummarizeDocumentTool Uses Private `_extract_text`
**Location:** `app/tools/documents.py:SummarizeDocumentTool.run` (line 445)
Calls `self.service._extract_text()` (private method). **Fix:** Add public API.

### L-34: `GermanBusinessDocument.kind` Missing `"gewerbeanmeldung"`
**Location:** `app/types.py:444-451` vs `app/german_business.py:529`
`create_gewerbeanmeldung` inserts `kind='gewerbeanmeldung'` which is not in the Literal type union. Pydantic would reject it. **Fix:** Add to Literal type.

### L-35: Knowledge Graph `commit()` Inside Nested Transaction
**Location:** `app/knowledge_graph.py:extract_from_text` (line 257)
`commit()` inside a method called from larger transactions may commit partial work. **Fix:** Let caller manage commits.

### L-36: Operator Precedence Bug in `_contextual_reminder`
**Location:** `app/action_planner.py:_contextual_reminder` (line 538)
`ctx["subject"] or ctx["references"][0] if ctx["references"] else "Nachfassen"` — precedence may not match intent. **Fix:** Add parentheses.

### L-37: Notification Service `_sent_event_keys` Grows Forever
**Location:** `app/reminders.py:NotificationService` (line 48-62)
Set accumulates without cleanup. **Fix:** Bound size or prune periodically.

### L-38: Custom `contextlib.suppress` Reimplemented
**Location:** `app/backup.py:315-323`
Reimplements standard library `contextlib.suppress`. **Fix:** Use `from contextlib import suppress`.

### L-39: Backup Payload Stored as JSON (1.37x Size Inflation)
**Location:** `app/backup.py:create_encrypted_profile_backup` (line 48-58)
Ciphertext stored as base64 inside JSON text. Multi-GB profiles will exhaust memory. **Fix:** Stream encrypted output to binary file.

### L-40: `_resolve_account` Returns Non-SMTP Account When SMTP Required
**Location:** `app/email_service.py:_resolve_account` (line 601)
Falls back to `accounts[0]` even when `require_smtp=True` and no account has SMTP. **Fix:** Return `None`.

### L-41: Duplicate Scheduler `_get()` Calls in Availability Check
**Location:** `app/tools/scheduler_tools.py` lines 83-84, 111-112, 161-162
`_get()` called twice in `availability()`. **Fix:** Store result in local variable.

### L-42: TaxSupportTool Calls Service Twice
**Location:** `app/tools/german_business.py:TaxSupportTool.run` (lines 116-117)
Calls `tax_support_result(query)` and `tax_support(query)` separately. **Fix:** Single call returning both.

### L-43: OpenAI API Key Used in "Local-First" System
**Location:** `app/runtime.py:__init__` (line 99)
`settings.openai_api_key` passed to `Brain` constructor in a system claiming no cloud connections. **Fix:** Refuse cloud API keys when `local_mode_enabled` is True.

---

## Top 3 Highest-Priority Fixes

These are the changes that would prevent the most damage if left unaddressed:

### 1. Implement Authentication (C-01 + C-02)
**Impact:** Without authentication, every other security control is moot. Any attacker who can reach the server has full control — backup/restore arbitrary files, send emails, read documents, execute commands. The WebSocket endpoint is the most dangerous because it accepts every operation and has no CSRF, no Origin check, and no auth.

**Action:** Add authentication middleware for all non-health endpoints. Validate Origin on WebSocket connections. Enforce `KERN_ADMIN_AUTH_TOKEN` if it's set.

### 2. Add Path Boundary Validation Everywhere (H-02, H-04, M-04, M-06, M-07, M-14)
**Impact:** At least 6 different code paths accept arbitrary filesystem paths from untrusted sources (WebSocket clients, LLM tool arguments). These enable reading sensitive files (passwords, keys, configs), writing to arbitrary locations, and deleting files outside expected directories.

**Action:** Create a shared `validate_path_boundary(path, allowed_roots)` utility. Apply it to every operation that touches the filesystem based on user/LLM input.

### 3. Eliminate Command Injection Vectors (C-03, H-05, H-10)
**Impact:** Three different code paths pass user-influenced strings through `cmd /c start` or PowerShell with `-ExecutionPolicy Bypass`. These enable arbitrary command execution on the host OS.

**Action:** Replace `cmd /c start` with `os.startfile()` or `webbrowser.open()`. Validate all shell-bound inputs against strict allowlists. Never pass user-controlled strings through a shell interpreter.

---

## Systemic Observation

**The Trust Boundary Between LLM and System Is Missing.**

The codebase treats LLM-generated tool call arguments with the same trust level as user input — but it should treat them with *less* trust. Multiple tools accept `file_path`, `backup_path`, `folder_path`, `allow_sensitive`, `username`, `password`, `app_name`, `url`, `query`, and `action` directly from LLM arguments and pass them to filesystem operations, subprocess calls, network requests, and authorization decisions without validation.

This creates a **prompt injection → system compromise** pipeline:

1. An adversarial document is ingested (or a user is socially engineered to ask a crafted question)
2. The LLM's response includes a tool call with malicious arguments
3. The tool executes those arguments against the real system

The fix is architectural: **every tool must validate its arguments against a schema and enforce path/value boundaries independently of the LLM.** Authorization decisions (like `allow_sensitive`) must never come from LLM arguments — they must come from the human user through a separate confirmation channel.

This single pattern generates at least 15 of the findings in this report.

---

---

## Deployment Readiness Assessment — German Corporate Context

> **Context:** Kern is intended as a locally-deployed AI assistant for German corporate firms, handling business documents (Angebote, Rechnungen, Verträge, HR records, tax documents). This section evaluates deployment readiness beyond the technical findings above — covering legal compliance, architecture gaps, operational requirements, and enterprise readiness.

---

### Overall Readiness Verdict

| Layer | Status | Blocker? |
|-------|--------|----------|
| Core AI functionality | Ready | No |
| Technical security (post-fix) | Ready | No |
| Authentication & access control | **Not built** | **Yes** |
| Multi-user / multi-tenant | **Not built** | **Yes** |
| GDPR compliance | **Not built** | **Yes** |
| GoBD compliance | **Unclear** | **Yes** |
| Data residency enforcement | **Partial** | **Yes** |
| Audit trail integrity | **Incomplete** | **Yes** |
| Professional penetration test | **Not done** | **Yes** |
| Disaster recovery | Partial | Conditional |
| IT security policy alignment | **Not assessed** | **Yes** |

**Conclusion: Not ready for German corporate deployment, even after fixing all 110 technical findings.** The missing items are not bugs — they are architectural features and legal obligations that do not exist yet.

---

### 1. Multi-User Architecture — Not Built

**What exists:** The system is built around a single `profile` with a single user context, a single PIN, a single set of documents, and a single LLM session.

**What corporate deployment requires:**
- Each employee must have isolated access to their own documents
- A manager must not be able to access an employee's personal HR documents
- An admin must be able to provision, suspend, and audit user accounts
- Session isolation must be enforced at the database level, not the application level

**What this means technically:**
- Every database table needs a `user_id` or `tenant_id` foreign key with row-level security
- Every API endpoint and WebSocket command must enforce ownership checks
- The profile system must be redesigned from "single profile per installation" to "multiple isolated profiles per installation with access controls"

**Reference:** ISO/IEC 27001:2022 — Access Control (Clause 8.3). Most German corporate IT departments require ISO 27001 certification or alignment from vendors.

---

### 2. GDPR Compliance — Not Built

Kern processes personal data in multiple ways: employee names in documents, email addresses, meeting attendee lists, calendar entries, and potentially HR records. This makes it a **data processor** under GDPR (EU Regulation 2016/679), and the corporate customer is the **data controller**.

**Specific gaps:**

**Article 5 — Data minimization and purpose limitation**
The system collects clipboard content, calendar events, email metadata, browser context, and active window titles. There is no mechanism to limit collection to what is strictly necessary for the stated purpose, nor to inform users what is being collected.

**Article 13/14 — Transparency**
Users must be informed what personal data is processed, for how long, and for what purpose. There is currently no privacy notice, no data processing registry, and no per-user transparency mechanism.

**Article 17 — Right to erasure ("Right to be Forgotten")**
There is a `retention.py` module and an `uninstall-data-deletion` guide, but no documented, tested, complete erasure procedure that covers all data stores (SQLite databases, encrypted backups, cached embeddings, knowledge graph entries, audit logs). A user exercising their Article 17 right requires a verifiable, complete deletion.

**Article 20 — Data portability**
Users must be able to export their personal data in a machine-readable format. There is no dedicated export-my-data feature.

**Article 25 — Privacy by design and by default**
Sensitive document access is opt-in via `allow_sensitive` flag (which can be bypassed via LLM arguments — see H-07). Data minimization is not enforced by default. Privacy-by-design requires that the most privacy-preserving option is the default, not something to opt into.

**Article 28 — Data Processing Agreement (DPA)**
If Kern is sold to German companies, a formal DPA (Auftragsverarbeitungsvertrag) must be established between Kern's operator and each corporate customer. This is a legal document, not a code change, but the product must be able to technically fulfill the obligations within it.

**Article 32 — Security of processing**
Requires "appropriate technical and organisational measures." The current state (no auth, path traversal, command injection) does not meet this standard. Post-fix, this needs to be documented and demonstrable.

**Article 35 — Data Protection Impact Assessment (DPIA)**
Because Kern processes business documents that may contain personal data at scale using AI, a DPIA is likely mandatory before deployment. This is a formal risk assessment document, not a code artifact, but the product must provide the technical inputs for it.

**Relevant authority:** Bundesbeauftragte für den Datenschutz und die Informationsfreiheit (BfDI) — Germany's federal data protection authority. German Landesdatenschutzbehörden (state-level DPAs) also have jurisdiction depending on the customer's federal state.

---

### 3. GoBD Compliance — Unclear / Likely Incomplete

**GoBD** (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form) are the German tax authority (BMF) requirements for electronic bookkeeping records. If Kern processes invoices, offers (Angebote), or any tax-relevant documents, GoBD applies.

**Key GoBD requirements and current gaps:**

**Unchangeability (Unveränderbarkeit)**
Once a business document is stored, it must not be modifiable. Currently, documents can be deleted, re-indexed, and modified. There is no immutability mechanism. GoBD requires that any change creates a versioned audit trail that cannot be retroactively altered.

**Completeness (Vollständigkeit)**
All business-relevant documents must be captured without exception. The current system ingests documents on demand — there is no mechanism to guarantee completeness of capture.

**Traceability (Nachvollziehbarkeit)**
Every processing step must be traceable. The AI's interaction with a document (summarization, extraction, classification) must be logged with enough detail to reconstruct what happened and why. The current audit log is a good start but does not record which specific document version was processed by which model with which prompt.

**Retention periods (Aufbewahrungsfristen)**
- Commercial documents (Handelsbriefe, Rechnungen): 6 years (§ 257 HGB)
- Tax-relevant documents: 10 years (§ 147 AO)

The current `retention.py` implements configurable retention but does not enforce GoBD-mandated minimums. A configuration error could delete documents before the legal minimum is met.

**Audit-proof archiving (revisionssichere Archivierung)**
GoBD requires that archived documents are stored in a way that makes unauthorized modification detectable. The current encrypted backup system provides confidentiality but not integrity proof — there is no cryptographic hash chain or digital signature that would allow a tax auditor to verify documents were not altered.

**Reference:** BMF-Schreiben vom 28. November 2019 — the binding guidance document for GoBD. Available at: bundesfinanzministerium.de

---

### 4. Audit Trail Integrity — Incomplete

The codebase has audit logging in `platform.py` (audit events, governance export) and a `/governance/export` endpoint. However:

**What is missing:**

- **Hash chaining:** Each audit record should include a cryptographic hash of the previous record, creating a tamper-evident chain. Currently, records can be deleted or modified without detection.
- **External anchoring:** For legally defensible audit trails, records should be periodically anchored to an external system (e.g., a timestamping authority per RFC 3161) so that even the system operator cannot retroactively alter history.
- **Model decision logging:** When the AI classifies a document, routes a query, or makes a policy decision, the specific model, version, prompt template, and input hash must be logged. This is required to investigate disputes.
- **Non-repudiation:** Actions that have legal consequences (sending an email, exporting data, deleting a document) must be logged with enough detail that the action cannot be denied.

**Reference:** BSI TR-03125 (TrustEd Desktop) and BSI TR-03138 (RESISCAN) — German BSI technical guidelines for trustworthy electronic records.

---

### 5. Data Residency Enforcement — Partial

The product claims local-first operation, but there is no technical enforcement of this:

- `settings.openai_api_key` is present in the config — an OpenAI API key could be set and data would silently leave the corporate network.
- The LLM endpoint URL is configurable — there is no validation that it resolves to a loopback or internal address.
- Email integration sends data to SMTP servers — no enforcement that these are internal mail servers.
- ntfy notification service sends data to an external URL — no domain allowlist.
- The network monitor (`network_monitor.py`) monitors connectivity but does not block outbound data flows.

**What is required for a credible data residency guarantee:**
- Strict allowlist of outbound network destinations
- Verified enforcement that all AI inference calls go to the configured local endpoint
- Regular automated verification that no data is sent outside the corporate network perimeter
- A documented and auditable "data flows" diagram that IT security can review

**Reference:** BSI C5 (Cloud Computing Compliance Criteria Catalogue) — even for on-premise deployments, German corporate IT departments increasingly use C5 as a benchmark.

---

### 6. Professional Penetration Test — Not Done

Fixing the findings in this report eliminates known vulnerabilities. It does not guarantee the absence of unknown ones. For German corporate deployment:

**Minimum required:**
- A black-box pentest of the HTTP API and WebSocket endpoint by an external party with no access to the source code
- A white-box review of the authentication implementation once it is built
- A social engineering / prompt injection red team exercise specifically targeting the LLM tool call surface

**Recommended certifications for enterprise sales:**
- **BSI IT-Grundschutz** — Germany's federal IT security framework, widely required by German public sector and large Mittelstand
- **ISO/IEC 27001** — International standard, required by most enterprise customers
- **SOC 2 Type II** — Required by many enterprise procurement departments, especially those with US parent companies

**Recommended pentest providers with German corporate experience:**
- SySS GmbH (Tübingen)
- SEC Consult (German branch)
- BSI-certified IS revision auditors (list at bsi.bund.de)

---

### 7. IT Security Policy Alignment

Most German medium and large enterprises have internal IT security policies (typically aligned with BSI IT-Grundschutz or ISO 27001) that any new software must pass before deployment. Common requirements that Kern currently cannot satisfy:

- **Software composition analysis (SCA):** A documented inventory of all third-party dependencies with known CVE status. The `pyproject.toml` lists dependencies but there is no automated CVE scanning in the build process.
- **Secure software development lifecycle (SSDLC):** Evidence of security testing in the development process. The test suite has adversarial tests but no automated SAST (static analysis) or dependency vulnerability scanning.
- **Vulnerability disclosure policy:** A published process for reporting and responding to security vulnerabilities. Not present.
- **Patch SLA:** A documented commitment to patch critical vulnerabilities within N days. Not present.
- **End-of-life policy:** A documented plan for how long the product will receive security updates.

---

### 8. Operational Requirements for Corporate Deployment

**What must exist before handing this to a corporate IT department:**

| Requirement | Current State | Notes |
|-------------|--------------|-------|
| Installation documentation | Partial (README, deploy docs) | Needs hardening guide |
| Uninstall / data deletion | Partial (`uninstall-kern.ps1`) | GDPR erasure not complete |
| Backup and restore procedure | Exists | RTO/RPO not defined |
| Incident response procedure | **Missing** | Required by ISO 27001 |
| Dependency vulnerability scanning | **Missing** | Add `pip-audit` to CI |
| Security patch process | **Missing** | Must commit to SLA |
| Monitoring and alerting | Partial (`metrics.py`) | No alerting on security events |
| Log retention and SIEM integration | **Missing** | Logs must be forwardable to SIEM |
| Disaster recovery test | **Missing** | Backups untested at scale |

---

### 9. Realistic Timeline to Production-Ready

Assuming the 110 technical findings are fixed first:

| Work item | Estimated effort |
|-----------|-----------------|
| Authentication + session management | 3–4 weeks |
| Multi-user / row-level isolation | 6–8 weeks |
| GDPR technical controls (erasure, portability, minimization) | 4–6 weeks |
| Audit trail hash chaining | 1–2 weeks |
| GoBD immutability + retention enforcement | 3–4 weeks |
| Data residency enforcement (network allowlist) | 1–2 weeks |
| CVE scanning + SSDLC tooling | 1 week |
| Documentation (hardening guide, DPA template, DPIA inputs) | 2–3 weeks |
| Professional penetration test + remediation | 4–6 weeks |

**Realistic total: 4–6 months of focused engineering and legal work** before the first corporate pilot should begin. A limited pilot with a single trusted customer under a controlled NDA and formal risk acceptance is reasonable after 2–3 months, provided authentication and the most critical GDPR controls are in place.

---

---

## Next Steps — Actionable Roadmap

> Do these in order. Each phase unblocks the next. Do not skip to Phase 3 before Phase 1 is done — compliance work on top of a broken security foundation is wasted effort.

---

### Phase 1: Fix Critical & High Security Findings

| Task | Finding Ref |
|------|-------------|
| Implement authentication middleware — enforce `KERN_ADMIN_AUTH_TOKEN` on all non-health endpoints | C-01 |
| Validate `Origin` header on WebSocket connections | C-02 |
| Replace `cmd /c start` with `os.startfile()` in Spotify and OpenApp tools | C-03 |
| Force CSRF on in production mode regardless of env var | C-04 |
| Fix license validation — at least one failure mode must deny access | C-05 |
| Validate all LLM tool call arguments against expected schemas | C-06 |
| Remove or restrict `KERN_SKIP_VALIDATION` to dev-only | C-07 |
| Add path boundary validation to all filesystem-touching operations (backup, restore, ingest, retention, attachments) | H-02, H-04, M-04, M-14 |
| Remove `cmd`/`powershell` from app whitelist | H-05 |
| Fix CSRF timing attack — use `hmac.compare_digest` | H-13 |
| Remove User-Agent rate limit bypass | H-01 |

---

### Phase 2: Fix High & Medium Bugs

| Task | Finding Ref |
|------|-------------|
| Replace all `datetime.now()` with `datetime.now(timezone.utc)` globally | M-15 |
| Fix non-atomic encrypted database write (use temp file + rename) | H-17 |
| Add threading lock to encrypted DB persistence | H-16 |
| Fix scheduler TOCTOU race (optimistic locking) | M-22 |
| Fix LLM health check — 404 is not healthy | M-17 |
| Fix RAG `answer()` — move extractive check before LLM call | M-19 |
| Add SQL full-text search / pre-filtering (eliminate full table scans) | H-11 |
| Fix `create_local_event` duplicate definitions | M-16 |
| Fix cron day-of-week wrap-around bug | M-23 |
| Fix TTS worker thread data race | M-21 |
| Add file size limit before Excel parsing | M-24 |
| Redact backup passwords from policy gate arguments | M-02 |

---

### Phase 3: Build Authentication & Multi-User

- Design user model: each employee gets isolated profile, documents, preferences
- Add row-level isolation to every database table (`user_id` / `tenant_id`)
- Enforce ownership checks on every API endpoint and WebSocket command
- Build admin provisioning: create, suspend, audit user accounts
- Add session management: login, logout, session expiry
- Central server deployment model: one Kern server, employees connect via browser

---

### Phase 4: GDPR & GoBD Compliance

- Implement complete data erasure flow (Article 17) — all tables, backups, embeddings, knowledge graph
- Add data export / portability feature (Article 20)
- Enforce data minimization — make clipboard reading, email metadata collection opt-in
- Add audit trail hash chaining — tamper-evident records
- Enforce GoBD retention minimums (6 years commercial, 10 years tax) — block deletion of legally-retained documents
- Add document immutability flag — GoBD-relevant documents cannot be modified, only versioned
- Prepare DPA template (Auftragsverarbeitungsvertrag) for customer contracts
- Conduct DPIA (Data Protection Impact Assessment) — document what data is processed, why, for how long

---

### Phase 5: Model Fine-Tuning

- Choose base model: **Mistral 7B Instruct v0.3** or **Llama 3.1 8B Instruct** (not Qwen — Chinese supply chain concerns in German corporate)
- Build dataset: 3,000–5,000 high-quality German business document instruction pairs
  - 2,000–3,000 synthetic (generated via Claude/GPT-4) — Rechnungen, Angebote, Behördenschreiben, DSGVO-Texte
  - 1,000–2,000 real anonymized documents from first pilot customer
  - Apply difficulty-graded curriculum: 20% simple / 40% standard / 30% complex / 10% multi-constraint
- Purge synthetic failure modes: regex-filter informal pronouns, hallucinated legal citations, anglicized syntax
- Fine-tune using QLoRA: 4-bit NF4, rank 64, all-linear layers, cosine decay, LR 2e-5, warmup 0.05
- Your hardware (16GB VRAM): supports 7B/8B training directly — for 14B, rent A100 80GB on RunPod (~$80-150 for one training run)
- Merge adapters → convert to GGUF Q5_K_M → re-evaluate the GGUF output (not just training checkpoints)
- Evaluate with LLM-as-a-Judge, not BLEU/ROUGE — test DIN 5008 compliance, § 14 UStG fields, register resilience
- Refer to: `AI Fine-Tuning Playbook for German Documents.md` for full technical detail

---

### Phase 6: Pre-Pilot Hardening

- Commission professional penetration test (recommended: SySS GmbH or SEC Consult)
- Remediate pentest findings
- Add CVE scanning to build pipeline (`pip-audit`)
- Write hardening guide for IT administrators
- Define and document patch SLA (e.g., critical vulnerabilities patched within 72 hours)
- Set up log forwarding / SIEM integration for corporate IT
- Test backup restore procedure end-to-end — define RTO/RPO
- Write incident response procedure

---

### Phase 7: First Corporate Pilot

- Single trusted customer, controlled NDA, formal risk acceptance document
- Deploy on customer's on-premise server (central deployment — employees use browser, no model on laptops)
- Monitor, collect error-driven fine-tuning data from real usage
- Iterate on model with Phase 5 error-driven injection technique

---

### Summary

| Phase | What | Dependency |
|-------|------|-----------|
| 1 | Fix critical/high security | Start now |
| 2 | Fix medium bugs | After Phase 1 |
| 3 | Multi-user architecture | After Phase 1 |
| 4 | GDPR / GoBD compliance | After Phase 3 |
| 5 | Model fine-tuning | Parallel with 3–4 |
| 6 | Pentest + hardening | After 3–5 |
| 7 | First pilot | After Phase 6 |

---

---

## Product Idea: Continuously Improving Local Intelligence Layer

### The Core Principle
The model does not need to get smarter after deployment. It needs to know more about this specific customer. These are different problems with different solutions.

A fine-tuned model knows German business language. But it does not know that this customer always uses 14-day payment terms, calls their product "Basispaket", and addresses their contact at Müller GmbH by name. That is not a model problem — it is a memory and retrieval problem.

---

### Architecture

```
Fixed Model (frozen after deployment)
         ↓
Customer Knowledge Base (grows daily through normal use)
         ↓
RAG retrieval at query time
         ↓
Output that feels personalized and improving
```

The model never changes after deployment. The knowledge base is what learns.

---

### What the Knowledge Base Captures Automatically

During normal use, with no action required from the user:

- Every approved document — stored as a reference example
- Every correction — stored as a preference signal
- Supplier names, product names, prices mentioned in documents — extracted and stored
- Preferred phrasing, closing lines, salutations — captured from approvals
- Frequently used document structures — recognized and stored as templates

---

### How Output Improves Over Time

**Day 1:** User asks Kern to write an Angebot. Kern uses only its base knowledge of German Angebot structure.

**Day 30:** User asks the same thing. Kern retrieves the last 8 approved Angebote, standard payment terms, preferred closing paragraph, actual product names and prices, and the specific contact person at the recipient company. The output is dramatically better — and no training happened.

---

### What You Tell the Customer

> "Kern learns your business by remembering what you approve and how you work. No training, no algorithms running at night. It simply remembers — like a colleague who has worked with you for a year."

Honest, accurate, and auditable by any German IT department.

---

### Notifications — Transparent and Specific

Instead of opaque "model updated" messages:

> "This week I processed 12 new documents. I now know your standard Basispaket price, your preferred payment terms, and your contact at Müller GmbH. This will be reflected in future drafts."

---

### What Fine-Tuning Is For in This Architecture

Fine-tune once, centrally, before deployment — to teach the model German business language, Nominalstil, DIN 5008, § 14 UStG. After deployment the model is frozen. Per-customer intelligence comes entirely from the knowledge layer.

This is fully auditable by regulators: fixed model, customer data stored locally, used only to improve retrieval context for that customer, nothing sent anywhere, nothing trained on after deployment.

---

### What Already Exists in the Codebase

This architecture is largely already present — the RAG pipeline, the knowledge graph, the memory system, the document ingestion layer. The work is directing what exists toward this specific purpose, not building from scratch.

---

*End of audit report.*
