"""Tests for German document entity extraction: companies, dates, amounts, locations, addresses."""
from __future__ import annotations

import os

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

import pytest

from app.knowledge_graph import KnowledgeGraphService


@pytest.fixture
def kg(tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "kg.db")
    conn.execute("CREATE TABLE IF NOT EXISTS knowledge_entities (id TEXT PRIMARY KEY, profile_slug TEXT, entity_type TEXT, name TEXT, display_name TEXT, metadata_json TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS knowledge_edges (id TEXT PRIMARY KEY, profile_slug TEXT, source_id TEXT, target_id TEXT, relationship TEXT, weight REAL, metadata_json TEXT, source_document_id TEXT, created_at TEXT)")
    conn.commit()
    return KnowledgeGraphService(conn, "test")


# ── German company names ─────────────────────────────────────────────


def test_extract_gmbh(kg):
    result = kg.extract_from_text("Die Müller GmbH hat den Vertrag unterzeichnet.")
    assert "company" in result


def test_extract_ag(kg):
    result = kg.extract_from_text("Siemens AG liefert die Technik.")
    assert "company" in result


def test_extract_kg_company(kg):
    result = kg.extract_from_text("Schmidt KG bestellt 100 Einheiten.")
    assert "company" in result


def test_extract_ohg(kg):
    result = kg.extract_from_text("Bauer OHG ist Lieferant.")
    assert "company" in result


def test_extract_ev(kg):
    result = kg.extract_from_text("Förderverein e.V. organisiert die Veranstaltung.")
    assert "company" in result


def test_extract_ug(kg):
    result = kg.extract_from_text("StartupXYZ UG wurde gegründet.")
    assert "company" in result


def test_extract_kgaa(kg):
    result = kg.extract_from_text("Henkel KGaA veröffentlicht Quartalsbericht.")
    assert "company" in result


def test_extract_gbr(kg):
    result = kg.extract_from_text("Weber GbR bietet Beratung an.")
    assert "company" in result


# ── German date formats ──────────────────────────────────────────────


def test_extract_german_date_dd_mm_yyyy(kg):
    result = kg.extract_from_text("Fällig am 15.04.2026.")
    assert "date" in result


def test_extract_german_date_short_year(kg):
    result = kg.extract_from_text("Erstellt am 01.03.26.")
    assert "date" in result


def test_extract_iso_date(kg):
    result = kg.extract_from_text("Deadline: 2026-04-15.")
    assert "date" in result


# ── Currency extraction including CHF ────────────────────────────────


def test_extract_eur(kg):
    result = kg.extract_from_text("Der Betrag beträgt 1.500,00 EUR.")
    assert "amount" in result


def test_extract_chf(kg):
    result = kg.extract_from_text("Preis: 2.400 CHF für Schweizer Kunden.")
    assert "amount" in result


def test_extract_euro_symbol(kg):
    result = kg.extract_from_text("Kosten: 750€ plus MwSt.")
    assert "amount" in result


# ── German address patterns ──────────────────────────────────────────


def test_extract_strasse_address(kg):
    result = kg.extract_from_text("Senden Sie die Rechnung an Hauptstraße 42.")
    assert "location" in result


def test_extract_weg_address(kg):
    result = kg.extract_from_text("Unser Büro befindet sich am Birkenweg 7.")
    assert "location" in result


def test_extract_platz_address(kg):
    result = kg.extract_from_text("Termin am Marktplatz 1 in München.")
    assert "location" in result


# ── German postal code + city ────────────────────────────────────────


def test_extract_plz_city(kg):
    result = kg.extract_from_text("Adresse: Hauptstraße 42, 80331 München.")
    assert "location" in result


def test_extract_plz_compound_city(kg):
    result = kg.extract_from_text("Lieferung nach 60313 Frankfurt am Main.")
    assert "location" in result


# ── Full German document extraction ──────────────────────────────────


def test_full_german_invoice(kg):
    text = (
        "Rechnung Nr. 2026-042\n"
        "Müller GmbH\n"
        "Hauptstraße 15, 80331 München\n"
        "Rechnungsdatum: 25.03.2026\n"
        "Fällig bis: 15.04.2026\n"
        "Nettobetrag: 3.500,00 EUR\n"
        "MwSt 19%: 665,00 EUR\n"
        "Bruttobetrag: 4.165,00 EUR\n"
    )
    result = kg.extract_from_text(text)
    assert "company" in result
    assert "date" in result
    assert "amount" in result


def test_full_german_contract(kg):
    text = (
        "Vertrag zwischen Schmidt KG und Weber GbR.\n"
        "Vertragsbeginn: 01.04.2026.\n"
        "Vergütung: 12.000 EUR jährlich.\n"
        "Ansprechpartner: Hans Müller.\n"
    )
    result = kg.extract_from_text(text)
    assert "company" in result
    assert "date" in result
    assert "person" in result
    assert "amount" in result
