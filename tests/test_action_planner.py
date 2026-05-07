"""Tests for ActionPlanner contextual payload generation."""
from __future__ import annotations

import os

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.action_planner import ActionPlanner


@pytest.fixture
def planner():
    return ActionPlanner()


# â”€â”€ Context extraction â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_extract_context_inbox(planner):
    alert = {
        "type": "inbox",
        "samples": [
            {
                "sender": "Hans MÃ¼ller <hans@firma.de>",
                "subject": "Rechnung Nr. 2026-001",
                "body_preview": "Bitte Ã¼berweisen Sie 1.500,00 EUR bis 01.04.2026.",
            }
        ],
    }
    ctx = planner._extract_context(alert)
    assert "Hans MÃ¼ller" in ctx["names"]
    assert ctx["subject"] == "Rechnung Nr. 2026-001"
    assert any("01.04.2026" in d for d in ctx["dates"])
    assert any("EUR" in a for a in ctx["amounts"])


def test_extract_context_document(planner):
    alert = {
        "type": "document",
        "documents": [
            {"title": "Vertrag ABC GmbH", "due_date": "2026-04-15"},
        ],
    }
    ctx = planner._extract_context(alert)
    assert "Vertrag ABC GmbH" in ctx["references"]
    assert "2026-04-15" in ctx["dates"]


def test_extract_context_calendar(planner):
    alert = {
        "type": "calendar",
        "event_title": "Projektbesprechung",
        "starts_at": "2026-04-01T10:00:00+00:00",
    }
    ctx = planner._extract_context(alert)
    assert "Projektbesprechung" in ctx["references"]
    assert any("2026-04-01" in d for d in ctx["dates"])


def test_extract_context_empty(planner):
    ctx = planner._extract_context({})
    assert ctx["names"] == []
    assert ctx["dates"] == []
    assert ctx["amounts"] == []
    assert ctx["references"] == []


# â”€â”€ Contextual email payload â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.skip(reason="legacy email drafting surface intentionally removed")
def test_contextual_email_with_sender(planner):
    alert = {
        "type": "inbox",
        "samples": [
            {
                "sender": "Anna Schmidt <anna@firma.de>",
                "subject": "Angebot fÃ¼r Projekt",
                "body_preview": "Das Angebot betrÃ¤gt 5.000 EUR.",
            }
        ],
    }
    payload = planner.build_contextual_payload("draft_email", alert)
    assert "anna@firma.de" in str(payload.get("to", []))
    assert "Re:" in payload["subject"]
    assert "Anna Schmidt" in payload["body"]
    assert "5.000 EUR" in payload["body"]
    assert payload["generated_by"] == "template"


@pytest.mark.skip(reason="legacy email drafting surface intentionally removed")
def test_contextual_email_no_sender(planner):
    alert = {"type": "document", "documents": [{"title": "Budget 2026"}]}
    payload = planner.build_contextual_payload("draft_email", alert)
    assert payload["to"] == []
    assert "Sehr geehrte Damen und Herren" in payload["body"]


# â”€â”€ Contextual reminder payload â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_contextual_reminder_with_date(planner):
    future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
    alert = {
        "type": "document",
        "documents": [{"title": "Vertrag", "due_date": future}],
    }
    payload = planner.build_contextual_payload("create_reminder", alert)
    assert "Nachfassen" in payload["title"]
    assert "Vertrag" in payload["title"]
    assert "due_at" in payload
    assert payload["generated_by"] == "template"


def test_contextual_reminder_no_date(planner):
    alert = {"type": "inbox", "samples": [{"subject": "Test"}]}
    payload = planner.build_contextual_payload("create_reminder", alert)
    assert "due_at" in payload


# â”€â”€ Contextual task payload â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_contextual_task(planner):
    alert = {
        "type": "file_watch",
        "document_title": "Report Q1",
        "evidence": ["Erstellt am 15.03.2026"],
    }
    payload = planner.build_contextual_payload("create_task", alert)
    assert "Prüfen" in payload["title"]
    assert "15.03.2026" in payload["title"]


# â”€â”€ Contextual letter payload â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_contextual_letter(planner):
    alert = {
        "type": "inbox",
        "samples": [{"sender": "Max Weber <max@behoerde.de>", "subject": "Antrag 456"}],
    }
    payload = planner.build_contextual_payload("draft_letter", alert)
    assert payload["recipient_name"] == "Max Weber"
    assert payload["subject"] == "Antrag 456"


# â”€â”€ LLM prompt building â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.skip(reason="legacy email drafting surface intentionally removed")
def test_build_llm_prompt_email(planner):
    alert = {
        "type": "inbox",
        "message": "2 new emails",
        "samples": [{"sender": "Test <t@x.de>", "subject": "Q1 Report"}],
    }
    ctx = planner._extract_context(alert)
    prompt = planner._build_llm_prompt("draft_email", alert, ctx)
    assert "email" in prompt.lower()
    assert "German" in prompt
    assert "Q1 Report" in prompt


def test_build_llm_prompt_reminder(planner):
    alert = {"type": "calendar", "event_title": "Meeting"}
    ctx = planner._extract_context(alert)
    prompt = planner._build_llm_prompt("create_reminder", alert, ctx)
    assert "reminder" in prompt.lower()
    assert "Meeting" in prompt


# â”€â”€ LLM payload generation (async) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_contextual_payload_llm_unavailable(planner):
    llm = MagicMock()
    llm.available = False
    alert = {"type": "inbox", "samples": [{"sender": "a@b.de", "subject": "Test"}]}
    result = await planner.build_contextual_payload_llm("create_reminder", alert, llm)
    assert result["generated_by"] == "template"


@pytest.mark.asyncio
async def test_contextual_payload_llm_success(planner):
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={
        "choices": [{"message": {"content": "Vielen Dank fÃ¼r Ihre Nachricht. Ich werde den Vorgang prÃ¼fen."}}]
    })
    alert = {"type": "inbox", "samples": [{"sender": "a@b.de", "subject": "Test"}]}
    result = await planner.build_contextual_payload_llm("create_reminder", alert, llm)
    assert result["generated_by"] == "llm"
    assert "Vielen Dank" in str(result)


@pytest.mark.asyncio
async def test_contextual_payload_llm_error_falls_back(planner):
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(side_effect=Exception("LLM error"))
    alert = {"type": "inbox", "samples": [{"sender": "a@b.de", "subject": "Test"}]}
    result = await planner.build_contextual_payload_llm("create_reminder", alert, llm)
    assert result["generated_by"] == "template"


# â”€â”€ Date and amount extraction â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_extract_dates(planner):
    dates = planner._extract_dates("Frist: 01.04.2026, ISO: 2026-04-01, US: 04/01/2026")
    assert "01.04.2026" in dates
    assert "2026-04-01" in dates
    assert "04/01/2026" in dates


def test_extract_amounts(planner):
    amounts = planner._extract_amounts("Total: 1.500,00 EUR and $200")
    assert any("EUR" in a for a in amounts)
    assert any("$" in a for a in amounts)


def test_extract_amounts_empty(planner):
    assert planner._extract_amounts("no money here") == []
