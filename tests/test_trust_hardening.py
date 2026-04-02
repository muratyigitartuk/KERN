from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import runpy
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.action_planner import ActionPlanner
from app.attention import FileWatcher
from app.artifacts import ArtifactStore
from app.backup import BackupService
from app.config import settings
from app.database import connect
from app.documents import DocumentService
from app.email_service import EmailService
from app.german_business import GermanBusinessService
from app.knowledge_graph import KnowledgeGraphService
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.meetings import MeetingService
from app.platform import PlatformStore, connect_platform_db
from app.retention import RetentionService
from app.retrieval import RetrievalService
from app.routes import register_routes
from app.scheduler import SchedulerService, _next_run_from_cron
from app.spreadsheet import SpreadsheetParser
from app.syncing import SyncService
from app.tools.calendar import CalendarService
from app.tools.memory_tools import RecallMemoryTool
from app.tools.documents import QuerySpreadsheetTool, SearchDocumentsTool
from app.tools.scheduler_tools import CreateScheduleTool
from app.types import BackupTarget, EmailDraft, ProfileSession, ProfileSummary, ToolRequest, UICommand


class StubRecognizer:
    def transcribe(self, _payload: bytes) -> str:
        return "Decision: keep the local plan. TODO: send the recap. Follow up next week."


class _FakeOCRBackend:
    def __init__(self, results: list[tuple[str, float | None]]) -> None:
        self._results = list(results)
        self.calls: list[str] = []

    def extract_image(self, image_path: str | Path):
        self.calls.append(str(image_path))
        text, confidence = self._results.pop(0)
        return __import__("app.ocr", fromlist=["OCRPageResult"]).OCRPageResult(
            text=text,
            confidence_avg=confidence,
            line_count=1 if text.strip() else 0,
        )


def _build_stack(tmp_path: Path):
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    memory = MemoryRepository(connect(Path(profile.db_path)), profile_slug=profile.slug)
    local_data = LocalDataService(memory, "sir")
    return platform, profile, memory, local_data


def _configure_runtime_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "system_db_path", tmp_path / "kern-system.db")
    monkeypatch.setattr(settings, "profile_root", tmp_path / "profiles")
    monkeypatch.setattr(settings, "backup_root", tmp_path / "backups")
    monkeypatch.setattr(settings, "db_path", tmp_path / "legacy.db")
    monkeypatch.setattr(settings, "root_path", tmp_path / ".kern")
    monkeypatch.setattr(settings, "document_root", tmp_path / "documents")
    monkeypatch.setattr(settings, "attachment_root", tmp_path / "attachments")
    monkeypatch.setattr(settings, "archive_root", tmp_path / "archives")
    monkeypatch.setattr(settings, "meeting_root", tmp_path / "meetings")
    monkeypatch.setattr(settings, "google_calendar_token_cache", tmp_path / "google-calendar.json")
    monkeypatch.setattr(settings, "seed_defaults", False)
    monkeypatch.setattr(settings, "rag_enabled", False)
    monkeypatch.setattr(settings, "db_encryption_mode", "fernet")
    monkeypatch.setattr(settings, "artifact_encryption_enabled", True)


def _dispose_runtime(runtime) -> None:
    with contextlib.suppress(Exception):
        runtime._close_profile_stack()
    with contextlib.suppress(Exception):
        runtime.platform.connection.close()
    locked_scaffold = getattr(runtime, "_locked_scaffold_path", None)
    if locked_scaffold:
        Path(locked_scaffold).unlink(missing_ok=True)


def _write_pdf(path: Path, page_texts: list[str]) -> None:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    try:
        for page_text in page_texts:
            page = doc.new_page()
            if page_text:
                page.insert_text((72, 72), page_text)
        doc.save(path)
    finally:
        doc.close()


def test_audit_chain_detects_tampering(tmp_path: Path):
    platform, profile, _, _ = _build_stack(tmp_path)
    platform.record_audit("runtime", "startup", "info", "boot", profile_slug=profile.slug)
    ok, reason = platform.verify_audit_chain()
    assert ok is True
    assert reason is None

    platform.connection.execute("UPDATE audit_events SET message = 'tampered' WHERE id = 1")
    platform.connection.commit()

    ok, reason = platform.verify_audit_chain()
    assert ok is False
    assert reason is not None


