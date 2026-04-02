"""Verify locale JSON files have full parity between en and de."""
from __future__ import annotations

import json
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "locales"

# Keys that are intentionally identical in German (loan words, proper nouns, technical terms)
ALLOWED_IDENTICAL = {
    "status.offline",
    "plan.group",
    "ops.audio",
    "audit.audit",
    "settings.audit_label",
    "settings.backend",
    "chat.kern",
    "schedules.cron_placeholder",
}


def _load(lang: str) -> dict[str, str]:
    with open(LOCALES_DIR / f"{lang}.json", encoding="utf-8") as f:
        return json.load(f)


def test_de_has_all_en_keys():
    en = _load("en")
    de = _load("de")
    missing = sorted(k for k in en if k not in de)
    assert not missing, f"Keys in en.json missing from de.json: {missing}"


def test_en_has_all_de_keys():
    en = _load("en")
    de = _load("de")
    extra = sorted(k for k in de if k not in en)
    assert not extra, f"Keys in de.json not in en.json: {extra}"


def test_de_values_not_empty():
    de = _load("de")
    empty = sorted(k for k, v in de.items() if not v.strip())
    assert not empty, f"Empty values in de.json: {empty}"


def test_de_values_are_translated():
    en = _load("en")
    de = _load("de")
    untranslated = sorted(
        k for k in en
        if k in de and en[k] == de[k] and k not in ALLOWED_IDENTICAL
    )
    assert not untranslated, f"Untranslated (identical to EN) in de.json: {untranslated}"
