from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
import smtplib

from app.documents import DocumentService
from app.email_service import EmailService
from app.german_business import GermanBusinessService
from app.local_data import LocalDataService
from app.email_service import EmailService as MailService
from app.meetings import MeetingService
from app.memory import MemoryRepository
from app.platform import PlatformStore, connect_platform_db
from app.syncing import SyncService
from app.tools.calendar import CalendarService
from app.tools.email_tools import EmailReminderTool
from app.database import connect
from app.types import CalendarActionPlan, EmailDraft, EmailMessage, ToolRequest


def build_profile_stack(tmp_path: Path):
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    memory = MemoryRepository(connect(Path(profile.db_path)))
    local_data = LocalDataService(memory, "sir")
    return platform, profile, memory, local_data


def test_document_ingest_and_search(tmp_path: Path):
    platform, profile, memory, _ = build_profile_stack(tmp_path)
    source = tmp_path / "invoice.txt"
    source.write_text("Rechnung 2026-001\nBetrag 100 EUR\nSteuerhinweis", encoding="utf-8")
    service = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))

    record = service.ingest_file(source, source="manual")
    hits = service.search("Betrag", scope="profile_plus_archive")

    assert record.category == "invoice"
    assert hits
    assert hits[0].metadata["title"] == "invoice"


def test_archive_import_creates_catalog_entry(tmp_path: Path):
    _, profile, memory, _ = build_profile_stack(tmp_path)
    archive_path = tmp_path / "chatgpt.json"
    archive_path.write_text(json.dumps({"title": "Imported chat", "messages": [{"role": "user", "content": "Hello"}]}), encoding="utf-8")
    service = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))

    record = service.import_conversation_archive(archive_path, source="chatgpt")

    assert record.imported_turns == 1
    assert memory.list_conversation_archives(limit=5)[0].title == "Imported chat"


def test_email_deadline_reminder_and_invite_flow(tmp_path: Path):
    platform, profile, memory, local_data = build_profile_stack(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    calendar = CalendarService(local_data)
    service = EmailService(memory.connection, platform, profile, local_data, calendar, documents)

    message = EmailMessage(
        id=str(uuid4()),
        subject="Offer review due",
        sender="finance@example.com",
        recipients=["me@example.com"],
        received_at=datetime.utcnow(),
        has_attachments=False,
    )
    memory.append_mailbox_message(message, body_text="Please respond by 2026-04-01.")

    title, due_at = service.create_reminder_from_email(local_data.create_reminder, message.id)
    result = service.schedule_meeting_and_invite(
        CalendarActionPlan(
            title="Customer sync",
            starts_at=datetime(2026, 4, 2, 10, 0),
            invite_recipients=["client@example.com"],
            draft=EmailDraft(to=["client@example.com"], subject="Invite", body="Please join."),
        ),
        send_invite=False,
    )

    assert "Email follow-up" in title
    assert due_at.year == 2026
    assert result["event_id"] > 0
    assert result["draft_id"]

def test_german_business_generation_and_sync(tmp_path: Path):
    platform, profile, memory, local_data = build_profile_stack(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    service = GermanBusinessService(memory, None, profile, local_data, documents)
    angebot = service.create_offer("ACME GmbH", "A-100", [{"label": "Beratung", "amount": "500,00 EUR"}])
    sync = SyncService(memory, profile)
    destination = sync.mirror_directory(profile.profile_root, str(tmp_path / "mirror"))

    assert Path(angebot.file_path).exists()
    assert Path(destination).exists()


def test_email_mailbox_dedupes_message_ids_at_the_database_layer(tmp_path: Path):
    platform, profile, memory, local_data = build_profile_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    EmailService(memory.connection, platform, profile, local_data, CalendarService(local_data), documents)

    index_rows = memory.connection.execute("PRAGMA index_list(mailbox_messages)").fetchall()
    unique_indexes = {row["name"] for row in index_rows if row["unique"]}

    assert "idx_mailbox_messages_profile_message_id_unique" in unique_indexes


def test_email_read_recent_email_surfaces_sync_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform, profile, memory, local_data = build_profile_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    service = EmailService(memory.connection, platform, profile, local_data, CalendarService(local_data), documents)
    monkeypatch.setattr(service, "sync_mailbox", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        service.read_recent_email()


def test_email_send_retries_transient_smtp_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform, profile, memory, local_data = build_profile_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    service = EmailService(memory.connection, platform, profile, local_data, CalendarService(local_data), documents)
    account = service.create_or_update_account(
        label="Primary inbox",
        email_address="kern@example.com",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="kern-user",
        password="secret-password",
    )
    attempts = {"count": 0}

    class FlakySMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self):
            return None

        def login(self, username, password):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise smtplib.SMTPServerDisconnected("temporary network drop")

        def send_message(self, message):
            return None

    monkeypatch.setattr("app.email_service.smtplib.SMTP", FlakySMTP)

    subject = service.send_email(
        EmailDraft(to=["client@example.com"], subject="Retry subject", body="Hello from KERN."),
        account_id=account.id,
    )

    assert subject == "Retry subject"
    assert attempts["count"] >= 2


def test_calendar_schedule_meeting_routes_through_calendar_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    platform, profile, memory, local_data = build_profile_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    calendar = CalendarService(local_data)
    service = EmailService(memory.connection, platform, profile, local_data, calendar, documents)
    seen = {}

    def fake_schedule(plan):
        seen["title"] = plan.title
        return plan.model_copy(update={"event_id": 77, "invite_status": "draft_only"})

    monkeypatch.setattr(calendar, "schedule_meeting", fake_schedule)

    result = service.schedule_meeting_and_invite(
        CalendarActionPlan(
            title="Customer sync",
            starts_at=datetime(2026, 4, 2, 10, 0),
            invite_recipients=["client@example.com"],
            draft=EmailDraft(to=["client@example.com"], subject="Invite", body="Please join."),
        ),
        send_invite=False,
    )

    assert seen["title"] == "Customer sync"
    assert result["event_id"] == 77
    assert result["invite_status"] == "draft_only"


@pytest.mark.asyncio
async def test_email_reminder_tool_uses_suggestion_flow(tmp_path: Path):
    platform, profile, memory, local_data = build_profile_stack(tmp_path)
    documents = DocumentService(memory.connection, Path(profile.documents_root), Path(profile.archives_root))
    service = EmailService(memory.connection, platform, profile, local_data, CalendarService(local_data), documents)
    tool = EmailReminderTool(service, local_data.create_reminder)
    message = EmailMessage(
        id=str(uuid4()),
        subject="Offer review due",
        sender="finance@example.com",
        recipients=["me@example.com"],
        received_at=datetime.utcnow(),
        has_attachments=False,
    )
    memory.append_mailbox_message(message, body_text="Please respond by 2026-04-01.")

    result = await tool.run(
        ToolRequest(
            tool_name="create_email_reminder",
            arguments={"message_id": message.id},
            user_utterance="create reminder from email",
            reason="test",
        )
    )

    assert result.data["suggestions"]
    assert result.data["suggestions"][0]["message_id"] == message.id
