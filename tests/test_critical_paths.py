"""Unit tests for critical code paths: retention, invoices, quiet hours, KG dates, db_retry, cron."""
from __future__ import annotations

import os
import sqlite3
import time

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── db_retry ──────────────────────────────────────────────────────────

from app.database import db_retry


def test_db_retry_succeeds_first_try():
    assert db_retry(lambda: 42) == 42


def test_db_retry_succeeds_on_second_try():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    with patch("app.database.time.sleep"):
        assert db_retry(flaky) == "ok"
    assert calls["n"] == 2


def test_db_retry_exhausts_all_attempts():
    def always_locked():
        raise sqlite3.OperationalError("database is locked")

    with patch("app.database.time.sleep"):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db_retry(always_locked, max_attempts=3)


def test_db_retry_non_locked_error_raises_immediately():
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: foo")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db_retry(bad)
    assert calls["n"] == 1


def test_db_retry_non_operational_error_raises():
    def bad():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        db_retry(bad)


def test_db_retry_returns_none():
    assert db_retry(lambda: None) is None


def test_db_retry_max_attempts_one():
    def locked():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        db_retry(locked, max_attempts=1)


# ── Invoice arithmetic (_compute_totals) ──────────────────────────────

from app.german_business import GermanBusinessService


@pytest.fixture
def business_service(tmp_path):
    from app.database import connect
    from app.memory import MemoryRepository

    conn = connect(tmp_path / "kern.db")
    memory = MemoryRepository(conn, profile_slug="test")
    platform = MagicMock()
    platform.record_audit = MagicMock()
    profile = MagicMock()
    profile.slug = "test"
    return GermanBusinessService(memory, platform, profile, MagicMock(), MagicMock())


def test_compute_totals_single_item(business_service):
    result = business_service._compute_totals([{"amount": 100}])
    assert result["total_net"] == 100.0
    assert result["vat_rate"] == 0.19
    assert result["vat_amount"] == 19.0
    assert result["total_gross"] == 119.0


def test_compute_totals_multiple_items(business_service):
    items = [{"amount": 33.33}, {"amount": 66.67}, {"amount": 0.01}]
    result = business_service._compute_totals(items)
    assert result["total_net"] == 100.01
    assert result["vat_amount"] == 19.0
    assert result["total_gross"] == 119.01


def test_compute_totals_zero_amount(business_service):
    result = business_service._compute_totals([{"amount": 0}])
    assert result["total_net"] == 0.0
    assert result["vat_amount"] == 0.0
    assert result["total_gross"] == 0.0


def test_compute_totals_vat_exempt(business_service):
    result = business_service._compute_totals([{"amount": 200}], vat_exempt=True)
    assert result["vat_rate"] == 0.0
    assert result["vat_amount"] == 0.0
    assert result["total_gross"] == 200.0
    assert "vat_exempt_note" in result


def test_compute_totals_invalid_amount_skipped(business_service):
    items = [{"amount": 50}, {"amount": "not-a-number"}, {"amount": 30}]
    result = business_service._compute_totals(items)
    assert result["total_net"] == 80.0


def test_compute_totals_empty_list(business_service):
    result = business_service._compute_totals([])
    assert result["total_net"] == 0.0
    assert result["total_gross"] == 0.0


def test_compute_totals_decimal_precision(business_service):
    # Three items that test cent rounding: 33.33 * 3 = 99.99
    items = [{"amount": 33.33}] * 3
    result = business_service._compute_totals(items)
    assert result["total_net"] == 99.99
    # 99.99 * 0.19 = 18.9981 → rounded to 19.00
    assert result["vat_amount"] == 19.0
    assert result["total_gross"] == 118.99


# ── Quiet hours ───────────────────────────────────────────────────────

from app.local_data import LocalDataService


@pytest.fixture
def local_data(tmp_path):
    from app.database import connect
    from app.memory import MemoryRepository

    conn = connect(tmp_path / "kern.db")
    memory = MemoryRepository(conn, profile_slug="test")
    profile = MagicMock()
    profile.slug = "test"
    return LocalDataService(memory, profile)


def test_quiet_hours_not_configured(local_data):
    assert local_data.quiet_hours_active() is False


