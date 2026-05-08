"""Tests for configuration validation (Phase 0, Task 0.5)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import _normalize_db_encryption_mode
from app.config_validation import validate_env_types, validate_settings


# ---------------------------------------------------------------------------
# Helper: build a minimal valid Settings-like object for validate_settings
# ---------------------------------------------------------------------------
@dataclass
class FakeSettings:
    product_posture: str = "production"
    policy_mode: str = "personal"
    cognition_backend: str = "hybrid"
    db_encryption_mode: str = "fernet"
    sync_mode: str = "off"
    ui_language: str = "en"
    timezone: str = "Europe/Berlin"
    llm_max_tokens: int = 1024
    llm_context_window: int = 8192
    prompt_cache_size: int = 24
    allow_cloud_llm: bool = False
    snapshot_dirty_debounce_ms: int = 120
    context_clipboard_max_chars: int = 280
    network_monitor_interval: int = 30
    scheduler_retry_delay_minutes: int = 10
    scheduler_max_retries: int = 2
    scheduler_stale_run_minutes: int = 45
    retention_documents_days: int = 3650
    retention_email_days: int = 730
    retention_transcripts_days: int = 365
    retention_audit_days: int = 2555
    retention_backups_days: int = 365
    retention_run_interval_hours: int = 12
    rag_top_k: int = 12
    rag_rerank_top_n: int = 4
    inbox_watch_interval: int = 300
    proactive_scan_interval: int = 600
    heartbeat_seconds: float = 2.0
    monitor_interval_seconds: float = 0.35
    context_refresh_seconds: float = 1.5
    capability_refresh_seconds: float = 3.0
    llama_server_timeout: float = 30.0
    llama_server_url: str = "http://127.0.0.1:8080"
    llama_server_model_path: str | None = None
    llm_enabled: bool = False
    llm_local_only: bool = True
    llm_temperature: float = 0.3
    rag_min_score: float = 0.1
    ocr_engine: str = "paddleocr"
    ocr_low_confidence_threshold: float = 0.55


# ---------------------------------------------------------------------------
# validate_settings — enum validations
# ---------------------------------------------------------------------------
class TestEnumValidation:
    def test_valid_product_posture(self):
        for value in ("production", "personal"):
            s = FakeSettings(product_posture=value)
            errors = validate_settings(s)
            assert not any("KERN_PRODUCT_POSTURE" in e for e in errors)

    def test_invalid_product_posture(self):
        s = FakeSettings(product_posture="debug")
        errors = validate_settings(s)
        assert any("KERN_PRODUCT_POSTURE" in e and "'debug'" in e for e in errors)

    def test_valid_policy_mode(self):
        for value in ("corporate", "personal"):
            s = FakeSettings(policy_mode=value)
            errors = validate_settings(s)
            assert not any("KERN_POLICY_MODE" in e for e in errors)

    def test_invalid_policy_mode(self):
        s = FakeSettings(policy_mode="open")
        errors = validate_settings(s)
        assert any("KERN_POLICY_MODE" in e and "'open'" in e for e in errors)

    def test_valid_cognition_backend(self):
        for value in ("hybrid", "llama_cpp", "openai"):
            s = FakeSettings(cognition_backend=value)
            errors = validate_settings(s)
            assert not any("KERN_COGNITION_BACKEND" in e for e in errors)

    def test_invalid_cognition_backend(self):
        s = FakeSettings(cognition_backend="gpt5")
        errors = validate_settings(s)
        assert any("KERN_COGNITION_BACKEND" in e for e in errors)

    def test_valid_db_encryption_mode(self):
        for value in ("fernet", "off", "none"):
            s = FakeSettings(db_encryption_mode=value)
            errors = validate_settings(s)
            assert not any("KERN_DB_ENCRYPTION_MODE" in e for e in errors)

    def test_invalid_db_encryption_mode(self):
        s = FakeSettings(db_encryption_mode="aes256")
        errors = validate_settings(s)
        assert any("KERN_DB_ENCRYPTION_MODE" in e for e in errors)

    def test_valid_sync_mode(self):
        for value in ("off", "webdav", "nextcloud"):
            s = FakeSettings(sync_mode=value)
            errors = validate_settings(s)
            assert not any("KERN_SYNC_MODE" in e for e in errors)

    def test_invalid_sync_mode(self):
        s = FakeSettings(sync_mode="dropbox")
        errors = validate_settings(s)
        assert any("KERN_SYNC_MODE" in e for e in errors)

    def test_valid_ui_language(self):
        for value in ("en", "de"):
            s = FakeSettings(ui_language=value)
            errors = validate_settings(s)
            assert not any("KERN_UI_LANGUAGE" in e for e in errors)

    def test_invalid_ui_language(self):
        s = FakeSettings(ui_language="fr")
        errors = validate_settings(s)
        assert any("KERN_UI_LANGUAGE" in e for e in errors)

    def test_llm_local_only_accepts_loopback_url(self):
        s = FakeSettings(llm_enabled=True, llm_local_only=True, llama_server_url="http://127.0.0.1:8080")
        errors = validate_settings(s)
        assert not any("KERN_LLAMA_SERVER_URL" in e for e in errors)

    def test_llm_local_only_rejects_remote_url(self):
        s = FakeSettings(llm_enabled=True, llm_local_only=True, llama_server_url="https://example.com")
        errors = validate_settings(s)
        assert any("KERN_LLAMA_SERVER_URL" in e for e in errors)

    def test_corporate_production_requires_local_only_llm(self):
        s = FakeSettings(
            product_posture="production",
            policy_mode="corporate",
            llm_enabled=True,
            llm_local_only=False,
        )
        errors = validate_settings(s)
        assert any("KERN_LLM_LOCAL_ONLY" in e for e in errors)

    def test_corporate_production_rejects_cloud_llm_toggle(self):
        s = FakeSettings(
            product_posture="production",
            policy_mode="corporate",
            allow_cloud_llm=True,
        )
        errors = validate_settings(s)
        assert any("KERN_ALLOW_CLOUD_LLM" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_settings — timezone validation
# ---------------------------------------------------------------------------
class TestTimezoneValidation:
    def test_valid_timezone(self):
        s = FakeSettings(timezone="Europe/Berlin")
        errors = validate_settings(s)
        assert not any("KERN_TIMEZONE" in e for e in errors)

    def test_valid_timezone_utc(self):
        s = FakeSettings(timezone="UTC")
        errors = validate_settings(s)
        assert not any("KERN_TIMEZONE" in e for e in errors)

    def test_invalid_timezone(self):
        s = FakeSettings(timezone="Mars/Olympus")
        errors = validate_settings(s)
        assert any("KERN_TIMEZONE" in e and "'Mars/Olympus'" in e for e in errors)

    def test_empty_timezone(self):
        s = FakeSettings(timezone="")
        errors = validate_settings(s)
        assert any("KERN_TIMEZONE" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_settings — positive integer validations
# ---------------------------------------------------------------------------
class TestPositiveIntegerValidation:
    def test_valid_positive_integers(self):
        s = FakeSettings()
        errors = validate_settings(s)
        assert not errors

    def test_zero_llm_max_tokens(self):
        s = FakeSettings(llm_max_tokens=0)
        errors = validate_settings(s)
        assert any("KERN_LLM_MAX_TOKENS" in e for e in errors)

    def test_negative_retention_days(self):
        s = FakeSettings(retention_documents_days=-1)
        errors = validate_settings(s)
        assert any("KERN_RETENTION_DOCUMENTS_DAYS" in e for e in errors)

    def test_zero_prompt_cache_size(self):
        s = FakeSettings(prompt_cache_size=0)
        errors = validate_settings(s)
        assert any("KERN_PROMPT_CACHE_SIZE" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_settings — positive float validations
# ---------------------------------------------------------------------------
class TestPositiveFloatValidation:
    def test_valid_positive_floats(self):
        s = FakeSettings()
        errors = validate_settings(s)
        assert not errors

    def test_zero_heartbeat(self):
        s = FakeSettings(heartbeat_seconds=0.0)
        errors = validate_settings(s)
        assert any("KERN_HEARTBEAT_SECONDS" in e for e in errors)



# ---------------------------------------------------------------------------
# validate_settings — range validations
# ---------------------------------------------------------------------------
class TestRangeValidation:
    def test_rag_min_score_above_range(self):
        s = FakeSettings(rag_min_score=2.0)
        errors = validate_settings(s)
        assert any("KERN_RAG_MIN_SCORE" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_env_types — raw environment variable type checking
# ---------------------------------------------------------------------------
class TestEnvTypeValidation:
    def test_valid_int_env(self):
        with patch.dict(os.environ, {"KERN_LLM_MAX_TOKENS": "2048"}, clear=False):
            errors = validate_env_types()
            assert not any("KERN_LLM_MAX_TOKENS" in e for e in errors)

    def test_invalid_int_env(self):
        with patch.dict(os.environ, {"KERN_LLM_MAX_TOKENS": "abc"}, clear=False):
            errors = validate_env_types()
            assert any("KERN_LLM_MAX_TOKENS" in e and "'abc'" in e for e in errors)

    def test_valid_float_env(self):
        with patch.dict(os.environ, {"KERN_HEARTBEAT_SECONDS": "3.5"}, clear=False):
            errors = validate_env_types()
            assert not any("KERN_HEARTBEAT_SECONDS" in e for e in errors)

    def test_invalid_float_env(self):
        with patch.dict(os.environ, {"KERN_HEARTBEAT_SECONDS": "not_a_float"}, clear=False):
            errors = validate_env_types()
            assert any("KERN_HEARTBEAT_SECONDS" in e and "'not_a_float'" in e for e in errors)

    def test_unset_vars_pass(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("KERN_")}
        with patch.dict(os.environ, env, clear=True):
            errors = validate_env_types()
            assert not errors

    def test_multiple_invalid_vars(self):
        overrides = {
            "KERN_LLM_MAX_TOKENS": "bad",
            "KERN_PROMPT_CACHE_SIZE": "worse",
        }
        with patch.dict(os.environ, overrides, clear=False):
            errors = validate_env_types()
            assert len([e for e in errors if "KERN_LLM_MAX_TOKENS" in e]) == 1
            assert len([e for e in errors if "KERN_PROMPT_CACHE_SIZE" in e]) == 1


# ---------------------------------------------------------------------------
# validate_settings — multiple errors at once
# ---------------------------------------------------------------------------
class TestMultipleErrors:
    def test_collects_all_errors(self):
        s = FakeSettings(
            product_posture="bad",
            timezone="Invalid/TZ",
            llm_max_tokens=-1,
            rag_min_score=5.0,
        )
        errors = validate_settings(s)
        assert len(errors) >= 4

    def test_valid_default_settings_pass(self):
        s = FakeSettings()
        errors = validate_settings(s)
        assert errors == []


# ---------------------------------------------------------------------------
# Integration: .env.example parses without validation errors
# ---------------------------------------------------------------------------
class TestEnvExample:
    def test_env_example_parses_without_error(self):
        env_example = Path(".env.example")
        assert env_example.exists(), ".env.example must ship with the repo."

        parsed = {}
        for line in env_example.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            # Strip inline comments (matches dotenv behavior)
            if "#" in value:
                value = value[:value.index("#")].strip()
            parsed[key.strip()] = value

        with patch.dict(os.environ, parsed, clear=False):
            errors = validate_env_types()
            assert errors == [], f"Type errors in .env.example: {errors}"


# ---------------------------------------------------------------------------
# Defaults applied when optional variables absent
# ---------------------------------------------------------------------------
class TestDefaults:
    def test_default_settings_have_expected_values(self):
        s = FakeSettings()
        assert s.product_posture == "production"
        assert s.policy_mode == "personal"
        assert s.timezone == "Europe/Berlin"
        assert s.llm_max_tokens == 1024
        assert s.heartbeat_seconds == 2.0
        assert s.rag_min_score == 0.1


class TestConfigNormalization:
    def test_db_encryption_mode_normalizes_legacy_none_alias(self):
        assert _normalize_db_encryption_mode("none") == "off"
        assert _normalize_db_encryption_mode("off") == "off"
        assert _normalize_db_encryption_mode("fernet") == "fernet"
