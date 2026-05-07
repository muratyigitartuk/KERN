from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


if os.getenv("KERN_DISABLE_DOTENV", "").strip().lower() not in {"1", "true", "yes", "on"}:
    load_dotenv()


def _validation_skip_allowed() -> bool:
    skip_requested = os.getenv("KERN_SKIP_VALIDATION", "").strip().lower() in ("1", "true", "yes")
    posture = (os.getenv("KERN_PRODUCT_POSTURE", "production") or "production").strip().lower()
    policy_mode = (os.getenv("KERN_POLICY_MODE", "personal") or "personal").strip().lower()
    if skip_requested and (posture == "production" or policy_mode == "corporate"):
        print(
            "KERN_SKIP_VALIDATION is not allowed in production or corporate mode.",
            file=sys.stderr,
        )
        sys.exit(1)
    return skip_requested


if not _validation_skip_allowed():
    from app.config_validation import validate_env_types

    _type_errors = validate_env_types()
    if _type_errors:
        print("KERN configuration validation failed:", file=sys.stderr)
        for _err in _type_errors:
            print(f"  - {_err}", file=sys.stderr)
        sys.exit(1)
    del _type_errors


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is not None:
        return value
    return default


def _enum(value: str | None, default: str, allowed: set[str]) -> str:
    if value is None:
        return default
    normalized = value.strip().lower()
    return normalized if normalized in allowed else default


def _normalize_db_encryption_mode(value: str | None, default: str = "fernet") -> str:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized == "none":
        return "off"
    return normalized or default


