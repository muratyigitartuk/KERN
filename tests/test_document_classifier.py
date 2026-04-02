"""Tests for document_classifier module."""
from __future__ import annotations

import os

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.document_classifier import classify_document, classify_document_llm


# ── Keyword classification ───────────────────────────────────────────


def test_classify_empty():
    result = classify_document("")
    assert result["classification"] == "internal"
    assert result["method"] == "default"


def test_classify_confidential_single_hit():
    result = classify_document("Dieses Dokument ist vertraulich.")
    assert result["classification"] == "confidential"


def test_classify_confidential_multiple_hits():
    result = classify_document("Personenbezogene Daten gemäß DSGVO. Gehaltsinformationen enthalten.")
    assert result["classification"] == "confidential"
    assert result["confidence"] >= 0.8


def test_classify_restricted():
    result = classify_document("STRENG GEHEIM — Nur für den Dienstgebrauch.")
    assert result["classification"] == "restricted"


def test_classify_public():
    result = classify_document("Pressemitteilung: Unternehmen gibt neues Produkt bekannt.")
    assert result["classification"] == "public"


def test_classify_internal_default():
    result = classify_document("Quartalsbericht Q1 2026 für die Geschäftsleitung.")
    assert result["classification"] == "internal"


def test_classify_restricted_takes_priority():
    text = "Verschlusssache. Pressemitteilung im Anhang."
    result = classify_document(text)
    assert result["classification"] == "restricted"


def test_classify_custom_default():
    result = classify_document("Some generic text.", default="public")
    assert result["classification"] == "public"


# ── LLM classification ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_llm_unavailable():
    llm = MagicMock()
    llm.available = False
    result = await classify_document_llm("Vertraulich.", llm)
    assert result["classification"] == "confidential"
    assert result["method"] == "keyword"


@pytest.mark.asyncio
async def test_classify_llm_success():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={
        "choices": [{"message": {"content": "confidential"}}]
    })
    result = await classify_document_llm("Some document text.", llm)
    assert result["classification"] == "confidential"
    assert result["method"] == "llm"


@pytest.mark.asyncio
async def test_classify_llm_invalid_response_falls_back():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(return_value={
        "choices": [{"message": {"content": "maybe confidential, I'm not sure"}}]
    })
    result = await classify_document_llm("Pressemitteilung.", llm)
    assert result["method"] == "keyword"  # falls back
    assert result["classification"] == "public"


@pytest.mark.asyncio
async def test_classify_llm_error_falls_back():
    llm = MagicMock()
    llm.available = True
    llm.chat = AsyncMock(side_effect=Exception("LLM down"))
    result = await classify_document_llm("Normal text.", llm)
    assert result["method"] == "default"