def test_backup_validation_rejects_traversal(tmp_path: Path):
    pytest.importorskip("cryptography")
    service = BackupService()
    payload_buffer = io.BytesIO()
    with zipfile.ZipFile(payload_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../escape.txt", "bad")
    from cryptography.fernet import Fernet

    salt = Fernet.generate_key()
    key = service._derive_key("secret-pass", salt)  # noqa: SLF001
    encrypted = Fernet(key).encrypt(payload_buffer.getvalue())
    backup_path = tmp_path / "bad.kernbak"
    backup_path.write_text(
        json.dumps(
            {
                "version": 2,
                "profile_slug": "default",
                "created_at": datetime.utcnow().isoformat(),
                "salt": __import__("base64").urlsafe_b64encode(salt).decode("ascii"),
                "ciphertext": encrypted.decode("ascii"),
            }
        ),
        encoding="utf-8",
    )

    validation = service.validate_backup(backup_path, "secret-pass")

    assert validation.valid is False
    assert any("Path traversal rejected" in error for error in validation.errors)


def test_locked_profile_blocks_document_email_and_sync_services(tmp_path: Path):
    platform, profile, memory, local_data = _build_stack(tmp_path)
    platform.lock_profile(profile.slug, reason="locked for test")
    documents = DocumentService(memory.connection, platform, profile)
    email = EmailService(
        memory.connection,
        platform,
        profile,
        local_data,
        CalendarService(local_data),
        documents,
    )
    sync = SyncService(memory, profile, platform=platform)

    with pytest.raises(PermissionError):
        documents.list_documents()
    with pytest.raises(PermissionError):
        email.list_drafts()
    with pytest.raises(PermissionError):
        sync.list_targets()


@pytest.mark.asyncio
async def test_memory_scope_off_blocks_durable_recall(tmp_path: Path):
    _, profile, memory, local_data = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    retrieval = RetrievalService(memory)
    local_data.remember_fact("favorite_editor", "VS Code")
    tool = RecallMemoryTool(local_data, documents, retrieval)
    local_data.set_memory_scope("off")

    result = await tool.run(
        ToolRequest(
            tool_name="recall_memory",
            arguments={"query": "editor"},
            user_utterance="what do you remember about my editor",
            reason="test",
        )
    )

    assert result.data["facts"] == []
    assert result.data["document_hits"] == []
    assert result.data["retrieval_hits"] == []


def test_retrieval_builds_local_index_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, profile, memory, _ = _build_stack(tmp_path)
    document_path = Path(profile.documents_root) / "invoice.txt"
    document_path.write_text("Rechnung fuer Beratung und Steuerunterlagen", encoding="utf-8")
    service = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    service.ingest_document(document_path, source="manual")

    monkeypatch.setattr("app.retrieval.settings.rag_enabled", True)
    monkeypatch.setattr("app.retrieval.settings.rag_embed_model", "local-tfidf")
    monkeypatch.setattr("app.retrieval.settings.rag_index_version", "v-test")
    retrieval = RetrievalService(memory)

    hits = retrieval.retrieve("steuer unterlagen", scope="profile", limit=5)

    assert hits
    assert retrieval.status.backend == "local_tfidf"
    assert retrieval.status.index_health == "healthy"


def test_document_reads_are_audited_and_archive_hits_keep_source_type(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, platform, profile)
    source = tmp_path / "archive.json"
    source.write_text(json.dumps([{"role": "user", "content": "archive evidence"}]), encoding="utf-8")

    documents.ingest_document(source)
    documents.list_documents()
    archive = documents.import_conversation_archive(source, source="chatgpt")
    hits = documents.search("archive evidence", scope="profile_plus_archive")

    assert Path(archive.file_path).is_relative_to(Path(profile.archives_root))
    assert any(event.action == "list_documents" for event in platform.list_audit_events(profile.slug, limit=20))
    assert any(event.category == "retrieval" and event.action == "search" for event in platform.list_audit_events(profile.slug, limit=20))
    assert any(hit.source_type == "archive" for hit in hits)


def test_document_ingest_sets_corporate_classification(tmp_path: Path):
    _, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    source = tmp_path / "invoice.txt"
    source.write_text("Invoice payment due 2026-04-10 with Umsatzsteuer and IBAN details.", encoding="utf-8")

    record = documents.ingest_document(source)
    listed = documents.list_documents(limit=1)[0]

    assert record.classification == "finance"
    assert listed.classification == "finance"


def test_pdf_ingest_uses_native_text_without_ocr_when_signal_is_strong(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    pdf_path = tmp_path / "native-text.pdf"
    _write_pdf(pdf_path, ["Invoice total 4250 EUR due 2026-04-10 for ACME GmbH."])
    fake_ocr = _FakeOCRBackend([("SHOULD NOT RUN", 0.99)])

    monkeypatch.setattr("app.documents.get_ocr_backend", lambda engine, lang: fake_ocr)
    monkeypatch.setattr("app.documents.ocr_backend_available", lambda engine: True)
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(settings, "ocr_min_text_chars_per_page", 16)

    record = documents.ingest_document(pdf_path)
    row = memory.connection.execute(
        "SELECT metadata_json FROM document_records WHERE id = ?",
        (record.id,),
    ).fetchone()
    metadata = json.loads(row["metadata_json"])

    assert metadata["ocr_used"] is False
    assert metadata["ocr_pages"] == 0
    assert fake_ocr.calls == []


def test_pdf_ingest_triggers_ocr_fallback_for_blank_pdf_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    pdf_path = tmp_path / "scan-like.pdf"
    _write_pdf(pdf_path, [""])
    fake_ocr = _FakeOCRBackend([("Rechnung Due date: 2026-04-02 Amount: 2750 EUR", 0.91)])

    monkeypatch.setattr("app.documents.get_ocr_backend", lambda engine, lang: fake_ocr)
    monkeypatch.setattr("app.documents.ocr_backend_available", lambda engine: True)
    monkeypatch.setattr(settings, "ocr_enabled", True)

    record = documents.ingest_document(pdf_path)
    row = memory.connection.execute(
        "SELECT metadata_json FROM document_records WHERE id = ?",
        (record.id,),
    ).fetchone()
    metadata = json.loads(row["metadata_json"])
    hit = documents.search("2750", scope="profile", limit=1)[0]

    assert metadata["ocr_used"] is True
    assert metadata["ocr_pages"] == 1
    assert metadata["ocr_page_indices"] == [1]
    assert metadata["ocr_confidence_avg"] == pytest.approx(0.91)
    assert metadata["ocr_low_confidence"] is False
    assert "2750 EUR" in hit.text
    assert hit.metadata["ocr_used"] is True


def test_pdf_ingest_only_ocrs_weak_pages_in_mixed_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    pdf_path = tmp_path / "mixed.pdf"
    _write_pdf(
        pdf_path,
        [
            "Invoice total 4250 EUR due 2026-04-10 for ACME GmbH.",
            "",
        ],
    )
    fake_ocr = _FakeOCRBackend([("Offer Target amount: 3100 EUR", 0.88)])

    monkeypatch.setattr("app.documents.get_ocr_backend", lambda engine, lang: fake_ocr)
    monkeypatch.setattr("app.documents.ocr_backend_available", lambda engine: True)
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(settings, "ocr_min_text_chars_per_page", 16)

    record = documents.ingest_document(pdf_path)
    row = memory.connection.execute(
        "SELECT metadata_json FROM document_records WHERE id = ?",
        (record.id,),
    ).fetchone()
    metadata = json.loads(row["metadata_json"])

    assert metadata["ocr_used"] is True
    assert metadata["ocr_pages"] == 1
    assert metadata["ocr_page_indices"] == [2]
    assert len(fake_ocr.calls) == 1


def test_pdf_availability_reports_ocr_degraded_when_paddle_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))

    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr("app.documents.ocr_backend_available", lambda engine: False)
    monkeypatch.setattr(documents, "_import_available", lambda module_name: module_name == "fitz")

    ok, note = documents.availability()

    assert ok is True
    assert note is not None
    assert "PDF ready / OCR fallback unavailable" in note


@pytest.mark.asyncio
async def test_corporate_search_tool_restricts_sensitive_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, platform, profile)
    source = tmp_path / "salary-review.txt"
    source.write_text("Confidential salary review for employee bonus allocation.", encoding="utf-8")
    documents.ingest_document(source, source="manual")
    tool = SearchDocumentsTool(documents)

    monkeypatch.setattr(settings, "policy_mode", "corporate")
    monkeypatch.setattr(settings, "policy_restrict_sensitive_reads", True)

    result = await tool.run(
        ToolRequest(
            tool_name="search_documents",
            arguments={"query": "salary review"},
            user_utterance="search my documents for salary review",
            reason="test",
        )
    )

    assert result.status == "failed"
    assert "restricted" in result.display_text.lower()


