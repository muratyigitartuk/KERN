"""Document classification for KERN.

Classifies documents into: public, internal, confidential, restricted.
Uses keyword-based heuristic with optional LLM upgrade path.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.llm_client import LlamaServerClient

logger = logging.getLogger(__name__)

CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
DEFAULT_CLASSIFICATION = "internal"

_CONFIDENTIAL_PATTERNS = [
    re.compile(r"\b(?:vertraulich|confidential|geheim|streng\s+vertraulich)", re.IGNORECASE),
    re.compile(r"\b(?:personenbezogen|personal\s+data|datenschutz|DSGVO|GDPR)\b", re.IGNORECASE),
    re.compile(r"\b(?:gehalt|salary|compensation|vergÃ¼tung|bonus)", re.IGNORECASE),
    re.compile(r"\b(?:kÃ¼ndigung|termination|abmahnung|disciplinary)", re.IGNORECASE),
]

_RESTRICTED_PATTERNS = [
    re.compile(r"\b(?:streng\s+geheim|top\s+secret|nur\s+fÃ¼r\s+den\s+dienstgebrauch)\b", re.IGNORECASE),
    re.compile(r"\b(?:restricted|classified|verschlusssache)\b", re.IGNORECASE),
]

_PUBLIC_PATTERNS = [
    re.compile(r"\b(?:pressemitteilung|press\s+release|Ã¶ffentlich|public)\b", re.IGNORECASE),
    re.compile(r"\b(?:verÃ¶ffentlichung|publication|newsletter)\b", re.IGNORECASE),
]


def classify_document(text: str, *, default: str = DEFAULT_CLASSIFICATION) -> dict[str, str | float]:
    """Classify a document based on keyword patterns.

    Returns a dict with 'classification', 'confidence', and 'method'.
    When uncertain, defaults to 'internal' (safe default for corporate use).
    """
    if not text or not text.strip():
        return {"classification": default, "confidence": 0.3, "method": "default"}

    text_lower = text[:5000].lower()

    # Check restricted first (highest sensitivity)
    for pattern in _RESTRICTED_PATTERNS:
        if pattern.search(text_lower):
            return {"classification": "restricted", "confidence": 0.85, "method": "keyword"}

    # Check confidential
    confidential_hits = sum(1 for p in _CONFIDENTIAL_PATTERNS if p.search(text_lower))
    if confidential_hits >= 2:
        return {"classification": "confidential", "confidence": 0.82, "method": "keyword"}
    if confidential_hits == 1:
        return {"classification": "confidential", "confidence": 0.65, "method": "keyword"}

    # Check public
    for pattern in _PUBLIC_PATTERNS:
        if pattern.search(text_lower):
            return {"classification": "public", "confidence": 0.72, "method": "keyword"}

    # Default to internal when uncertain
    return {"classification": default, "confidence": 0.5, "method": "default"}


async def classify_document_llm(
    text: str,
    llm: "LlamaServerClient",
    *,
    default: str = DEFAULT_CLASSIFICATION,
) -> dict[str, str | float]:
    """Classify using LLM with keyword fallback."""
    if not llm.available:
        return classify_document(text, default=default)

    prompt_text = text[:3000] if len(text) > 3000 else text
    try:
        response = await llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Classify this document into exactly one category: public, internal, confidential, restricted. "
                        "Respond with only the category name, nothing else. "
                        "If unsure, choose 'internal' as the safe default."
                    ),
                },
                {"role": "user", "content": prompt_text},
            ],
            max_tokens=20,
            temperature=0.0,
        )
        content = str((response.get("choices") or [{}])[0].get("message", {}).get("content", "")).strip().lower()
        if content in CLASSIFICATIONS:
            return {"classification": content, "confidence": 0.88, "method": "llm"}
    except Exception as exc:
        logger.debug("LLM classification failed: %s", exc)

    return classify_document(text, default=default)
