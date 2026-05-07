from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    pass


_KNOWN_IANA_REGIONS = {
    "Africa",
    "America",
    "Antarctica",
    "Arctic",
    "Asia",
    "Atlantic",
    "Australia",
    "Brazil",
    "Canada",
    "Chile",
    "Etc",
    "Europe",
    "Indian",
    "Mexico",
    "Pacific",
    "US",
}
_TIMEZONE_TOKEN = re.compile(r"^[A-Za-z0-9._+-]+$")


def _timezone_db_available() -> bool:
    try:
        ZoneInfo("UTC")
        return True
    except ZoneInfoNotFoundError:
        return False


def _looks_like_iana_timezone(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return False
    if normalized == "UTC":
        return True
    parts = normalized.split("/")
    if len(parts) < 2 or parts[0] not in _KNOWN_IANA_REGIONS:
        return False
    return all(part and _TIMEZONE_TOKEN.fullmatch(part) for part in parts)


def _timezone_is_acceptable(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return False
    try:
        ZoneInfo(normalized)
        return True
    except (ZoneInfoNotFoundError, KeyError):
        if not _timezone_db_available() and _looks_like_iana_timezone(normalized):
            logger.info(
                "ZoneInfo database unavailable; accepting timezone '%s' by IANA-format fallback.",
                normalized,
            )
            return True
        return False


def _is_loopback_llm_url(value: str) -> bool:
    parsed = urlparse((value or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def validate_settings(settings) -> list[str]:
    """Validate a Settings instance and return a list of error messages.

    Returns an empty list if all validations pass.
    """
    errors: list[str] = []
    value = lambda name, default=None: getattr(settings, name, default)
    external_llm_endpoint = bool(str(value("llama_server_url", "") or "").strip())

    # --- Enum validations ---
    if value("product_posture", "production") not in ("production", "personal"):
        errors.append(
            f"KERN_PRODUCT_POSTURE must be 'production' or 'personal', "
            f"got '{value('product_posture', 'production')}'"
        )

    if value("policy_mode", "personal") not in ("corporate", "personal"):
        errors.append(
            f"KERN_POLICY_MODE must be 'corporate' or 'personal', "
            f"got '{value('policy_mode', 'personal')}'"
        )

    desktop_loopback_mode = bool(value("desktop_mode", False)) and bool(value("disable_auth_for_loopback", False))

    if not value("server_mode", False) and (
        value("product_posture", "production") == "production" or value("policy_mode", "personal") == "corporate"
    ):
        if not desktop_loopback_mode and not str(value("admin_auth_token", "") or "").strip():
            errors.append(
                "KERN_ADMIN_AUTH_TOKEN must be set when production posture or corporate policy mode is enabled"
            )
        if (value("oidc_enabled", False) or value("admin_dashboard_enabled", False)) and not str(value("session_secret", "") or "").strip():
            errors.append(
                "KERN_SESSION_SECRET must be set when production posture or corporate policy mode is enabled"
            )
        if value("oidc_enabled", False):
            if not str(value("oidc_issuer_url", "") or "").strip():
                errors.append("KERN_OIDC_ISSUER_URL must be set when KERN_OIDC_ENABLED=true")
            if not str(value("oidc_client_id", "") or "").strip():
                errors.append("KERN_OIDC_CLIENT_ID must be set when KERN_OIDC_ENABLED=true")
            if not str(value("oidc_redirect_uri", "") or "").strip():
                errors.append("KERN_OIDC_REDIRECT_URI must be set when KERN_OIDC_ENABLED=true")

    if value("server_mode", False):
        required = {
            "KERN_POSTGRES_DSN": "postgres_dsn",
            "KERN_REDIS_URL": "redis_url",
            "KERN_OIDC_ISSUER_URL": "oidc_issuer_url",
            "KERN_OIDC_CLIENT_ID": "oidc_client_id",
            "KERN_OIDC_CLIENT_SECRET": "oidc_client_secret",
            "KERN_OIDC_REDIRECT_URI": "oidc_redirect_uri",
            "KERN_SESSION_SECRET": "session_secret",
            "KERN_ENCRYPTION_KEY_PROVIDER": "encryption_key_provider",
            "KERN_OBJECT_STORAGE_ROOT": "object_storage_root",
            "KERN_NETWORK_ALLOWED_HOSTS": "network_allowed_hosts",
            "KERN_PUBLIC_BASE_URL": "public_base_url",
        }
        for env_name, attr in required.items():
            if not str(value(attr, "") or "").strip():
                errors.append(f"{env_name} must be set when KERN_SERVER_MODE=true")
        if not value("oidc_enabled", False):
            errors.append("KERN_OIDC_ENABLED=true is required when KERN_SERVER_MODE=true")
        if not value("proxy_headers_enabled", False):
            errors.append("KERN_PROXY_HEADERS_ENABLED=true is required when KERN_SERVER_MODE=true")
        if value("disable_auth_for_loopback", False):
            errors.append("KERN_DISABLE_AUTH_FOR_LOOPBACK=false is required when KERN_SERVER_MODE=true")
        if str(value("admin_auth_token", "") or "").strip():
            errors.append("KERN_ADMIN_AUTH_TOKEN must not be used for normal server-mode authentication")
        if value("server_break_glass_enabled", False) and not str(value("break_glass_ip_allowlist", "") or "").strip():
            errors.append("KERN_BREAK_GLASS_IP_ALLOWLIST must be set when server break-glass is enabled")
        if value("server_break_glass_enabled", False) and not str(value("break_glass_password", "") or "").strip():
            errors.append("KERN_BREAK_GLASS_PASSWORD must be set when server break-glass is enabled")

    if value("cognition_backend", "hybrid") not in ("hybrid", "llama_cpp", "openai"):
        errors.append(
            f"KERN_COGNITION_BACKEND must be 'hybrid', 'llama_cpp', or 'openai', "
            f"got '{value('cognition_backend', 'hybrid')}'"
        )

    if value("db_encryption_mode", "fernet") not in ("fernet", "off", "none"):
        errors.append(
            f"KERN_DB_ENCRYPTION_MODE must be 'fernet' or 'off' (legacy alias 'none' is also accepted), "
            f"got '{value('db_encryption_mode', 'fernet')}'"
        )

    if value("sync_mode", "off") not in ("off", "webdav", "nextcloud"):
        errors.append(
            f"KERN_SYNC_MODE must be 'off', 'webdav', or 'nextcloud', "
            f"got '{value('sync_mode', 'off')}'"
        )

    if value("ui_language", "en") not in ("en", "de"):
        errors.append(
            f"KERN_UI_LANGUAGE must be 'en' or 'de', "
            f"got '{value('ui_language', 'en')}'"
        )

    if value("ocr_engine", "paddleocr") not in ("paddleocr",):
        errors.append(
            f"KERN_OCR_ENGINE must be 'paddleocr', got '{value('ocr_engine', 'paddleocr')}'"
        )

    if value("llm_enabled", False) and value("llm_local_only", True) and not _is_loopback_llm_url(str(value("llama_server_url", "") or "")):
        errors.append(
            "KERN_LLAMA_SERVER_URL must point to a localhost endpoint when "
            "KERN_LLM_LOCAL_ONLY=true"
        )

    if value("product_posture", "production") == "production" and value("policy_mode", "personal") == "corporate":
        if value("llm_enabled", False) and not value("llm_local_only", True):
            errors.append(
                "Corporate production posture requires KERN_LLM_LOCAL_ONLY=true when LLM inference is enabled"
            )
        if value("allow_cloud_llm", False):
            errors.append(
                "Corporate production posture requires KERN_ALLOW_CLOUD_LLM=false"
            )

    # An external local server can be used without a local GGUF path.
    # Only validate the path when one is explicitly provided.
    if value("llama_server_model_path"):
        model_path = Path(value("llama_server_model_path")).expanduser()
        if not model_path.exists():
            errors.append(
                f"KERN_LLAMA_SERVER_MODEL_PATH does not exist: '{value('llama_server_model_path')}'"
            )
    elif value("llm_enabled", False) and external_llm_endpoint:
        pass

    if value("license_public_key_path") and not Path(value("license_public_key_path")).expanduser().exists():
        errors.append(
            f"KERN_LICENSE_PUBLIC_KEY_PATH does not exist: '{value('license_public_key_path')}'"
        )

    # --- Timezone validation ---
    if not _timezone_is_acceptable(str(value("timezone", "") or "")):
        errors.append(
            f"KERN_TIMEZONE must be a valid IANA timezone, "
            f"got '{value('timezone', '')}'"
        )

    # --- Positive integer validations ---
    positive_int_fields = {
        "KERN_LLM_MAX_TOKENS": value("llm_max_tokens", 1024),
        "KERN_LLM_CONTEXT_WINDOW": value("llm_context_window", 8192),
        "KERN_PROMPT_CACHE_SIZE": value("prompt_cache_size", 24),
        "KERN_SNAPSHOT_DIRTY_DEBOUNCE_MS": value("snapshot_dirty_debounce_ms", 120),
        "KERN_CONTEXT_CLIPBOARD_MAX_CHARS": value("context_clipboard_max_chars", 280),
        "KERN_NETWORK_MONITOR_INTERVAL": value("network_monitor_interval", 30),
        "KERN_SCHEDULER_RETRY_DELAY_MINUTES": value("scheduler_retry_delay_minutes", 10),
        "KERN_SCHEDULER_MAX_RETRIES": value("scheduler_max_retries", 2),
        "KERN_SCHEDULER_STALE_RUN_MINUTES": value("scheduler_stale_run_minutes", 45),
        "KERN_SESSION_TTL_HOURS": value("session_ttl_hours", 8),
        "KERN_SESSION_IDLE_MINUTES": value("session_idle_minutes", 60),
        "KERN_RETENTION_DOCUMENTS_DAYS": value("retention_documents_days", 3650),
        "KERN_RETENTION_TRANSCRIPTS_DAYS": value("retention_transcripts_days", 365),
        "KERN_RETENTION_AUDIT_DAYS": value("retention_audit_days", 2555),
        "KERN_RETENTION_BACKUPS_DAYS": value("retention_backups_days", 365),
        "KERN_RETENTION_RUN_INTERVAL_HOURS": value("retention_run_interval_hours", 12),
        "KERN_RAG_TOP_K": value("rag_top_k", 12),
        "KERN_RAG_RERANK_TOP_N": value("rag_rerank_top_n", 4),
        "KERN_PROACTIVE_SCAN_INTERVAL": value("proactive_scan_interval", 600),
        "KERN_OCR_MIN_TEXT_CHARS_PER_PAGE": value("ocr_min_text_chars_per_page", 16),
    }
    for var_name, field_value in positive_int_fields.items():
        if field_value <= 0:
            errors.append(
                f"{var_name} must be a positive integer, got {field_value}"
            )

    # --- Positive float validations ---
    positive_float_fields = {
        "KERN_HEARTBEAT_SECONDS": value("heartbeat_seconds", 2.0),
        "KERN_MONITOR_INTERVAL_SECONDS": value("monitor_interval_seconds", 0.35),
        "KERN_CONTEXT_REFRESH_SECONDS": value("context_refresh_seconds", 1.5),
        "KERN_CAPABILITY_REFRESH_SECONDS": value("capability_refresh_seconds", 3.0),
        "KERN_LLAMA_SERVER_TIMEOUT": value("llama_server_timeout", 30.0),
        "KERN_LLM_TEMPERATURE": value("llm_temperature", 0.3),
        "KERN_OCR_LOW_CONFIDENCE_THRESHOLD": value("ocr_low_confidence_threshold", 0.55),
    }
    for var_name, field_value in positive_float_fields.items():
        if field_value <= 0:
            errors.append(
                f"{var_name} must be a positive number, got {field_value}"
            )

    # --- Range validations ---
    if not (0.0 <= value("rag_min_score", 0.1) <= 1.0):
        errors.append(
            f"KERN_RAG_MIN_SCORE must be between 0.0 and 1.0, "
            f"got {value('rag_min_score', 0.1)}"
        )

    if not (0.0 <= value("ocr_low_confidence_threshold", 0.55) <= 1.0):
        errors.append(
            f"KERN_OCR_LOW_CONFIDENCE_THRESHOLD must be between 0.0 and 1.0, "
            f"got {value('ocr_low_confidence_threshold', 0.55)}"
        )

    return errors


def validate_env_types() -> list[str]:
    """Validate raw environment variable types before Settings construction.

    Catches int/float parsing errors that would otherwise crash the dataclass.
    Returns a list of error messages.
    """
    errors: list[str] = []
    int_vars = [
        "KERN_LLM_MAX_TOKENS", "KERN_LLM_CONTEXT_WINDOW", "KERN_PROMPT_CACHE_SIZE",
        "KERN_SNAPSHOT_DIRTY_DEBOUNCE_MS",
        "KERN_CONTEXT_CLIPBOARD_MAX_CHARS", "KERN_NETWORK_MONITOR_INTERVAL",
        "KERN_SCHEDULER_RETRY_DELAY_MINUTES", "KERN_SCHEDULER_MAX_RETRIES",
        "KERN_SCHEDULER_STALE_RUN_MINUTES", "KERN_RETENTION_DOCUMENTS_DAYS",
        "KERN_SESSION_TTL_HOURS", "KERN_SESSION_IDLE_MINUTES",
        "KERN_RETENTION_TRANSCRIPTS_DAYS",
        "KERN_RETENTION_AUDIT_DAYS", "KERN_RETENTION_BACKUPS_DAYS",
        "KERN_RETENTION_RUN_INTERVAL_HOURS", "KERN_RAG_TOP_K",
        "KERN_RAG_RERANK_TOP_N",
        "KERN_PROACTIVE_SCAN_INTERVAL", "KERN_FILE_WATCH_RECONCILE_MINUTES",
        "KERN_OCR_MIN_TEXT_CHARS_PER_PAGE",
    ]
    for var in int_vars:
        raw = os.getenv(var)
        if raw is not None:
            try:
                int(raw)
            except ValueError:
                errors.append(
                    f"{var} must be an integer, got '{raw}'"
                )

    float_vars = [
        "KERN_HEARTBEAT_SECONDS", "KERN_MONITOR_INTERVAL_SECONDS",
        "KERN_CONTEXT_REFRESH_SECONDS", "KERN_CAPABILITY_REFRESH_SECONDS",
        "KERN_LLAMA_SERVER_TIMEOUT", "KERN_LLM_TEMPERATURE",
        "KERN_RAG_MIN_SCORE",
        "KERN_INTENT_FALLBACK_MIN_CONFIDENCE",
        "KERN_OCR_LOW_CONFIDENCE_THRESHOLD",
    ]
    for var in float_vars:
        raw = os.getenv(var)
        if raw is not None:
            try:
                float(raw)
            except ValueError:
                errors.append(
                    f"{var} must be a number, got '{raw}'"
                )

    return errors


def run_validation(settings) -> None:
    """Run all validations and exit if any fail.

    Skipped when KERN_SKIP_VALIDATION=1 is set.
    """
    if os.getenv("KERN_SKIP_VALIDATION", "").strip() in ("1", "true", "yes"):
        return

    errors = validate_settings(settings)
    if errors:
        logger.error("Configuration validation failed:")
        for error in errors:
            logger.error("  - %s", error)
        print("KERN configuration validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)