def test_quiet_hours_start_equals_end(local_data):
    local_data.memory.set_value("preferences", "quiet_hours_start", "22:00")
    local_data.memory.set_value("preferences", "quiet_hours_end", "22:00")
    assert local_data.quiet_hours_active() is False


def test_quiet_hours_normal_range_active(local_data):
    local_data.memory.set_value("preferences", "quiet_hours_start", "09:00")
    local_data.memory.set_value("preferences", "quiet_hours_end", "17:00")
    noon = datetime(2026, 3, 25, 12, 0, 0)
    assert local_data.quiet_hours_active(now=noon) is True


def test_quiet_hours_normal_range_inactive(local_data):
    local_data.memory.set_value("preferences", "quiet_hours_start", "09:00")
    local_data.memory.set_value("preferences", "quiet_hours_end", "17:00")
    evening = datetime(2026, 3, 25, 20, 0, 0)
    assert local_data.quiet_hours_active(now=evening) is False


def test_quiet_hours_midnight_wrap_active(local_data):
    local_data.memory.set_value("preferences", "quiet_hours_start", "22:00")
    local_data.memory.set_value("preferences", "quiet_hours_end", "06:00")
    late = datetime(2026, 3, 25, 23, 0, 0)
    assert local_data.quiet_hours_active(now=late) is True


def test_quiet_hours_midnight_wrap_inactive(local_data):
    local_data.memory.set_value("preferences", "quiet_hours_start", "22:00")
    local_data.memory.set_value("preferences", "quiet_hours_end", "06:00")
    noon = datetime(2026, 3, 25, 12, 0, 0)
    assert local_data.quiet_hours_active(now=noon) is False


def test_quiet_hours_midnight_wrap_early_morning(local_data):
    local_data.memory.set_value("preferences", "quiet_hours_start", "22:00")
    local_data.memory.set_value("preferences", "quiet_hours_end", "06:00")
    early = datetime(2026, 3, 25, 3, 0, 0)
    assert local_data.quiet_hours_active(now=early) is True


def test_quiet_hours_at_start_boundary(local_data):
    local_data.memory.set_value("preferences", "quiet_hours_start", "09:00")
    local_data.memory.set_value("preferences", "quiet_hours_end", "17:00")
    start = datetime(2026, 3, 25, 9, 0, 0)
    assert local_data.quiet_hours_active(now=start) is True


# ── Knowledge graph date normalization ────────────────────────────────

from app.knowledge_graph import KnowledgeGraphService


@pytest.fixture
def kg_service(tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "kern.db")
    conn.execute("CREATE TABLE IF NOT EXISTS knowledge_entities (id TEXT PRIMARY KEY, profile_slug TEXT, entity_type TEXT, name TEXT, display_name TEXT, metadata_json TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS knowledge_edges (id TEXT PRIMARY KEY, profile_slug TEXT, source_id TEXT, target_id TEXT, relationship TEXT, weight REAL, metadata_json TEXT, source_document_id TEXT, created_at TEXT)")
    conn.commit()
    return KnowledgeGraphService(conn, "test")


def test_normalize_date_iso(kg_service):
    assert kg_service._normalize_date("2026-03-25") == "2026-03-25"


def test_normalize_date_german_format(kg_service):
    assert kg_service._normalize_date("25.03.2026") == "2026-03-25"


def test_normalize_date_slash_format(kg_service):
    assert kg_service._normalize_date("25/03/2026") == "2026-03-25"


def test_normalize_date_two_digit_year_low(kg_service):
    assert kg_service._normalize_date("15.06.24") == "2024-06-15"


def test_normalize_date_two_digit_year_high(kg_service):
    assert kg_service._normalize_date("15.06.98") == "1998-06-15"


def test_normalize_date_two_digit_year_boundary(kg_service):
    assert kg_service._normalize_date("01.01.49") == "2049-01-01"
    assert kg_service._normalize_date("01.01.50") == "1950-01-01"


def test_normalize_date_invalid_string(kg_service):
    assert kg_service._normalize_date("not-a-date") == "not-a-date"


# ── Cron validation ──────────────────────────────────────────────────

