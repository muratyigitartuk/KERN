from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.action_planner import ActionPlanner
from app.backup import BackupService
from app.attention import DocumentWatcher
from app.database import connect
from app.documents import DocumentService
from app.email_service import EmailService
from app.knowledge_graph import KnowledgeGraphService
from app.local_data import LocalDataService
from app.meetings import MeetingService
from app.memory import MemoryRepository
from app.platform import PlatformStore, connect_platform_db
from app.retrieval import RetrievalService
from app.scheduler import SchedulerService
from app.spreadsheet import SpreadsheetParser
from app.tools.calendar import CalendarService
from app.types import BackupTarget, EmailDraft, EmailMessage, MeetingRecord, ProfileSummary


def _create_profile(tmp_path: Path):
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    memory = MemoryRepository(connect(Path(profile.db_path)))
    local_data = LocalDataService(memory, "sir")
    return platform, profile, memory, local_data


def test_retrieval_service_combines_memory_and_documents(tmp_path: Path):
    _, profile, memory, local_data = _create_profile(tmp_path)
    local_data.remember_fact("tax_mode", "Private business setup")
    document_path = tmp_path / "tax-note.txt"
    document_path.write_text("Tax checklist for private business clients", encoding="utf-8")
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    documents.ingest_document(document_path)
    retrieval = RetrievalService(memory)

    hits = retrieval.retrieve("tax", scope="profile", limit=6)

    assert hits
    assert any(hit.source_type == "memory" for hit in hits)
    assert any(hit.source_type == "document" for hit in hits)
    memory_hit = next(hit for hit in hits if hit.source_type == "memory")
    assert memory_hit.metadata["memory_kind"] == "fact"


def test_retrieval_memory_hits_keep_structured_memory_metadata(tmp_path: Path):
    _, _, memory, local_data = _create_profile(tmp_path)
    local_data.remember_fact(
        "preferred editor",
        "VS Code",
        memory_kind="preference",
        provenance={"origin": "unit_test"},
    )
    retrieval = RetrievalService(memory)

    hits = retrieval.retrieve("working preferences", scope="profile", limit=3)

    assert hits
    memory_hit = next(hit for hit in hits if hit.source_type == "memory")
    assert memory_hit.metadata["memory_kind"] == "preference"
    assert memory_hit.metadata["provenance"]["origin"] == "unit_test"


