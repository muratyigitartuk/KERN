"""Adversarial / negative tests: malformed input, edge cases, large data."""
from __future__ import annotations

import os
import sqlite3

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from unittest.mock import MagicMock

import pytest


# ── Invalid cron expressions ──────────────────────────────────────────

from app.scheduler import _parse_cron_expression


def test_cron_six_fields():
    with pytest.raises(ValueError, match="5 fields"):
        _parse_cron_expression("* * * * * *")


def test_cron_single_word():
    with pytest.raises(ValueError):
        _parse_cron_expression("abc")


def test_cron_empty_string():
    with pytest.raises(ValueError):
        _parse_cron_expression("")


def test_cron_minute_out_of_range():
    with pytest.raises(ValueError):
        _parse_cron_expression("60 * * * *")


def test_cron_hour_out_of_range():
    with pytest.raises(ValueError):
        _parse_cron_expression("* 25 * * *")


def test_cron_month_out_of_range():
    with pytest.raises(ValueError):
        _parse_cron_expression("* * * 13 *")


def test_cron_very_long_string():
    with pytest.raises(ValueError):
        _parse_cron_expression("a " * 500)


# ── Knowledge graph edge cases ────────────────────────────────────────

from app.knowledge_graph import KnowledgeGraphService, _fuzzy_match


@pytest.fixture
def kg(tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "kg.db")
    conn.execute("CREATE TABLE IF NOT EXISTS knowledge_entities (id TEXT PRIMARY KEY, profile_slug TEXT, entity_type TEXT, name TEXT, display_name TEXT, metadata_json TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS knowledge_edges (id TEXT PRIMARY KEY, profile_slug TEXT, source_id TEXT, target_id TEXT, relationship TEXT, weight REAL, metadata_json TEXT, source_document_id TEXT, created_at TEXT)")
    conn.commit()
    return KnowledgeGraphService(conn, "test")


def test_extract_empty_string(kg):
    result = kg.extract_from_text("")
    assert result == {}


def test_extract_no_entities(kg):
    result = kg.extract_from_text("this is just a plain sentence with nothing special")
    # Should not crash; may or may not find anything
    assert isinstance(result, dict)


def test_extract_only_special_chars(kg):
    result = kg.extract_from_text("!@#$%^&*()_+-=[]{}|;':\",./<>?")
    assert isinstance(result, dict)


def test_extract_large_text(kg):
    text = "Acme GmbH signed contract on 15.03.2026. " * 500
    result = kg.extract_from_text(text)
    assert isinstance(result, dict)
    assert "company" in result or "date" in result


def test_extract_text_with_german_entities(kg):
    text = "Die Müller GmbH hat am 25.03.2026 eine Rechnung über 1500 EUR ausgestellt."
    result = kg.extract_from_text(text)
    assert isinstance(result, dict)


def test_fuzzy_match_completely_different():
    assert _fuzzy_match("apple", "banana") is False


def test_fuzzy_match_empty_strings():
    assert _fuzzy_match("", "") is True  # identical


def test_fuzzy_match_one_empty():
    assert _fuzzy_match("hello", "") is False


def test_infer_event_invoice(kg):
    assert kg._infer_event_name("Please see attached invoice") == "invoice"
    assert kg._infer_event_name("Rechnung Nr. 12345") == "invoice"


def test_infer_event_contract(kg):
    assert kg._infer_event_name("New agreement signed") == "contract"
    assert kg._infer_event_name("Vertrag unterzeichnet") == "contract"


def test_infer_event_none(kg):
    assert kg._infer_event_name("The weather is nice today") is None


# ── Database adversarial ──────────────────────────────────────────────

from app.database import db_retry
from unittest.mock import patch


def test_db_retry_value_error_no_retry():
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        db_retry(bad)
    assert calls["n"] == 1


