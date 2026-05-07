"""Tests for ActionPlanner contextual payload generation."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from app.action_planner import ActionPlanner


@pytest.fixture
def planner() -> ActionPlanner:
    return ActionPlanner()


def test_extract_context_inbox(planner: ActionPlanner) -> None:
    alert = {
        "type": "inbox",
        "samples": [
            {
                "sender": "Hans Mueller <hans@firma.de>",
                "subject": "Rechnung Nr. 2026-001",
                "body_preview": "Bitte ueberweisen Sie 1.500,00 EUR bis 01.04.2026.",
            }
        ],
    }
    ctx = planner._extract_context(alert)
    assert "Hans Mueller" in ctx["names"]
    assert ctx["subject"] == "Rechnung Nr. 2026-001"
    assert any("01.04.2026" in d for d in ctx["dates"])
    assert any("EUR" in a for a in ctx["amounts"])


def test_extract_context_document(planner: ActionPlanner) -> None:
    alert = {
        "type": "document",
        "documents": [
            {"title": "Vertrag ABC GmbH", "due_date": "2026-04-15"},
        ],
    }
    ctx = planner._extract_context(alert)
    assert "Vertrag ABC GmbH" in ctx["references"]
    assert "2026-04-15" in ctx["dates"]


def test_extract_context_calendar(planner: ActionPlanner) -> None:
    alert = {
        "type": "calendar",
        "event_title": "Projektbesprechung",
        "starts_at": "2026-04-01T10:00:00+00:00",
    }
    ctx = planner._extract_context(alert)
    assert "Projektbesprechung" in ctx["references"]
    assert any("2026-04-01" in d for d in ctx["dates"])


def test_extract_context_empty(planner: ActionPlanner) -> None:
    ctx = planner._extract_context({})
    assert ctx["names"] == []
    assert ctx["dates"] == []
    assert ctx["amounts"] == []
    assert ctx["references"] == []


def test_contextual_reminder_with_date(planner: ActionPlanner) -> None:
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


def test_contextual_reminder_no_date(planner: ActionPlanner) -> None:
    alert = {"type": "inbox", "samples": [{"subject": "Test"}]}
    payload = planner.build_contextual_payload("create_reminder", alert)
    assert "due_at" in payload


def test_contextual_task(planner: ActionPlanner) -> None:
    alert = {
        "type": "file_watch",
        "document_title": "Report Q1",
        "evidence": ["Erstellt am 15.03.2026"],
    }
    payload = planner.build_contextual_payload("create_task", alert)
    assert "Report Q1" in payload["title"]
    assert "15.03.2026" in payload["title"]
    assert payload["generated_by"] == "template"


def test_contextual_letter(planner: ActionPlanner) -> None:
    alert = {
        "type": "inbox",
        "samples": [{"sender": "Max Weber <max@behoerde.de>", "subject": "Antrag 456"}],
    }
    payload = planner.build_contextual_payload("draft_letter", alert)
    assert payload["recipient_name"] == "Max Weber"
    assert payload["subject"] == "Antrag 456"


def test_build_llm_prompt_reminder(planner: ActionPlanner) -> None:
    alert = {"type": "calendar", "event_title": "Meeting"}
    ctx = planner._extract_context(alert)
    prompt = planner._build_llm_prompt("create_reminder", alert, ctx)
    assert "reminder" in prompt.lower()
    assert "Meeting" in prompt


@pytest.mark.asyncio
async def test_contextual_payload_llm_unavailable(planner: ActionPlanner) -> None:
    llm = MagicMock()
    llm.available = False
    alert = {"type": "inbox", "samples": [{"sender": "a@b.de", "subject": "Test"}]}
    result = await planner.build_contextual_payload_llm("create_reminder", alert, llm)
    assert result["generated_by"] == "template"


@pytest.mark.asyncio
async def test_contextual_payload_llm_success(planner: ActionPlanner) -> None:
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(
        return_value={
            "choices": [
                {
                    "message": {
                        "content": "Vielen Dank fuer Ihre Nachricht. Ich pruefe den Vorgang."
                    }
                }
            ]
        }
    )
    alert = {"type": "inbox", "samples": [{"sender": "a@b.de", "subject": "Test"}]}
    result = await planner.build_contextual_payload_llm("create_reminder", alert, llm)
    assert result["generated_by"] == "llm"
    assert "Vielen Dank" in str(result)


@pytest.mark.asyncio
async def test_contextual_payload_llm_error_falls_back(planner: ActionPlanner) -> None:
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(side_effect=Exception("LLM error"))
    alert = {"type": "inbox", "samples": [{"sender": "a@b.de", "subject": "Test"}]}
    result = await planner.build_contextual_payload_llm("create_reminder", alert, llm)
    assert result["generated_by"] == "template"


def test_extract_dates(planner: ActionPlanner) -> None:
    dates = planner._extract_dates("Frist: 01.04.2026, ISO: 2026-04-01, US: 04/01/2026")
    assert "01.04.2026" in dates
    assert "2026-04-01" in dates
    assert "04/01/2026" in dates


def test_extract_amounts(planner: ActionPlanner) -> None:
    amounts = planner._extract_amounts("Total: 1.500,00 EUR and $200")
    assert any("EUR" in a for a in amounts)
    assert any("$" in a for a in amounts)


def test_extract_amounts_empty(planner: ActionPlanner) -> None:
    assert planner._extract_amounts("no money here") == []