def test_retrieval_service_prioritizes_more_specific_documents(tmp_path: Path, monkeypatch):
    _, profile, memory, _ = _create_profile(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    (tmp_path / "invoice-a.txt").write_text("Rechnung deadline Steuerunterlagen bis 2026-04-02", encoding="utf-8")
    (tmp_path / "invoice-b.txt").write_text("Allgemeine Notizen zum Projekt", encoding="utf-8")
    documents.ingest_document(tmp_path / "invoice-a.txt")
    documents.ingest_document(tmp_path / "invoice-b.txt")
    monkeypatch.setattr("app.retrieval.settings.rag_enabled", True)
    monkeypatch.setattr("app.retrieval.settings.rag_embed_model", "local-tfidf")
    monkeypatch.setattr("app.retrieval.settings.rag_index_version", "v-specificity")
    retrieval = RetrievalService(memory)

    hits = retrieval.retrieve("rechnung steuerunterlagen deadline", scope="profile", limit=3)

    assert hits
    assert hits[0].metadata["title"] == "invoice-a"
    assert hits[0].metadata["backend"] == "local_tfidf"
    assert hits[0].metadata["classification"] == "finance"


def test_retrieval_service_expands_german_query_terms_to_match_english_structured_fields(tmp_path: Path, monkeypatch):
    _, profile, memory, _ = _create_profile(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    invoice_path = tmp_path / "acme-invoice.txt"
    offer_path = tmp_path / "acme-offer.txt"
    invoice_path.write_text(
        "\n".join(
            [
                "ACME GmbH Invoice",
                "Due date: 2026-04-02",
                "Amount: 2750 EUR",
            ]
        ),
        encoding="utf-8",
    )
    offer_path.write_text(
        "\n".join(
            [
                "ACME GmbH Offer",
                "Target amount: 4850 EUR",
                "Project: UI modernization",
            ]
        ),
        encoding="utf-8",
    )
    documents.ingest_document(invoice_path)
    documents.ingest_document(offer_path)
    monkeypatch.setattr("app.retrieval.settings.rag_enabled", True)
    monkeypatch.setattr("app.retrieval.settings.rag_embed_model", "local-tfidf")
    monkeypatch.setattr("app.retrieval.settings.rag_index_version", "v-german-structured-fields")
    retrieval = RetrievalService(memory)

    hits = retrieval.retrieve("rechnung zielbetrag faellig", scope="profile", limit=4)
    titles = {hit.metadata["title"] for hit in hits}

    assert "acme-invoice" in titles
    assert "acme-offer" in titles


def test_email_service_persists_accounts_and_drafts(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    service = EmailService(
        memory.connection,
        platform,
        profile,
        local_data,
        CalendarService(local_data),
        documents,
        email_address="kern@example.com",
        smtp_host="smtp.example.com",
    )

    account = service.create_or_update_account(
        label="Primary inbox",
        email_address="kern@example.com",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="kern-user",
        password_ref="secret-ref",
    )
    draft = service.save_draft(EmailDraft(to=["client@example.com"], subject="Draft subject", body="Hello there"))

    accounts = service.list_accounts()
    drafts = service.list_drafts()

    assert accounts
    assert account.id in {entry.id for entry in accounts}
    assert drafts
    assert draft.id == drafts[0].id


def test_email_reminder_suggestions_include_rationale_and_due_date(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    service = EmailService(
        memory.connection,
        platform,
        profile,
        local_data,
        CalendarService(local_data),
        documents,
    )
    service.create_or_update_account(
        label="Primary inbox",
        email_address="kern@example.com",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="kern-user",
        password="secret-pass",
        account_id="acct-1",
    )
    message = EmailMessage(
        id=str(uuid4()),
        account_id="acct-1",
        message_id="<msg-1@example.com>",
        subject="Please send the Rechnung by 2026-04-02",
        sender="client@example.com",
        recipients=["kern@example.com"],
        received_at=datetime.utcnow(),
        has_attachments=False,
        folder="INBOX",
        body_preview="Deadline reminder for invoice submission.",
    )
    memory.append_mailbox_message(
        message,
        account_id="acct-1",
        folder="INBOX",
        body_text="Please review the Rechnung and send it by 2026-04-02.",
        message_id=message.message_id,
    )

    suggestions = service.list_reminder_suggestions(limit=3)

    assert suggestions
    assert suggestions[0].due_at.date().isoformat() == "2026-04-02"
    assert "deadline" in suggestions[0].rationale.lower() or "follow-up" in suggestions[0].rationale.lower()

def test_backup_service_can_restore_round_trip(tmp_path: Path):
    service = BackupService()
    profile_root = tmp_path / "profile-root"
    profile_root.mkdir(parents=True, exist_ok=True)
    (profile_root / "state.txt").write_text("kern state", encoding="utf-8")
    profile = ProfileSummary(
        slug="default",
        title="Default",
        profile_root=str(profile_root),
        db_path=str(profile_root / "kern.db"),
        documents_root=str(profile_root / "documents"),
        attachments_root=str(profile_root / "attachments"),
        archives_root=str(profile_root / "archives"),
        meetings_root=str(profile_root / "meetings"),
        backups_root=str(tmp_path / "backups"),
    )
    target = BackupTarget(kind="local_folder", path=str(tmp_path / "backups"), label="Backups")

    backup_path = service.create_encrypted_profile_backup(profile, target, "secret-passphrase")
    restored = service.restore_encrypted_profile_backup(backup_path, "secret-passphrase", tmp_path / "restore")

    assert Path(backup_path).exists()
    assert (restored / "state.txt").read_text(encoding="utf-8") == "kern state"


def test_scheduler_records_failure_and_retry_state(tmp_path: Path):
    _, profile, memory, _ = _create_profile(tmp_path)
    scheduler = SchedulerService(memory.connection, profile.slug)
    task = scheduler.create_task("Mail summary", "0 9 * * *", "summarize_emails", {}, max_retries=2)

    first = scheduler.record_failure(task["id"], "imap timeout")
    second = scheduler.record_failure(task["id"], "imap timeout again")
    row = scheduler.list_tasks()[0]

    assert first["status"] == "retrying"
    assert second["status"] in {"retrying", "failed"}
    assert row["failure_count"] >= 2
    assert row["last_error"] == "imap timeout again"


def test_knowledge_graph_normalizes_company_aliases(tmp_path: Path):
    _, profile, memory, _ = _create_profile(tmp_path)
    graph = KnowledgeGraphService(memory.connection, profile.slug)

    first = graph.upsert_entity("company", "Acme GmbH")
    second = graph.upsert_entity("company", "ACME GmbH", {"source_document_id": "doc-1"})
    results = graph.search_entities("acme")

    assert first == second
    assert results
    assert results[0]["metadata"]["canonical_name"] == "acme GMBH".lower()


def test_knowledge_graph_extracts_grounded_relations_and_provenance(tmp_path: Path):
    _, profile, memory, _ = _create_profile(tmp_path)
    graph = KnowledgeGraphService(memory.connection, profile.slug)

    total = graph.extract_from_document(
        "doc-1",
        "Acme GmbH invoice is due on 2026-04-02 for 4200 EUR. John Miller will review the meeting next week.",
    )
    results = graph.search_entities("acme")

    assert total >= 4
    assert results
    entity = results[0]
    assert "doc-1" in entity["metadata"]["source_document_ids"]
    neighborhood = graph.get_neighborhood(entity["id"], depth=2)
    relationships = {edge["relationship"] for edge in neighborhood["edges"]}
    assert "involves" in relationships or "co_occurs_with" in relationships
    assert "amount_associated_with" in relationships or "date_associated_with" in relationships
    assert any(edge["metadata"].get("evidence_samples") for edge in neighborhood["edges"])


def test_knowledge_graph_neighborhood_honors_depth(tmp_path: Path):
    _, profile, memory, _ = _create_profile(tmp_path)
    graph = KnowledgeGraphService(memory.connection, profile.slug)

    a = graph.upsert_entity("company", "Acme GmbH", {"source_document_ids": ["doc-1"]})
    b = graph.upsert_entity("event", "invoice", {"source_document_ids": ["doc-1"]})
    c = graph.upsert_entity("date", "2026-04-02", {"source_document_ids": ["doc-1"]})
    graph.add_edge(a, b, "involves", "doc-1", {"evidence_samples": ["Acme GmbH invoice"]})
    graph.add_edge(b, c, "due_on", "doc-1", {"evidence_samples": ["invoice due on 2026-04-02"]})
    memory.connection.commit()

    depth_one = graph.get_neighborhood(a, depth=1)
    depth_two = graph.get_neighborhood(a, depth=2)

    assert any(edge["target"]["id"] == b or edge["source"]["id"] == b for edge in depth_one["edges"])
    assert not any(edge["target"]["id"] == c or edge["source"]["id"] == c for edge in depth_one["edges"])
    assert any(edge["target"]["id"] == c or edge["source"]["id"] == c for edge in depth_two["edges"])


def test_action_planner_ranks_alerts_and_applies_feedback(tmp_path: Path):
    _, _, _, local_data = _create_profile(tmp_path)
    planner = ActionPlanner()
    local_data.set_assistant_mode("manual")
    now = datetime(2026, 3, 21, 9, 0)
    inbox_alert = {
        "type": "inbox",
        "title": "New email",
        "message": "You have 2 new unread messages.",
        "count": 2,
        "samples": [
            {"id": "m1", "sender": "client@example.com", "subject": "Urgent invoice update", "received_at": now.isoformat()},
        ],
        "evidence": ["client@example.com: Urgent invoice update"],
    }
    file_alert = {
        "type": "file_watch",
        "title": "New file indexed",
        "message": "New file indexed: notes",
        "path": "C:/docs/notes.txt",
        "document_id": "doc-1",
        "document_title": "notes",
        "category": "internal",
        "evidence": ["notes (internal)"],
    }

    ranked = planner.rank_alerts([file_alert, inbox_alert], local_data, now=now)

    assert ranked[0]["type"] == "inbox"
    assert ranked[0]["priority"] == "high"
    assert ranked[0]["interrupt_now"] is True
    assert ranked[1]["type"] == "file_watch"
    assert ranked[1]["interruption_class"] == "ambient"

    first_score = float(ranked[0]["priority_score"])
    planner.record_feedback(local_data, ranked[0], "dismissed")
    reranked = planner.rank_alerts([inbox_alert], local_data, now=now)

    assert float(reranked[0]["priority_score"]) < first_score


def test_action_planner_enriches_suggested_actions_with_alert_context(tmp_path: Path):
    _, _, _, local_data = _create_profile(tmp_path)
    planner = ActionPlanner()
    now = datetime(2026, 3, 21, 9, 0)
    alert = {
        "type": "document",
        "title": "Documents due soon",
        "message": "1 document requires attention within 7 days.",
        "documents": [{"id": "doc-1", "title": "Invoice A", "category": "invoice", "due_date": "2026-03-22T12:00:00"}],
        "evidence": ["Invoice A (2026-03-22T12:00:00)"],
    }

    ranked = planner.rank_alerts([alert], local_data, now=now)
    suggested = ranked[0]["suggested_actions"]

    assert suggested
    assert suggested[0]["payload"]["source_alert_type"] == "document"
    assert suggested[0]["payload"]["source_alert_key"] == ranked[0]["alert_key"]
    assert suggested[0]["evidence"]
    assert suggested[0]["confidence"] >= 0.4


def test_spreadsheet_query_supports_filters_and_grouping():
    data = [
        {"Customer": "Acme", "Status": "paid", "Revenue": "1200"},
        {"Customer": "Acme", "Status": "open", "Revenue": "800"},
        {"Customer": "Beta", "Status": "paid", "Revenue": "500"},
    ]

    grouped = SpreadsheetParser.query_dataframe(data, "sum revenue group by status")
    filtered = SpreadsheetParser.query_dataframe(data, "show rows where status is paid")

    assert "paid" in grouped.lower()
    assert "Filtered rows: 2" in filtered


def test_document_watcher_emits_evidence_and_action_payloads(tmp_path: Path):
    platform, profile, memory, _ = _create_profile(tmp_path)
    documents = DocumentService(memory, platform, profile)
    source = tmp_path / "invoice.txt"
    source.write_text("Rechnung fällig am 2026-03-25", encoding="utf-8")
    documents.ingest_document(source)
    watcher = DocumentWatcher(documents, profile.slug, interval_seconds=0, lookahead_days=10)

    alerts = watcher.check(now=datetime(2026, 3, 20))

    assert alerts
    alert = alerts[0]
    assert alert["evidence"]
    assert alert["documents"][0]["title"]
    assert alert["suggested_actions"]
    assert alert["suggested_actions"][0]["payload"]["source_alert_message"] == alert["message"]


def test_action_planner_validates_email_to_string_as_list():
    from app.action_planner import ActionPlanner
    planner = ActionPlanner()
    payload = planner._validate_payload("draft_email", {"to": "user@example.com", "subject": "Test"})
    assert payload["to"] == ["user@example.com"]
    assert payload["subject"] == "Test"
    assert payload["body"] == ""

def test_action_planner_validates_email_missing_to():
    from app.action_planner import ActionPlanner
    planner = ActionPlanner()
    payload = planner._validate_payload("draft_email", {"subject": "Test"})
    assert payload["to"] == []
    assert payload["subject"] == "Test"

def test_action_planner_validates_reminder_defaults():
    from app.action_planner import ActionPlanner
    planner = ActionPlanner()
    payload = planner._validate_payload("create_reminder", {})
    assert payload["title"] == "Follow up"
    assert payload["kind"] == "reminder"


def test_scheduler_retry_failed_task_resets_state():
    import sqlite3
    from app.database import SCHEMA, _apply_migrations, _ensure_profile_compat
    from app.scheduler import SchedulerService

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _apply_migrations(db)
    _ensure_profile_compat(db)
    db.commit()
    svc = SchedulerService(db, "test", max_retries=1)
    task = svc.create_task("Retry test", "0 9 * * *", "custom_prompt")
    svc.mark_task_failed(task["id"], "Error 1")
    svc.mark_task_failed(task["id"], "Error 2")
    failed = svc.list_tasks()
    failed_task = [t for t in failed if t["id"] == task["id"]][0]
    assert failed_task["status"] == "failed"
    assert not failed_task["enabled"]

    retried = svc.retry_failed_task(task["id"])
    assert retried["enabled"]
    assert retried["status"] == "idle"
    assert retried["failure_count"] == 0
    assert retried["retry_attempts"] == 0


def test_spreadsheet_german_sum_query():
    from app.spreadsheet import SpreadsheetParser
    data = [
        {"Kunde": "Müller GmbH", "Betrag": "1000.50 EUR", "Kategorie": "Dienstleistung"},
        {"Kunde": "Schmidt AG", "Betrag": "2500.00 EUR", "Kategorie": "Produkt"},
        {"Kunde": "Weber OHG", "Betrag": "750.00 EUR", "Kategorie": "Dienstleistung"},
    ]
    result = SpreadsheetParser.query_dataframe(data, "Summe von Betrag")
    assert "4250" in result.replace(",", "").replace(".", "") or "4,250" in result or "4250.50" in result


def test_spreadsheet_german_count_query():
    from app.spreadsheet import SpreadsheetParser
    data = [
        {"Kunde": "Müller GmbH", "Typ": "Rechnung"},
        {"Kunde": "Schmidt AG", "Typ": "Angebot"},
        {"Kunde": "Weber OHG", "Typ": "Rechnung"},
    ]
    result = SpreadsheetParser.query_dataframe(data, "Wie viele Rechnungen wo Typ = Rechnung")
    assert "2" in result


def test_spreadsheet_german_group_by():
    from app.spreadsheet import SpreadsheetParser
    data = [
        {"Kunde": "A", "Betrag": "100", "Kategorie": "Produkt"},
        {"Kunde": "B", "Betrag": "200", "Kategorie": "Dienst"},
        {"Kunde": "C", "Betrag": "300", "Kategorie": "Produkt"},
    ]
    result = SpreadsheetParser.query_dataframe(data, "Summe von Betrag gruppiert nach Kategorie")
    assert "Produkt" in result and "Dienst" in result


def test_spreadsheet_german_average_query():
    from app.spreadsheet import SpreadsheetParser
    data = [
        {"Name": "A", "Umsatz": "100"},
        {"Name": "B", "Umsatz": "200"},
        {"Name": "C", "Umsatz": "300"},
    ]
    result = SpreadsheetParser.query_dataframe(data, "Durchschnitt von Umsatz")
    assert "200" in result


def test_knowledge_graph_extracts_german_names_with_umlauts():
    import sqlite3
    from app.knowledge_graph import KnowledgeGraphService
    from app.database import SCHEMA, _apply_migrations, _ensure_profile_compat
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _apply_migrations(db)
    _ensure_profile_compat(db)
    db.commit()
    kg = KnowledgeGraphService(db, "test")
    result = kg.extract_from_text("Jörg Müller works at Böhmer GmbH.")
    # Should extract both person and company
    assert "person" in result or "company" in result
    entities = kg.search_entities("Müller")
    assert len(entities) > 0

def test_knowledge_graph_extracts_ev_ohg_kgaa():
    import sqlite3
    from app.knowledge_graph import KnowledgeGraphService
    from app.database import SCHEMA, _apply_migrations, _ensure_profile_compat
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _apply_migrations(db)
    _ensure_profile_compat(db)
    db.commit()
    kg = KnowledgeGraphService(db, "test")
    result = kg.extract_from_text("Deutscher Verband e.V. signed a contract with Müller OHG.")
    assert "company" in result

def test_knowledge_graph_fuzzy_dedup():
    import sqlite3
    from app.knowledge_graph import KnowledgeGraphService
    from app.database import SCHEMA, _apply_migrations, _ensure_profile_compat
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _apply_migrations(db)
    _ensure_profile_compat(db)
    db.commit()
    kg = KnowledgeGraphService(db, "test")
    id1 = kg.upsert_entity("company", "Apple Inc.")
    id2 = kg.upsert_entity("company", "Apple Inc")  # Missing period
    # Should deduplicate to the same entity
    assert id1 == id2

def test_fuzzy_match_umlaut_normalization():
    from app.knowledge_graph import _fuzzy_match, _normalize_umlaut
    assert _normalize_umlaut("Müller") == "mueller"
    assert _normalize_umlaut("Jörg") == "joerg"
    assert _fuzzy_match("mueller", "müller")
    assert _fuzzy_match("Müller GmbH", "Mueller GmbH")

def test_german_intent_document_search():
    from app.intent import RuleBasedIntentEngine
    engine = RuleBasedIntentEngine()
    result = engine.parse("suche in meinen dokumenten nach Verträgen")
    assert result.intent_name in ("search_documents", "document_search", "ingest_document") or result.intent_type == "action"

def test_german_intent_greeting():
    from app.intent import RuleBasedIntentEngine
    engine = RuleBasedIntentEngine()
    result = engine.parse("Guten Morgen")
    assert result.intent_type in ("greeting", "query", "action")

def test_german_intent_create_reminder():
    from app.intent import RuleBasedIntentEngine
    engine = RuleBasedIntentEngine()
    result = engine.parse("erstelle eine erinnerung für morgen")
    assert "reminder" in result.intent_name.lower() or result.intent_type == "action"


def test_validate_steuernummer_valid():
    from app.german_business import GermanBusinessService
    valid, msg = GermanBusinessService.validate_steuernummer("12/345/67890")
    assert valid
    valid2, msg2 = GermanBusinessService.validate_steuernummer("1234567890")
    assert valid2

def test_validate_steuernummer_invalid():
    from app.german_business import GermanBusinessService
    valid, msg = GermanBusinessService.validate_steuernummer("123")
    assert not valid

def test_validate_ust_id_valid():
    from app.german_business import GermanBusinessService
    valid, msg = GermanBusinessService.validate_ust_id("DE123456789")
    assert valid

def test_validate_ust_id_invalid():
    from app.german_business import GermanBusinessService
    valid, msg = GermanBusinessService.validate_ust_id("DE12345")
    assert not valid

def test_dsgvo_request_template_auskunft(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    from app.german_business import GermanBusinessService
    svc = GermanBusinessService(memory.connection, platform, profile, local_data, documents)
    template = svc.generate_dsgvo_request_template("auskunft")
    assert "Art. 15 DSGVO" in template
    assert "Auskunft" in template

def test_dsgvo_request_template_loeschung(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    from app.german_business import GermanBusinessService
    svc = GermanBusinessService(memory.connection, platform, profile, local_data, documents)
    template = svc.generate_dsgvo_request_template("loeschung")
    assert "Art. 17 DSGVO" in template
    assert "Löschung" in template

def test_tax_calendar_creates_reminders(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    from app.german_business import GermanBusinessService
    svc = GermanBusinessService(memory.connection, platform, profile, local_data, documents)
    reminders = svc.create_tax_calendar_reminders()
    assert len(reminders) == 7
    titles = [r["title"] for r in reminders]
    assert "USt-Voranmeldung (monatlich)" in titles
    assert "Jahresabschluss-Erinnerung" in titles


# ---------- 4.1: i18n locale files ----------

import json


def test_i18n_en_locale_exists_and_valid():
    locale_path = Path(__file__).resolve().parent.parent / "app" / "static" / "locales" / "en.json"
    assert locale_path.exists(), "en.json locale file must exist"
    data = json.loads(locale_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert len(data) > 200, "en.json should have 200+ keys"
    assert "app.title" in data
    assert "composer.placeholder" in data
    assert "schedules.retry" in data


def test_i18n_de_locale_exists_and_valid():
    locale_path = Path(__file__).resolve().parent.parent / "app" / "static" / "locales" / "de.json"
    assert locale_path.exists(), "de.json locale file must exist"
    data = json.loads(locale_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert len(data) > 200, "de.json should have 200+ keys"
    assert "app.title" in data
    assert data["app.title"] == "KERN Arbeitsbereich"
    assert data["composer.send"] == "Senden"


def test_i18n_locale_key_parity():
    """en.json and de.json must have exactly the same keys."""
    base = Path(__file__).resolve().parent.parent / "app" / "static" / "locales"
    en = json.loads((base / "en.json").read_text(encoding="utf-8"))
    de = json.loads((base / "de.json").read_text(encoding="utf-8"))
    missing_in_de = set(en.keys()) - set(de.keys())
    missing_in_en = set(de.keys()) - set(en.keys())
    assert not missing_in_de, f"Keys in en.json but missing in de.json: {missing_in_de}"
    assert not missing_in_en, f"Keys in de.json but missing in en.json: {missing_in_en}"


def test_i18n_js_module_exists():
    i18n_path = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "i18n.js"
    assert i18n_path.exists(), "i18n.js module must exist"
    content = i18n_path.read_text(encoding="utf-8")
    assert "function t(" in content
    assert "loadLocale" in content
    assert "getCurrentLang" in content


def test_i18n_renderer_imports_t():
    renderer_path = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "dashboard-renderer.js"
    content = renderer_path.read_text(encoding="utf-8")
    assert 'import { t }' in content, "dashboard-renderer.js must import t from i18n"
    assert 't("status.connected")' in content or "t(\"status.connected\")" in content


def test_ws_handlers_retry_failed_task():
    """ws_handlers.py must handle retry_failed_task command."""
    ws_path = Path(__file__).resolve().parent.parent / "app" / "ws_handlers.py"
    content = ws_path.read_text(encoding="utf-8")
    assert "retry_failed_task" in content


def test_schedule_renderer_has_retry_button():
    """dashboard-renderer.js must render retry button for failed tasks."""
    renderer_path = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "dashboard-renderer.js"
    content = renderer_path.read_text(encoding="utf-8")
    assert "schedule-retry-btn" in content