def test_db_retry_max_one_attempt():
    calls = {"n": 0}

    def locked():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        db_retry(locked, max_attempts=1)
    assert calls["n"] == 1


def test_db_retry_returns_none_value():
    assert db_retry(lambda: None) is None


def test_db_retry_returns_empty_list():
    assert db_retry(lambda: []) == []


# ── Upload filename validation ────────────────────────────────────────

from app.routes import _validate_upload_filename


def test_validate_filename_path_traversal():
    # Path separators are blocked
    err = _validate_upload_filename("../../../etc/passwd")
    assert err is not None
    assert "path separator" in err.lower()


def test_validate_filename_backslash_traversal():
    err = _validate_upload_filename("..\\..\\etc\\passwd")
    assert err is not None


def test_validate_filename_null_bytes():
    err = _validate_upload_filename("file\x00.pdf")
    assert err is not None
    assert "null" in err.lower()


def test_validate_filename_empty():
    err = _validate_upload_filename("")
    assert err is not None


def test_validate_filename_no_extension():
    err = _validate_upload_filename("noextension")
    assert err is not None


def test_validate_filename_exe_blocked():
    err = _validate_upload_filename("payload.exe")
    assert err is not None


def test_validate_filename_double_extension():
    err = _validate_upload_filename("report.exe.pdf")
    assert err is not None
    assert "double extension" in err.lower()


def test_validate_filename_valid_pdf():
    err = _validate_upload_filename("report.pdf")
    assert err is None


def test_validate_filename_valid_docx():
    err = _validate_upload_filename("document.docx")
    assert err is None


# ── Scheduler validate_cron_expression through service ────────────────

from app.scheduler import SchedulerService


@pytest.fixture
def scheduler(tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "sched.db")
    conn.execute("CREATE TABLE IF NOT EXISTS scheduled_tasks (id TEXT PRIMARY KEY, profile_slug TEXT, title TEXT, cron_expression TEXT, action_type TEXT, action_payload_json TEXT, enabled INTEGER, next_run_at TEXT, created_at TEXT, updated_at TEXT, run_status TEXT, failure_count INTEGER, retry_attempts INTEGER, max_retries INTEGER, last_result_json TEXT, run_started_at TEXT, last_error TEXT)")
    conn.commit()
    return SchedulerService(conn, "test")


def test_scheduler_invalid_cron_raises(scheduler):
    with pytest.raises(ValueError):
        scheduler.validate_cron_expression("not valid cron")


def test_scheduler_valid_cron_returns_dict(scheduler):
    result = scheduler.validate_cron_expression("30 8 * * 1")
    assert result["valid"] is True
    assert "next_run_at" in result


# ── KG LLM extraction ───────────────────────────────────────────────

from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_kg_llm_extraction_unavailable_falls_back(kg):
    llm = MagicMock()
    llm.available = False
    result = await kg.extract_from_text_llm("Acme GmbH signed on 15.03.2026", llm_client=llm)
    assert isinstance(result, dict)
    assert "company" in result or "date" in result


@pytest.mark.asyncio
async def test_kg_llm_extraction_merges_with_regex(kg):
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={
        "choices": [{"message": {"content": '{"persons": ["Hans Müller"], "companies": ["Acme GmbH"], "dates": ["2026-03-15"], "amounts": ["1500 EUR"]}'}}]
    })
    result = await kg.extract_from_text_llm(
        "Hans Müller von Acme GmbH hat am 15.03.2026 eine Rechnung über 1500 EUR gestellt.",
        llm_client=llm,
    )
    assert isinstance(result, dict)
    # LLM should find person + company, regex should also contribute
    assert "person" in result or "company" in result


@pytest.mark.asyncio
async def test_kg_llm_extraction_error_falls_back(kg):
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(side_effect=Exception("LLM error"))
    result = await kg.extract_from_text_llm("Acme GmbH signed on 15.03.2026", llm_client=llm)
    assert isinstance(result, dict)