def test_retention_service_prunes_expired_profile_data_and_reseals_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, platform, profile)
    old_document = tmp_path / "old-invoice.txt"
    old_document.write_text("Invoice due 2024-01-01", encoding="utf-8")
    record = documents.ingest_document(old_document, source="manual")
    old_attachment = Path(profile.attachments_root) / "old-attachment.txt"
    old_attachment.parent.mkdir(parents=True, exist_ok=True)
    old_attachment.write_text("old attachment", encoding="utf-8")
    memory.connection.execute(
        """
        INSERT INTO mailbox_messages (
            id, account_id, message_id, folder, subject, sender, recipients_json, received_at,
            body_text, has_attachments, attachment_paths_json, metadata_json, created_at, profile_slug
        ) VALUES (?, NULL, ?, 'INBOX', 'Old message', 'sender@example.com', '[]', ?, '', 1, ?, '{}', ?, ?)
        """,
        (
            str(uuid4()),
            str(uuid4()),
            (datetime.utcnow() - timedelta(days=10)).isoformat(),
            json.dumps([str(old_attachment)]),
            (datetime.utcnow() - timedelta(days=10)).isoformat(),
            profile.slug,
        ),
    )
    memory.connection.execute(
        "UPDATE document_records SET imported_at = ?, created_at = ? WHERE id = ?",
        (
            (datetime.utcnow() - timedelta(days=10)).isoformat(),
            (datetime.utcnow() - timedelta(days=10)).isoformat(),
            record.id,
        ),
    )
    memory.connection.commit()
    old_backup = Path(profile.backups_root) / f"{profile.slug}-old.kernbak"
    old_backup.parent.mkdir(parents=True, exist_ok=True)
    old_backup.write_text("backup", encoding="utf-8")
    old_time = (datetime.utcnow() - timedelta(days=10)).timestamp()
    os.utime(old_backup, (old_time, old_time))
    platform.record_audit("runtime", "old_event", "info", "old")
    platform.connection.execute(
        "UPDATE audit_events SET created_at = ?",
        ((datetime.utcnow() - timedelta(days=10)).isoformat(),),
    )
    platform.connection.commit()

    monkeypatch.setattr(settings, "retention_documents_days", 1)
    monkeypatch.setattr(settings, "retention_email_days", 1)
    monkeypatch.setattr(settings, "retention_transcripts_days", 1)
    monkeypatch.setattr(settings, "retention_audit_days", 1)
    monkeypatch.setattr(settings, "retention_backups_days", 1)

    retention = RetentionService(memory, platform, profile)
    result = retention.apply(reason="test")

    assert result.counts["documents"] == 1
    assert result.counts["email"] == 1
    assert result.counts["backups"] == 1
    assert not Path(record.file_path).exists()
    assert not old_attachment.exists()
    assert not old_backup.exists()
    assert memory.connection.execute("SELECT COUNT(*) AS count FROM document_records WHERE profile_slug = ?", (profile.slug,)).fetchone()["count"] == 0
    assert memory.connection.execute("SELECT COUNT(*) AS count FROM mailbox_messages WHERE profile_slug = ?", (profile.slug,)).fetchone()["count"] == 0
    ok, _reason = platform.verify_audit_chain()
    assert ok is True


@pytest.mark.asyncio
async def test_query_spreadsheet_respects_profile_lock(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, platform, profile)
    spreadsheet = Path(profile.documents_root) / "finance.csv"
    spreadsheet.parent.mkdir(parents=True, exist_ok=True)
    spreadsheet.write_text("Revenue,Status\n1200,paid\n", encoding="utf-8")
    tool = QuerySpreadsheetTool(documents)
    platform.lock_profile(profile.slug, reason="locked for test")

    with pytest.raises(PermissionError):
        await tool.run(
            ToolRequest(
                tool_name="query_spreadsheet",
                arguments={"file_path": str(spreadsheet), "query": "sum revenue"},
                user_utterance="query spreadsheet",
                reason="test",
            )
        )


@pytest.mark.asyncio
async def test_create_schedule_rejects_unsupported_action_type(tmp_path: Path):
    _, profile, memory, _ = _build_stack(tmp_path)
    scheduler = SchedulerService(memory.connection, profile.slug)
    tool = CreateScheduleTool(lambda: scheduler)

    result = await tool.run(
        ToolRequest(
            tool_name="create_schedule",
            arguments={"title": "Broken", "cron_expression": "0 9 * * *", "action_type": "run_tool"},
            user_utterance="create schedule",
            reason="test",
        )
    )

    assert result.status == "failed"
    assert "unsupported" in result.display_text.lower()


def test_retrieval_reindex_creates_job_when_platform_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform, profile, memory, _ = _build_stack(tmp_path)
    monkeypatch.setattr("app.retrieval.settings.rag_enabled", True)
    monkeypatch.setattr("app.retrieval.settings.rag_embed_model", "local-tfidf")
    monkeypatch.setattr("app.retrieval.settings.rag_index_version", "v-job")
    retrieval = RetrievalService(memory, platform=platform, profile_slug=profile.slug)

    status = retrieval.rebuild_index(scope="profile_plus_archive")
    jobs = platform.list_jobs(profile.slug, limit=10)

    assert status.ready is True
    assert any(job.job_type == "retrieval_reindex" and job.status == "completed" for job in jobs)


def test_encrypted_profile_db_roundtrip(tmp_path: Path):
    encrypted_path = tmp_path / "secure.db"
    key = __import__("base64").urlsafe_b64encode(__import__("os").urandom(32)).decode("ascii")
    connection = connect(encrypted_path, encryption_mode="fernet", encryption_key=key, key_version=1, key_derivation_version="v1")
    repo = MemoryRepository(connection)
    repo.create_note("encrypted note")
    connection.close()

    assert encrypted_path.exists()
    assert not encrypted_path.read_bytes().startswith(b"SQLite format 3")

    reopened = connect(encrypted_path, encryption_mode="fernet", encryption_key=key, key_version=1, key_derivation_version="v1")
    reopened_repo = MemoryRepository(reopened)
    assert "encrypted note" in reopened_repo.list_notes(limit=5)