@dataclass(slots=True)
class Settings:
    db_path: Path = Path(_env("KERN_DB_PATH", default="kern.db")).resolve()
    system_db_path: Path = Path(_env("KERN_SYSTEM_DB_PATH", default="kern-system.db")).resolve()
    root_path: Path = Path(_env("KERN_ROOT_PATH", default=".kern")).resolve()
    profile_root: Path = Path(_env("KERN_PROFILE_ROOT", default=".kern/profiles")).resolve()
    backup_root: Path = Path(_env("KERN_BACKUP_ROOT", default=".kern/backups")).resolve()
    document_root: Path = Path(_env("KERN_DOCUMENT_ROOT", default=".kern/documents")).resolve()
    attachment_root: Path = Path(_env("KERN_ATTACHMENT_ROOT", default=".kern/attachments")).resolve()
    archive_root: Path = Path(_env("KERN_ARCHIVE_ROOT", default=".kern/archives")).resolve()
    timezone: str = _env("KERN_TIMEZONE", default="Europe/Berlin") or "Europe/Berlin"
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
    user_title: str = _env("KERN_USER_TITLE", default="") or ""
    product_posture: str = _enum(_env("KERN_PRODUCT_POSTURE", default="production"), "production", {"production", "personal"})
    local_mode_enabled: bool = _as_bool(_env("KERN_LOCAL_MODE"), True)
    cognition_backend: str = _env("KERN_COGNITION_BACKEND", default="hybrid") or "hybrid"
    cognition_model: str | None = _env("KERN_COGNITION_MODEL") or None
    intent_fallback_mode: str = _env("KERN_INTENT_FALLBACK_MODE", default="off") or "off"
    intent_fallback_min_confidence: float = float(_env("KERN_INTENT_FALLBACK_MIN_CONFIDENCE", default="0.6") or "0.6")
    embed_model: str | None = _env("KERN_EMBED_MODEL") or None
    proactive_enabled: bool = _as_bool(_env("KERN_PROACTIVE_ENABLED"), True)
    heartbeat_seconds: float = float(_env("KERN_HEARTBEAT_SECONDS", default="2.0") or "2.0")
    monitor_interval_seconds: float = float(_env("KERN_MONITOR_INTERVAL_SECONDS", default="0.35") or "0.35")
    snapshot_dirty_debounce_ms: int = int(_env("KERN_SNAPSHOT_DIRTY_DEBOUNCE_MS", default="120") or "120")
    context_refresh_seconds: float = float(_env("KERN_CONTEXT_REFRESH_SECONDS", default="1.5") or "1.5")
    capability_refresh_seconds: float = float(_env("KERN_CAPABILITY_REFRESH_SECONDS", default="3.0") or "3.0")
    context_window_enabled: bool = _as_bool(_env("KERN_CONTEXT_WINDOW_ENABLED"), True)
    context_clipboard_enabled: bool = _as_bool(_env("KERN_CONTEXT_CLIPBOARD_ENABLED"), False)
    context_clipboard_max_chars: int = int(_env("KERN_CONTEXT_CLIPBOARD_MAX_CHARS", default="280") or "280")
    seed_defaults: bool = _as_bool(_env("KERN_SEED_DEFAULTS"), False)
    rag_enabled: bool = _as_bool(_env("KERN_RAG_ENABLED"), False)
    rag_embed_model: str | None = _env("KERN_RAG_EMBED_MODEL") or None
    rag_index_version: str = _env("KERN_RAG_INDEX_VERSION", default="v1") or "v1"
    model_mode: str = _env("KERN_MODEL_MODE", default="off") or "off"
    fast_model_path: str | None = _env("KERN_FAST_MODEL_PATH") or None
    deep_model_path: str | None = _env("KERN_DEEP_MODEL_PATH") or None
    prompt_cache_enabled: bool = _as_bool(_env("KERN_PROMPT_CACHE_ENABLED"), True)
    prompt_cache_size: int = int(_env("KERN_PROMPT_CACHE_SIZE", default="24") or "24")
    allow_cloud_llm: bool = _as_bool(_env("KERN_ALLOW_CLOUD_LLM"), False)
    audit_enabled: bool = _as_bool(_env("KERN_AUDIT_ENABLED"), True)
    policy_mode: str = _enum(_env("KERN_POLICY_MODE", default="personal"), "personal", {"corporate", "personal"})
    policy_allow_external_network: bool = _as_bool(_env("KERN_POLICY_ALLOW_EXTERNAL_NETWORK"), False)
    policy_restrict_sensitive_reads: bool = _as_bool(_env("KERN_POLICY_RESTRICT_SENSITIVE_READS"), True)
    policy_restrict_sensitive_exports: bool = _as_bool(_env("KERN_POLICY_RESTRICT_SENSITIVE_EXPORTS"), True)
    db_encryption_mode: str = _normalize_db_encryption_mode(_env("KERN_DB_ENCRYPTION_MODE", default="fernet"), "fernet")
    artifact_encryption_enabled: bool = _as_bool(_env("KERN_ARTIFACT_ENCRYPTION_ENABLED"), True)
    key_derivation_version: str = _env("KERN_KEY_DERIVATION_VERSION", default="v1") or "v1"
    profile_key_rotation_required: bool = _as_bool(_env("KERN_PROFILE_KEY_ROTATION_REQUIRED"), False)
    transcript_model: str | None = _env("KERN_TRANSCRIPT_MODEL") or None
    sync_mode: str = _env("KERN_SYNC_MODE", default="off") or "off"
    nextcloud_url: str | None = _env("KERN_NEXTCLOUD_URL") or None
    update_channel: str = _env("KERN_UPDATE_CHANNEL", default="stable") or "stable"
    license_root: Path = Path(
        _env("KERN_LICENSE_ROOT")
        or str(Path(_env("KERN_ROOT_PATH", default=".kern")).resolve() / "licenses")
    ).resolve()
    license_public_key: str | None = _env("KERN_LICENSE_PUBLIC_KEY") or None
    license_public_key_path: Path | None = Path(_env("KERN_LICENSE_PUBLIC_KEY_PATH")).resolve() if _env("KERN_LICENSE_PUBLIC_KEY_PATH") else None
    pwa_enabled: bool = _as_bool(_env("KERN_PWA_ENABLED"), False)
    llama_server_url: str = _env("KERN_LLAMA_SERVER_URL", default="http://127.0.0.1:8080") or "http://127.0.0.1:8080"
    llama_server_timeout: float = float(_env("KERN_LLAMA_SERVER_TIMEOUT", default="120.0") or "120.0")
    llama_server_binary: str | None = _env("KERN_LLAMA_SERVER_BINARY") or None
    llama_server_model_path: str | None = _env("KERN_LLAMA_SERVER_MODEL_PATH") or None
    llm_model: str | None = _env("KERN_LLM_MODEL") or None
    llm_local_only: bool = _as_bool(_env("KERN_LLM_LOCAL_ONLY"), True)
    llm_enabled: bool = _as_bool(_env("KERN_LLM_ENABLED"), False)
    llm_max_tokens: int = int(_env("KERN_LLM_MAX_TOKENS", default="1024") or "1024")
    llm_temperature: float = float(_env("KERN_LLM_TEMPERATURE", default="0.3") or "0.3")
    llm_context_window: int = int(_env("KERN_LLM_CONTEXT_WINDOW", default="8192") or "8192")
    embed_model_path: str | None = _env("KERN_EMBED_MODEL_PATH") or None
    vec_enabled: bool = _as_bool(_env("KERN_VEC_ENABLED"), False)
    rag_top_k: int = int(_env("KERN_RAG_TOP_K", default="12") or "12")
    rag_rerank_top_n: int = int(_env("KERN_RAG_RERANK_TOP_N", default="4") or "4")
    rag_min_score: float = float(_env("KERN_RAG_MIN_SCORE", default="0.1") or "0.1")
    rag_reranker_backend: str = _env("KERN_RAG_RERANKER_BACKEND", default="llm") or "llm"
    rag_rerank_max_concurrency: int = int(_env("KERN_RAG_RERANK_MAX_CONCURRENCY", default="4") or "4")
    rag_rerank_timeout_seconds: float = float(_env("KERN_RAG_RERANK_TIMEOUT_SECONDS", default="8.0") or "8.0")
    vec_index_batch_size: int = int(_env("KERN_VEC_INDEX_BATCH_SIZE", default="32") or "32")
    network_monitor_enabled: bool = _as_bool(_env("KERN_NETWORK_MONITOR_ENABLED"), True)
    network_monitor_interval: int = int(_env("KERN_NETWORK_MONITOR_INTERVAL", default="30") or "30")
    network_allowed_hosts: str = _env("KERN_NETWORK_ALLOWED_HOSTS", default="127.0.0.1,localhost,::1") or "127.0.0.1,localhost,::1"
    scheduler_enabled: bool = _as_bool(_env("KERN_SCHEDULER_ENABLED"), True)
    scheduler_retry_delay_minutes: int = int(_env("KERN_SCHEDULER_RETRY_DELAY_MINUTES", default="10") or "10")
    scheduler_max_retries: int = int(_env("KERN_SCHEDULER_MAX_RETRIES", default="2") or "2")
    scheduler_stale_run_minutes: int = int(_env("KERN_SCHEDULER_STALE_RUN_MINUTES", default="45") or "45")
    retention_documents_days: int = int(_env("KERN_RETENTION_DOCUMENTS_DAYS", default="3650") or "3650")
    legacy_email_retention_days: int = 730
    retention_transcripts_days: int = int(_env("KERN_RETENTION_TRANSCRIPTS_DAYS", default="365") or "365")
    retention_audit_days: int = int(_env("KERN_RETENTION_AUDIT_DAYS", default="2555") or "2555")
    retention_backups_days: int = int(_env("KERN_RETENTION_BACKUPS_DAYS", default="365") or "365")
    retention_enforcement_enabled: bool = _as_bool(_env("KERN_RETENTION_ENFORCEMENT_ENABLED"), True)
    retention_run_interval_hours: int = int(_env("KERN_RETENTION_RUN_INTERVAL_HOURS", default="12") or "12")
    file_watch_dirs: str = _env("KERN_FILE_WATCH_DIRS", default="") or ""
    file_watch_reconcile_minutes: int = int(_env("KERN_FILE_WATCH_RECONCILE_MINUTES", default="30") or "30")
    proactive_scan_interval: int = int(_env("KERN_PROACTIVE_SCAN_INTERVAL", default="600") or "600")
    dpo_contact_name: str = _env("KERN_DPO_CONTACT_NAME", default="") or ""
    dpo_contact_email: str = _env("KERN_DPO_CONTACT_EMAIL", default="") or ""
    ui_language: str = _env("KERN_UI_LANGUAGE", default="en") or "en"
    log_level: str = _env("KERN_LOG_LEVEL", default="INFO") or "INFO"
    log_format: str = _enum(_env("KERN_LOG_FORMAT", default="text"), "text", {"text", "json"})

    # â”€â”€ Phase 8 anticipated features â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Calendar cloud integration (8.5)
    outlook_calendar_tenant_id: str | None = _env("KERN_OUTLOOK_TENANT_ID") or None
    outlook_calendar_client_id: str | None = _env("KERN_OUTLOOK_CLIENT_ID") or None
    # Document classification (8.9)
    document_auto_classify: bool = _as_bool(_env("KERN_DOCUMENT_AUTO_CLASSIFY"), False)
    document_default_classification: str = _env("KERN_DOCUMENT_DEFAULT_CLASSIFICATION", default="internal") or "internal"
    ocr_enabled: bool = _as_bool(_env("KERN_OCR_ENABLED"), True)
    ocr_engine: str = _enum(_env("KERN_OCR_ENGINE", default="paddleocr"), "paddleocr", {"paddleocr"})
    ocr_lang: str = _env("KERN_OCR_LANG", default="de") or "de"
    ocr_low_confidence_threshold: float = float(_env("KERN_OCR_LOW_CONFIDENCE_THRESHOLD", default="0.80") or "0.80")
    ocr_min_text_chars_per_page: int = int(_env("KERN_OCR_MIN_TEXT_CHARS_PER_PAGE", default="32") or "32")
    # Admin dashboard (8.10)
    admin_dashboard_enabled: bool = _as_bool(_env("KERN_ADMIN_DASHBOARD_ENABLED"), False)
    desktop_mode: bool = _as_bool(_env("KERN_DESKTOP_MODE"), False)
    server_mode: bool = _as_bool(_env("KERN_SERVER_MODE"), False)
    postgres_dsn: str | None = _env("KERN_POSTGRES_DSN") or None
    redis_url: str | None = _env("KERN_REDIS_URL") or None
    object_storage_root: str | None = _env("KERN_OBJECT_STORAGE_ROOT") or None
    encryption_key_provider: str | None = _env("KERN_ENCRYPTION_KEY_PROVIDER") or None
    public_base_url: str | None = _env("KERN_PUBLIC_BASE_URL") or None
    proxy_headers_enabled: bool = _as_bool(_env("KERN_PROXY_HEADERS_ENABLED"), False)
    server_break_glass_enabled: bool = _as_bool(_env("KERN_SERVER_BREAK_GLASS_ENABLED"), False)
    break_glass_ip_allowlist: str = _env("KERN_BREAK_GLASS_IP_ALLOWLIST", default="") or ""
    admin_auth_token: str | None = _env("KERN_ADMIN_AUTH_TOKEN") or None
    disable_auth_for_loopback: bool = _as_bool(_env("KERN_DISABLE_AUTH_FOR_LOOPBACK"), False)
    loopback_nonce: str = _env("KERN_LOOPBACK_NONCE", default="codex-loopback") or "codex-loopback"
    session_cookie_name: str = _env("KERN_SESSION_COOKIE_NAME", default="kern_session") or "kern_session"
    session_secret: str | None = _env("KERN_SESSION_SECRET") or _env("KERN_ADMIN_AUTH_TOKEN") or None
    session_ttl_hours: int = int(_env("KERN_SESSION_TTL_HOURS", default="8") or "8")
    session_idle_minutes: int = int(_env("KERN_SESSION_IDLE_MINUTES", default="60") or "60")
    oidc_enabled: bool = _as_bool(_env("KERN_OIDC_ENABLED"), False)
    oidc_issuer_url: str | None = _env("KERN_OIDC_ISSUER_URL") or None
    oidc_client_id: str | None = _env("KERN_OIDC_CLIENT_ID") or None
    oidc_client_secret: str | None = _env("KERN_OIDC_CLIENT_SECRET") or None
    oidc_redirect_uri: str | None = _env("KERN_OIDC_REDIRECT_URI") or None
    oidc_scopes: str = _env("KERN_OIDC_SCOPES", default="openid profile email") or "openid profile email"
    oidc_allowed_email_domains: str = _env("KERN_OIDC_ALLOWED_EMAIL_DOMAINS", default="") or ""
    oidc_required_group: str | None = _env("KERN_OIDC_REQUIRED_GROUP") or None
    oidc_email_claim: str = _env("KERN_OIDC_EMAIL_CLAIM", default="email") or "email"
    oidc_name_claim: str = _env("KERN_OIDC_NAME_CLAIM", default="name") or "name"
    oidc_groups_claim: str = _env("KERN_OIDC_GROUPS_CLAIM", default="groups") or "groups"
    break_glass_username: str = _env("KERN_BREAK_GLASS_USERNAME", default="breakglass") or "breakglass"
    break_glass_password: str | None = _env("KERN_BREAK_GLASS_PASSWORD") or None

    @property
    def session_ttl_seconds(self) -> int:
        return self.session_ttl_hours * 60 * 60


def _build_settings() -> Settings:
    s = Settings()
    if not _validation_skip_allowed():
        from app.config_validation import validate_settings

        value_errors = validate_settings(s)
        if value_errors:
            print("KERN configuration validation failed:", file=sys.stderr)
            for error in value_errors:
                print(f"  - {error}", file=sys.stderr)
            sys.exit(1)
    return s


settings = _build_settings()