from app.scheduler import SchedulerService, _parse_cron_expression


def test_cron_valid_expression():
    result = _parse_cron_expression("0 9 * * *")
    assert len(result) == 5
    assert 0 in result[0][0]  # minute 0
    assert 9 in result[1][0]  # hour 9


def test_cron_invalid_too_many_fields():
    with pytest.raises(ValueError, match="5 fields"):
        _parse_cron_expression("* * * * * *")


def test_cron_invalid_empty():
    with pytest.raises(ValueError):
        _parse_cron_expression("")


def test_cron_invalid_characters():
    with pytest.raises(ValueError):
        _parse_cron_expression("abc def ghi jkl mno")


def test_cron_invalid_minute_range():
    with pytest.raises(ValueError):
        _parse_cron_expression("60 * * * *")


def test_cron_invalid_hour_range():
    with pytest.raises(ValueError):
        _parse_cron_expression("* 25 * * *")


def test_scheduler_validate_cron(tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "sched.db")
    conn.execute("CREATE TABLE IF NOT EXISTS scheduled_tasks (id TEXT PRIMARY KEY, profile_slug TEXT, title TEXT, cron_expression TEXT, action_type TEXT, action_payload_json TEXT, enabled INTEGER, next_run_at TEXT, created_at TEXT, updated_at TEXT, run_status TEXT, failure_count INTEGER, retry_attempts INTEGER, max_retries INTEGER, last_result_json TEXT, run_started_at TEXT, last_error TEXT)")
    conn.commit()
    svc = SchedulerService(conn, "test")
    result = svc.validate_cron_expression("0 9 * * *")
    assert result["valid"] is True
    assert "next_run_at" in result


def test_scheduler_validate_cron_invalid(tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "sched.db")
    conn.execute("CREATE TABLE IF NOT EXISTS scheduled_tasks (id TEXT PRIMARY KEY, profile_slug TEXT, title TEXT, cron_expression TEXT, action_type TEXT, action_payload_json TEXT, enabled INTEGER, next_run_at TEXT, created_at TEXT, updated_at TEXT, run_status TEXT, failure_count INTEGER, retry_attempts INTEGER, max_retries INTEGER, last_result_json TEXT, run_started_at TEXT, last_error TEXT)")
    conn.commit()
    svc = SchedulerService(conn, "test")
    with pytest.raises(ValueError):
        svc.validate_cron_expression("bad cron")


# ── Retention _delete_path ────────────────────────────────────────────

def test_delete_path_none():
    svc = MagicMock(spec=["_delete_path", "_current_failures"])
    svc._current_failures = 0
    from app.retention import RetentionService
    RetentionService._delete_path(svc, None)
    # No error raised


def test_delete_path_existing_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("data")
    svc = MagicMock()
    svc._current_failures = 0
    from app.retention import RetentionService
    RetentionService._delete_path(svc, str(f))
    assert not f.exists()


def test_delete_path_missing_file(tmp_path):
    svc = MagicMock()
    svc._current_failures = 0
    from app.retention import RetentionService
    RetentionService._delete_path(svc, str(tmp_path / "nope.txt"))
    # No error, file just logged as already deleted


def test_delete_path_permission_error(tmp_path):
    f = tmp_path / "locked.txt"
    f.write_text("data")
    svc = MagicMock()
    svc._current_failures = 0
    from app.retention import RetentionService
    with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
        RetentionService._delete_path(svc, str(f))
    assert svc._current_failures == 1


# ── Fuzzy matching with umlauts ───────────────────────────────────────

from app.knowledge_graph import _fuzzy_match, _normalize_umlaut


def test_fuzzy_match_identical():
    assert _fuzzy_match("hello", "hello") is True


def test_fuzzy_match_umlaut_equivalent():
    assert _fuzzy_match("Müller", "Mueller") is True


def test_fuzzy_match_different():
    assert _fuzzy_match("apple", "banana") is False


def test_normalize_umlaut():
    assert _normalize_umlaut("Ärger") == "aerger"
    assert _normalize_umlaut("Über") == "ueber"
    assert _normalize_umlaut("Straße") == "strasse"