def test_meeting_action_items_can_be_reviewed(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    service = MeetingService(memory.connection, platform, profile)
    now = datetime.utcnow().isoformat()
    memory.connection.execute(
        """
        INSERT INTO meeting_records (id, profile_slug, title, audio_path, transcript_path, summary_json, status, created_at, updated_at)
        VALUES ('meeting-1', ?, 'Sprint Review', 'meeting.wav', 'meeting.txt', '{}', 'transcribed', ?, ?)
        """,
        (profile.slug, now, now),
    )
    memory.connection.execute(
        """
        INSERT INTO transcript_action_items (meeting_id, profile_slug, title, details, due_hint, created_at, review_state)
        VALUES ('meeting-1', ?, 'Send recap', 'Send recap to team', 'tomorrow', ?, 'pending')
        """,
        (profile.slug, now),
    )
    memory.connection.commit()

    reviewed = service.review_action_item(1, True)
    items = service.list_recent_reviews(limit=1)[0].action_items

    assert reviewed.review_state == "accepted"
    assert items[0].review_state == "accepted"


def test_meeting_acceptance_creates_follow_up_ids(tmp_path: Path):
    platform, profile, memory, local_data = _build_stack(tmp_path)
    service = MeetingService(memory.connection, platform, profile, local_data)
    now = datetime.utcnow().isoformat()
    memory.connection.execute(
        """
        INSERT INTO meeting_records (id, profile_slug, title, audio_path, transcript_path, summary_json, status, created_at, updated_at)
        VALUES ('meeting-follow-up', ?, 'Sprint Review', 'meeting.wav', 'meeting.txt', '{}', 'transcribed', ?, ?)
        """,
        (profile.slug, now, now),
    )
    memory.connection.execute(
        """
        INSERT INTO transcript_action_items (meeting_id, profile_slug, title, details, due_hint, created_at, review_state)
        VALUES ('meeting-follow-up', ?, 'Send recap', 'Send recap to team', 'tomorrow', ?, 'pending')
        """,
        (profile.slug, now),
    )
    memory.connection.commit()

    reviewed = service.review_action_item(1, True)
    row = memory.connection.execute(
        "SELECT related_task_id, related_reminder_id, review_state FROM transcript_action_items WHERE id = 1"
    ).fetchone()

    assert reviewed.review_state == "accepted"
    assert reviewed.related_reminder_id is not None
    assert row["review_state"] == "accepted"
    assert row["related_reminder_id"] == reviewed.related_reminder_id
    assert row["related_task_id"] is None


def test_runtime_audit_chain_helper_records_verification(tmp_path: Path):
    from types import SimpleNamespace

    from app.main import KernRuntime

    runtime = KernRuntime.__new__(KernRuntime)
    runtime.active_profile = ProfileSummary(
        slug="default",
        title="Primary profile",
        profile_root=str(tmp_path / "profiles" / "default"),
        db_path=str(tmp_path / "profiles" / "default" / "kern.db"),
        documents_root=str(tmp_path / "profiles" / "default" / "documents"),
        attachments_root=str(tmp_path / "profiles" / "default" / "attachments"),
        archives_root=str(tmp_path / "profiles" / "default" / "archives"),
        meetings_root=str(tmp_path / "profiles" / "default" / "meetings"),
        backups_root=str(tmp_path / "backups" / "default"),
        has_pin=False,
    )
    captured: list[tuple[str, str, str, str]] = []
    runtime.platform = SimpleNamespace(
        verify_audit_chain=lambda: (False, "broken"),
        record_audit=lambda category, action, status, message, **kwargs: captured.append((category, action, status, message)),
    )

    ok, reason = KernRuntime.verify_audit_chain(runtime, "startup")

    assert ok is False
    assert reason == "broken"
    assert captured and captured[0][1] == "audit_chain_verification"


@pytest.mark.asyncio
async def test_export_logs_verifies_audit_chain(monkeypatch: pytest.MonkeyPatch):
    from app import main as main_module
    from app import routes as routes_module

    monkeypatch.setattr(main_module.runtime, "ensure_production_access", lambda blocked_scope=None: True)
    monkeypatch.setattr(routes_module, "_http_policy_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_module.runtime.platform, "verify_audit_chain", lambda: (True, None))
    payload = await main_module.export_logs()

    assert payload["audit_chain_ok"] is True
    assert payload["audit_chain_reason"] is None


@pytest.mark.asyncio
async def test_export_logs_denies_when_profile_locked(monkeypatch: pytest.MonkeyPatch):
    from app import main as main_module

    captured: list[tuple[str, str, str]] = []
    monkeypatch.setattr(main_module.runtime, "ensure_production_access", lambda blocked_scope=None: True)
    monkeypatch.setattr(main_module.runtime.platform, "is_profile_locked", lambda _slug: True)
    monkeypatch.setattr(
        main_module.runtime.platform,
        "record_audit",
        lambda category, action, status, message, **kwargs: captured.append((category, action, status)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await main_module.export_logs()

    assert exc_info.value.status_code == 423
    assert ("runtime", "export_logs", "failure") in captured


@pytest.mark.asyncio
async def test_governance_export_denies_when_profile_locked(monkeypatch: pytest.MonkeyPatch):
    from app import routes as routes_module
    from app import main as main_module

    captured: list[tuple[str, str, str]] = []
    monkeypatch.setattr(main_module.runtime, "ensure_production_access", lambda blocked_scope=None: True)
    monkeypatch.setattr(main_module.runtime.platform, "is_profile_locked", lambda _slug: True)
    monkeypatch.setattr(
        main_module.runtime.platform,
        "record_audit",
        lambda category, action, status, message, **kwargs: captured.append((category, action, status)),
    )

    with pytest.raises(HTTPException) as exc_info:
        routes_module._get_runtime = lambda: main_module.runtime
        await routes_module.export_governance_bundle()

    assert exc_info.value.status_code == 423
    assert ("governance", "export_bundle", "failure") in captured


def test_artifact_store_migrates_plaintext_files(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    platform.ensure_profile_artifact_encryption(profile.slug)
    store = ArtifactStore(platform, profile)
    plaintext = Path(profile.documents_root) / "legacy.txt"
    plaintext.parent.mkdir(parents=True, exist_ok=True)
    plaintext.write_text("legacy artifact", encoding="utf-8")

    migrated = store.migrate_profile_artifacts(memory.connection)

    assert migrated >= 1
    assert not plaintext.exists()
    encrypted_matches = list(Path(profile.documents_root).glob("legacy.txt.kenc"))
    assert encrypted_matches
    assert store.read_text(encrypted_matches[0], encoding="utf-8") == "legacy artifact"


def test_artifact_store_denies_reads_while_profile_locked(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    platform.ensure_profile_artifact_encryption(profile.slug)
    store = ArtifactStore(platform, profile)
    encrypted_path = store.write_text(Path(profile.documents_root) / "locked-note.txt", "secret artifact", encoding="utf-8")

    platform.lock_profile(profile.slug, reason="lock for artifact test")

    with pytest.raises(PermissionError):
        store.read_text(encrypted_path, encoding="utf-8")


def test_runtime_lock_unlock_rebinds_encrypted_profile_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from app.runtime import KernRuntime

    _configure_runtime_settings(monkeypatch, tmp_path)
    runtime = KernRuntime()
    try:
        runtime.memory.create_note("rebind survives")
        runtime.platform.set_profile_pin(runtime.active_profile.slug, "1234")

        original_connection = runtime.profile_connection
        original_orchestrator = runtime.orchestrator
        assert Path(getattr(original_connection, "_encrypted_path")) == Path(runtime.active_profile.db_path)

        locked_session = runtime.lock_active_profile("Test lock.")
        locked_connection = runtime.profile_connection
        locked_orchestrator = runtime.orchestrator

        assert locked_session.unlocked is False
        assert runtime._using_locked_scaffold is True
        assert locked_connection is not original_connection
        assert locked_orchestrator is not original_orchestrator
        assert Path(
            runtime.profile_connection.execute("PRAGMA database_list").fetchone()["file"]
        ) == runtime._locked_scaffold_path
        with pytest.raises(PermissionError):
            runtime.document_service.list_documents()

        unlocked_session = runtime.unlock_active_profile("1234")

        assert unlocked_session.unlocked is True
        assert runtime._using_locked_scaffold is False
        assert runtime.profile_connection is not locked_connection
        assert runtime.orchestrator is not locked_orchestrator
        assert Path(getattr(runtime.profile_connection, "_encrypted_path")) == Path(runtime.active_profile.db_path)
        assert "rebind survives" in runtime.memory.list_notes(limit=10)
    finally:
        _dispose_runtime(runtime)


def test_runtime_restart_stays_on_locked_scaffold_until_correct_unlock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from app.runtime import KernRuntime

    _configure_runtime_settings(monkeypatch, tmp_path)
    first_runtime = KernRuntime()
    try:
        first_runtime.memory.create_note("persisted across restart")
        first_runtime.platform.set_profile_pin(first_runtime.active_profile.slug, "1234")
    finally:
        _dispose_runtime(first_runtime)

    restarted = KernRuntime()
    try:
        assert restarted.profile_session.unlocked is False
        assert restarted._using_locked_scaffold is True
        assert Path(
            restarted.profile_connection.execute("PRAGMA database_list").fetchone()["file"]
        ) == restarted._locked_scaffold_path

        failed = restarted.unlock_active_profile("0000")

        assert failed.unlocked is False
        assert restarted._using_locked_scaffold is True
        assert Path(
            restarted.profile_connection.execute("PRAGMA database_list").fetchone()["file"]
        ) == restarted._locked_scaffold_path

        unlocked = restarted.unlock_active_profile("1234")

        assert unlocked.unlocked is True
        assert restarted._using_locked_scaffold is False
        assert Path(getattr(restarted.profile_connection, "_encrypted_path")) == Path(restarted.active_profile.db_path)
        assert "persisted across restart" in restarted.memory.list_notes(limit=10)
    finally:
        _dispose_runtime(restarted)


def test_retrieval_recovery_resumes_reindex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform, profile, memory, _ = _build_stack(tmp_path)
    monkeypatch.setattr("app.retrieval.settings.rag_enabled", True)
    monkeypatch.setattr("app.retrieval.settings.rag_embed_model", "local-tfidf")
    monkeypatch.setattr("app.retrieval.settings.rag_index_version", "v-recover")
    retrieval = RetrievalService(memory, platform=platform, profile_slug=profile.slug)
    job = platform.create_job(
        "retrieval_reindex",
        "Recover retrieval index",
        profile_slug=profile.slug,
        payload={"scope": "profile_plus_archive"},
        detail="Interrupted",
    )
    platform.update_job(job.id, status="recoverable", recoverable=True, checkpoint_stage="retrieval_reindex_started", detail="Interrupted")

    retrieval.recover_jobs()

    jobs = platform.list_jobs(profile.slug, limit=10)
    assert any(item.id == job.id and item.status == "completed" for item in jobs)


def test_backup_restore_recovery_rolls_back_staging(tmp_path: Path):
    from app.runtime import KernRuntime

    runtime = KernRuntime.__new__(KernRuntime)
    platform, profile, memory, local_data = _build_stack(tmp_path)
    runtime.platform = platform
    runtime.active_profile = profile
    runtime.profile_connection = memory.connection
    runtime.local_data = local_data
    runtime.backup_service = BackupService()
    runtime.profile_session = ProfileSession(profile_slug=profile.slug, unlocked=True)

    requested_root = tmp_path / "restore-target"
    final_root = requested_root
    staged_root = tmp_path / ".restore-target.stage"
    rollback_root = tmp_path / ".restore-target.rollback"
    final_root.mkdir(parents=True, exist_ok=True)
    (rollback_root / "state").mkdir(parents=True, exist_ok=True)
    (staged_root / "partial").mkdir(parents=True, exist_ok=True)
    job = platform.create_job(
        "restore_backup",
        "Restore encrypted backup",
        profile_slug=profile.slug,
        payload={"backup_path": str(tmp_path / "backup.kernbak"), "restore_root": str(requested_root)},
        detail="Interrupted",
    )
    platform.update_checkpoint(
        job.id,
        "planned",
        {
            "staged_root": str(staged_root),
            "final_root": str(final_root),
            "requested_root": str(requested_root),
            "rollback_root": str(rollback_root),
        },
    )
    platform.update_job(job.id, status="recoverable", recoverable=True, checkpoint_stage="planned", detail="Interrupted restore")

    runtime._recover_backup_jobs()

    recovered_job = platform.get_job(job.id)
    assert recovered_job is not None
    assert recovered_job.status == "rolled_back"
    assert not staged_root.exists()


def test_retrieval_rebuilds_when_model_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, profile, memory, _ = _build_stack(tmp_path)
    document_path = Path(profile.documents_root) / "deadline-note.txt"
    document_path.write_text("Steuer deadline fuer Rechnung am 2026-04-02", encoding="utf-8")
    DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root)).ingest_document(document_path)

    monkeypatch.setattr("app.retrieval.settings.rag_enabled", True)
    monkeypatch.setattr("app.retrieval.settings.rag_embed_model", "local-tfidf-v1")
    monkeypatch.setattr("app.retrieval.settings.rag_index_version", "v-model")
    retrieval = RetrievalService(memory)

    first_hits = retrieval.retrieve("steuer deadline", scope="profile", limit=3)
    assert first_hits
    assert memory.get_index_metadata("embed_model") == "local-tfidf-v1"

    monkeypatch.setattr("app.retrieval.settings.rag_embed_model", "local-tfidf-v2")
    second_hits = retrieval.retrieve("steuer deadline", scope="profile", limit=3)

    assert second_hits
    assert memory.get_index_metadata("embed_model") == "local-tfidf-v2"
    assert retrieval.status.backend == "local_tfidf"


def test_document_recovery_prefers_owned_copy_when_original_is_missing(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, platform, profile)
    source = tmp_path / "source.txt"
    source.write_text("Rechnung recovery body", encoding="utf-8")
    job = platform.create_job(
        "document_ingest",
        "Recover document",
        profile_slug=profile.slug,
        detail="Interrupted",
        payload={"file_path": str(source), "source": "manual"},
    )
    owned_path = documents.artifacts.import_file(source, Path(profile.documents_root))
    platform.update_checkpoint(job.id, "artifact_copied", {"stored_path": str(owned_path), "original_path": str(source)})
    platform.update_job(job.id, status="recoverable", recoverable=True, checkpoint_stage="artifact_copied", detail="Interrupted")
    source.unlink()

    documents.recover_jobs()

    jobs = platform.list_jobs(profile.slug, limit=10)
    records = documents.list_documents(limit=5)
    assert any(item.id == job.id and item.status == "completed" for item in jobs)
    assert any(record.file_path == str(owned_path) for record in records)


def test_german_business_recovery_rolls_back_partial_generation(tmp_path: Path):
    platform, profile, memory, local_data = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, platform, profile)
    service = GermanBusinessService(memory.connection, platform, profile, local_data, documents)
    business_root = Path(profile.documents_root) / "german-business"
    business_root.mkdir(parents=True, exist_ok=True)
    file_path = business_root / "partial.md"
    file_path.write_text("partial invoice", encoding="utf-8")
    now = datetime.utcnow().isoformat()
    document_id = "biz-partial"
    memory.connection.execute(
        """
        INSERT INTO german_business_documents (id, profile_slug, kind, title, status, file_path, metadata_json, created_at, updated_at)
        VALUES (?, ?, 'rechnung', 'Partial invoice', 'draft', ?, '{}', ?, ?)
        """,
        (document_id, profile.slug, str(file_path), now, now),
    )
    memory.connection.commit()
    job = platform.create_job(
        "german_business_generation",
        "Recover business doc",
        profile_slug=profile.slug,
        detail="Interrupted",
        payload={"document_id": document_id, "kind": "rechnung", "title": "Partial invoice"},
    )
    platform.update_checkpoint(job.id, "artifact_written", {"document_id": document_id, "file_path": str(file_path)})
    platform.update_job(job.id, status="recoverable", recoverable=True, checkpoint_stage="artifact_written", detail="Interrupted")

    service.recover_jobs()

    recovered_job = platform.get_job(job.id)
    remaining = memory.connection.execute(
        "SELECT COUNT(*) AS count FROM german_business_documents WHERE id = ? AND profile_slug = ?",
        (document_id, profile.slug),
    ).fetchone()
    assert recovered_job is not None
    assert recovered_job.status == "rolled_back"
    assert not file_path.exists()
    assert int(remaining["count"]) == 0


def test_email_schedule_recovery_rolls_back_unsent_event_and_draft(tmp_path: Path):
    platform, profile, memory, local_data = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, platform, profile)
    calendar = CalendarService(local_data)
    service = EmailService(memory.connection, platform, profile, local_data, calendar, documents)
    event_id = local_data.create_event("Interrupted meeting", datetime.utcnow() + timedelta(days=1))
    draft = service.save_draft(EmailDraft(to=["client@example.com"], subject="Invite", body="Join us"))
    job = platform.create_job(
        "schedule_meeting_invite",
        "Recover scheduled meeting",
        profile_slug=profile.slug,
        detail="Interrupted",
        payload={"title": "Interrupted meeting", "send_invite": False},
    )
    platform.update_checkpoint(job.id, "event_scheduled", {"event_id": event_id})
    platform.update_checkpoint(job.id, "draft_saved", {"event_id": event_id, "draft_id": draft.id})
    platform.update_job(job.id, status="recoverable", recoverable=True, checkpoint_stage="draft_saved", detail="Interrupted")

    service.recover_jobs()

    recovered_job = platform.get_job(job.id)
    assert recovered_job is not None
    assert recovered_job.status == "rolled_back"
    assert service.memory.get_email_draft(draft.id or "") is None
    assert local_data.delete_event(event_id) is False


def test_sync_recovery_restores_destination_after_interrupted_commit(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    service = SyncService(memory, profile, platform=platform)
    destination_root = tmp_path / "mirror"
    backup_root = tmp_path / ".mirror.rollback"
    staging_root = tmp_path / ".mirror.stage"
    backup_root.mkdir(parents=True, exist_ok=True)
    (backup_root / "state.txt").write_text("original", encoding="utf-8")
    staging_root.mkdir(parents=True, exist_ok=True)
    (staging_root / "state.txt").write_text("staged", encoding="utf-8")
    job = platform.create_job(
        "profile_sync",
        "Recover sync",
        profile_slug=profile.slug,
        detail="Interrupted",
        payload={"target_id": "target-1", "data_classes": ["documents"]},
    )
    platform.update_checkpoint(
        job.id,
        "waiting_for_commit",
        {
            "target_id": "target-1",
            "destination_root": str(destination_root),
            "staging_root": str(staging_root),
            "backup_root": str(backup_root),
        },
    )
    platform.update_job(job.id, status="recoverable", recoverable=True, checkpoint_stage="waiting_for_commit", detail="Interrupted")

    service.recover_jobs()

    recovered_job = platform.get_job(job.id)
    assert recovered_job is not None
    assert recovered_job.status == "rolled_back"
    assert destination_root.exists()
    assert (destination_root / "state.txt").read_text(encoding="utf-8") == "original"
    assert not staging_root.exists()


def test_webdav_upload_requires_encrypted_payload(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    service = SyncService(memory, profile, platform=platform)
    source = tmp_path / "plain.txt"
    source.write_text("plain export", encoding="utf-8")

    with pytest.raises(RuntimeError, match="encrypted payload"):
        service.upload_webdav(
            str(source),
            "https://example.invalid/remote.php/dav/files/test/plain.txt",
            username="kern",
            password="secret",
        )


def test_dashboard_sources_reflect_locked_export_and_safe_reset():
    root = Path(__file__).resolve().parents[1]
    dashboard = (root / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")
    app_js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    events_js = (root / "app" / "static" / "js" / "dashboard-events.js").read_text(encoding="utf-8")
    renderer_js = (root / "app" / "static" / "js" / "dashboard-renderer.js").read_text(encoding="utf-8")
    theme_js = (root / "app" / "static" / "js" / "theme-controller.js").read_text(encoding="utf-8")

    assert 'id="exportLogsLink"' in dashboard
    assert 'id="settingsAuditChain"' in dashboard
    assert 'id="themeColorMeta"' in dashboard
    assert 'id="sidebarHome"' in dashboard
    assert 'id="openConversationSearch"' in dashboard
    assert 'id="conversationSearchModal"' in dashboard
    assert 'data-settings-section-nav="appearance"' in dashboard
    assert 'data-theme-mode="system"' in dashboard
    assert 'data-theme-mode="light"' in dashboard
    assert 'data-theme-mode="dark"' in dashboard
    assert 'bindDashboardEvents' in app_js
    assert 'createThemeController' in app_js
    assert 'function resetConversation() {' in events_js
    assert 'elements.sidebarHome?.addEventListener("click", resetConversation);' in events_js
    assert 'elements.openConversationSearch?.addEventListener("click", () => {' in events_js
    assert 'elements.commandInput.value = prompt;' in events_js
    assert 'themeController.setPreference' in events_js
    assert 'exportLogsLink.setAttribute("aria-disabled", locked ? "true" : "false");' in renderer_js
    assert 'function renderConversationSearch(filterText = "") {' in renderer_js
    assert 'alert.interruption_class' in renderer_js
    assert 'alert_index: idx' in renderer_js
    assert 'renderThemeState()' in renderer_js
    assert 'const THEME_MODE_KEY = "kern.theme.mode";' in theme_js
    assert 'document.documentElement.dataset.theme = theme;' in theme_js


def test_ui_command_accepts_new_dashboard_runtime_controls():
    for command_type in (
        "search_memory_history",
        "dismiss_all_alerts",
        "get_knowledge_graph",
        "search_knowledge_graph",
        "set_tts_speed",
        "set_tts_voice",
    ):
        command = UICommand.model_validate({"type": command_type, "settings": {}})
        assert command.type == command_type


def test_scheduler_supports_real_cron_expressions():
    now = datetime(2026, 3, 20, 10, 2)

    assert _next_run_from_cron("*/5 * * * *", now) == datetime(2026, 3, 20, 10, 5)
    assert _next_run_from_cron("15 14 * * 1-5", now) == datetime(2026, 3, 20, 14, 15)


def test_batch_ingest_persists_file_hash_for_deduplication(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, platform, profile)
    source = tmp_path / "invoice.txt"
    source.write_text("Rechnung fällig am 2026-03-25", encoding="utf-8")

    first = documents.ingest_batch([source])
    second = documents.ingest_batch([source])
    row = memory.connection.execute(
        "SELECT file_hash FROM document_records WHERE id = ?",
        (first[0].id,),
    ).fetchone()

    assert len(first) == 1
    assert second == []
    assert row is not None
    assert row["file_hash"]


def test_watch_folder_persists_rules_across_rebuild(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    documents = DocumentService(memory.connection, platform, profile)
    watched = tmp_path / "watched"
    watched.mkdir()

    watcher = FileWatcher([], documents, profile.slug, platform, connection=memory.connection)
    assert watcher.add_directory(watched) is True

    reloaded = FileWatcher([], documents, profile.slug, platform, connection=memory.connection)

    assert watched.resolve() in reloaded.list_directories()


def test_close_profile_stack_stops_watchers_and_closes_connection():
    from app.runtime import KernRuntime

    class StubWatcher:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    class StubConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    runtime = KernRuntime.__new__(KernRuntime)
    runtime._file_watcher = StubWatcher()
    runtime._inbox_watcher = object()
    runtime._calendar_watcher = object()
    runtime._document_watcher = object()
    runtime.profile_connection = StubConnection()
    runtime.memory = object()

    KernRuntime._close_profile_stack(runtime)

    assert runtime._file_watcher is None
    assert runtime._inbox_watcher is None
    assert runtime._calendar_watcher is None
    assert runtime._document_watcher is None
    assert runtime.profile_connection is None
    assert runtime.memory is None


def test_health_endpoint_reports_runtime_state(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    runtime = type("Runtime", (), {})()
    runtime.active_profile = profile
    runtime.memory = memory
    runtime.platform = platform
    runtime.audit_chain_ok = True
    runtime.audit_chain_reason = None
    runtime.last_audit_verification_at = None
    runtime.orchestrator = type(
        "Orchestrator",
        (),
        {
                "snapshot": type(
                    "Snapshot",
                    (),
                      {
                          "llm_available": False,
                          "model_info": type("ModelInfo", (), {"app_version": "0.1.0"})(),
                          "background_components": {"scheduler": "ready"},
                          "runtime_degraded_reasons": [],
                          "policy_mode": "corporate",
                        "retention_policies": {"documents_days": 3650},
                        "last_monitor_tick_at": None,
                    },
                )()
        },
    )()
    runtime.scheduler_service = object()
    runtime.network_monitor = type(
        "NetworkMonitor",
        (),
        {"status": type("Status", (), {"status": "isolated", "endpoints": []})()},
    )()
    runtime._pending_proactive_alerts = []
    runtime._using_locked_scaffold = False
    runtime.ensure_production_access = lambda blocked_scope=None: True

    app = FastAPI()
    register_routes(app, lambda: runtime)
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["components"]["scheduler"] == "ok"
    assert response.json()["app_version"] == "0.1.0"


def test_governance_export_bundle_reports_policy_and_classification(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    runtime = type("Runtime", (), {})()
    runtime.active_profile = profile
    runtime.memory = memory
    runtime.platform = type(
        "Platform",
        (),
        {
            "connection": platform.connection,
            "is_profile_locked": lambda self, slug: False,
            "record_audit": lambda *args, **kwargs: None,
            "list_backup_targets": lambda self, slug: [BackupTarget(kind="local_folder", path="C:/tmp", label="Local")],
            "list_audit_events": lambda self, slug, limit=50: [],
        },
    )()
    runtime.verify_audit_chain = lambda purpose=None: (True, None)
    runtime.backup_service = type(
        "BackupService",
        (),
        {
            "list_backups": lambda self, profile, target: [],
            "inspect_backup": lambda self, path: {"path": str(path), "created_at": "2026-03-20T00:00:00"},
        },
    )()
    runtime.memory = type(
        "Memory",
        (),
        {
            "connection": memory.connection,
            "summarize_document_classifications": lambda self: {"finance": 2, "internal": 1},
        },
    )()
    runtime.orchestrator = type(
        "Orchestrator",
        (),
        {
            "snapshot": type(
                "Snapshot",
                (),
                {
                    "model_info": type("ModelInfo", (), {"app_version": "0.1.0"})(),
                    "policy_summary": {"mode": "corporate"},
                    "retention_policies": {"documents_days": 3650},
                    "runtime_degraded_reasons": [],
                    "background_components": {"scheduler": "ready"},
                    "network_status": type("NetworkStatus", (), {"model_dump": lambda self, mode=None: {"status": "isolated"}})(),
                    "last_monitor_tick_at": None,
                    "security_status": type("Security", (), {"model_dump": lambda self, mode=None: {"db_encryption_enabled": True}})(),
                    "scheduled_tasks": [],
                },
            )()
        },
    )()

    app = FastAPI()
    register_routes(app, lambda: runtime)
    client = TestClient(app)

    response = client.post("/governance/export")

    assert response.status_code == 200
    payload = response.json()
    assert payload["app_version"] == "0.1.0"
    assert payload["policy"]["mode"] == "corporate"
    assert payload["document_classifications"]["finance"] == 2
    assert payload["retention_status"] == {}


def test_preflight_report_flags_corporate_without_encryption(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(settings, "system_db_path", tmp_path / "kern-system.db")
    monkeypatch.setattr(settings, "profile_root", tmp_path / "profiles")
    monkeypatch.setattr(settings, "backup_root", tmp_path / "backups")
    monkeypatch.setattr(settings, "root_path", tmp_path / ".kern")
    monkeypatch.setattr(settings, "policy_mode", "corporate")
    monkeypatch.setattr(settings, "db_encryption_mode", "off")
    monkeypatch.setattr(settings, "artifact_encryption_enabled", False)
    module = runpy.run_path(str(Path("scripts/preflight-kern.py")))
    report = module["build_preflight_report"]()

    assert report["status"] == "error"
    assert any("encrypted profile databases" in error for error in report["errors"])
    assert "extras" in report
    assert "scheduled_task" in report


def test_health_route_returns_503_when_degraded(tmp_path: Path):
    platform, profile, memory, _ = _build_stack(tmp_path)
    runtime = type("Runtime", (), {})()
    runtime.active_profile = profile
    runtime.memory = memory
    runtime.platform = platform
    runtime.audit_chain_ok = False
    runtime.audit_chain_reason = "broken"
    runtime.last_audit_verification_at = None
    runtime.orchestrator = type(
        "Orchestrator",
        (),
        {
            "snapshot": type(
                "Snapshot",
                (),
                {
                    "llm_available": False,
                    "model_info": type("ModelInfo", (), {"app_version": "0.1.0"})(),
                    "background_components": {"scheduler": "ready"},
                    "runtime_degraded_reasons": ["Audit chain verification failed."],
                    "policy_mode": "corporate",
                    "retention_policies": {"documents_days": 3650},
                    "retention_status": {},
                    "last_monitor_tick_at": None,
                },
            )()
        },
    )()
    runtime.scheduler_service = object()
    runtime.network_monitor = type(
        "NetworkMonitor",
        (),
        {"status": type("Status", (), {"status": "isolated", "endpoints": []})()},
    )()
    runtime._pending_proactive_alerts = []
    runtime._using_locked_scaffold = False

    app = FastAPI()
    register_routes(app, lambda: runtime)
    client = TestClient(app)

    response = client.get("/health")
    ready = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert ready.status_code == 503


def test_restore_bundle_compatibility_blocks_future_version(tmp_path: Path):
    pytest.importorskip("cryptography")
    module = runpy.run_path(str(Path("scripts/restore-kern.py")))
    derive_key = module["_derive_key"]
    validate_bundle = module["_validate_update_bundle_compatibility"]
    load_bundle = module["_load_update_bundle"]
    from cryptography.fernet import Fernet

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"app_version": "9.9.9"}))
        archive.writestr("state.txt", "kern")

    salt = Fernet.generate_key()
    key = derive_key("secret-pass", salt)
    bundle_path = tmp_path / "future.kernbundle"
    bundle_path.write_text(
        json.dumps(
            {
                "format": "self_contained_update_bundle",
                "version": 1,
                "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
                "ciphertext": Fernet(key).encrypt(archive_buffer.getvalue()).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )

    artifact = load_bundle(bundle_path, "secret-pass")
    errors = validate_bundle(artifact, force=False)

    assert errors
    assert "newer than this runtime" in errors[0]


def test_scheduler_records_retry_and_terminal_failure(tmp_path: Path):
    _, profile, memory, _ = _build_stack(tmp_path)
    scheduler = SchedulerService(memory.connection, profile.slug, retry_delay_minutes=5, max_retries=1)
    task = scheduler.create_task(
        "Retry once",
        "*/5 * * * *",
        action_type="custom_prompt",
        action_payload={"prompt": "missing"},
        max_retries=1,
    )

    scheduler.tick(datetime(2026, 3, 20, 10, 0))
    first_failure = scheduler.record_failure(task["id"], "boom")
    second_failure = scheduler.record_failure(task["id"], "boom again")

    assert first_failure["run_status"] == "retry_pending"
    assert first_failure["enabled"] is True
    assert second_failure["run_status"] == "failed"
    assert second_failure["enabled"] is False


def test_action_planner_hydrates_alert_payloads():
    planner = ActionPlanner()
    inbox_actions = planner.suggest_actions(
        {"type": "inbox", "samples": [{"sender": "alice@example.com", "subject": "Quarterly update"}]}
    )
    document_actions = planner.suggest_actions(
        {"type": "document", "documents": [{"title": "Invoice 42", "due_date": "2026-03-25T09:00:00"}]}
    )

    assert any(action["action_type"] == "draft_email" and "Re: Quarterly update" in action["payload"].get("subject", "") for action in inbox_actions)
    assert any(action["action_type"] == "create_reminder" and action["payload"].get("title") == "Review Invoice 42" for action in document_actions)


def test_spreadsheet_query_supports_filtered_aggregates():
    rows = [
        {"customer": "ACME", "revenue": "10", "status": "open"},
        {"customer": "ACME", "revenue": "15", "status": "closed"},
        {"customer": "Globex", "revenue": "20", "status": "open"},
    ]

    answer = SpreadsheetParser.query_dataframe(rows, "sum of revenue where customer = ACME")

    assert "revenue: 25.00" in answer


def test_knowledge_graph_deduplicates_normalized_company_names(tmp_path: Path):
    _, profile, memory, _ = _build_stack(tmp_path)
    kg = KnowledgeGraphService(memory.connection, profile.slug)

    first = kg.upsert_entity("company", "ACME GmbH")
    second = kg.upsert_entity("company", "  Acme   GmbH ")

    assert first == second
