from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.database import connect
from app.documents import DocumentService
from app.german_business import GermanBusinessService
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.platform import PlatformStore, connect_platform_db
from app.syncing import SyncService
from app.tools.german_business import CreateAngebotTool, CreateDsgvoReminderTool
from app.types import OfferDraft, SyncTarget, ToolRequest


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


def test_document_service_ingests_txt_and_searches(tmp_path: Path):
    platform, profile, memory, _ = _create_profile(tmp_path)
    service = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    source = tmp_path / "invoice.txt"
    source.write_text("Rechnung Beispiel GmbH Betrag 120 EUR", encoding="utf-8")

    record = service.ingest_document(source)
    hits = service.search("Beispiel")

    assert record.category == "invoice"
    assert Path(record.file_path).is_relative_to(Path(profile.documents_root))
    assert Path(record.file_path).exists()
    assert hits
    assert "Beispiel" in hits[0].text


def test_document_service_imports_archive(tmp_path: Path):
    platform, profile, memory, _ = _create_profile(tmp_path)
    service = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    archive = tmp_path / "chatgpt.json"
    archive.write_text(json.dumps([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]), encoding="utf-8")

    record = service.import_conversation_archive(archive, "chatgpt")
    hits = service.search("hello", scope="profile_plus_archive")

    assert record.source == "chatgpt"
    assert record.imported_turns == 2
    assert Path(record.file_path).is_relative_to(Path(profile.archives_root))
    assert Path(record.file_path).exists()
    assert any(hit.source_type == "archive" for hit in hits)


def test_german_business_service_creates_offer_and_dsgvo_reminders(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    service = GermanBusinessService(memory.connection, platform, profile, local_data, documents)

    angebot = service.create_angebot(
        OfferDraft(
            customer_name="Beispiel GmbH",
            offer_number="ANG-1",
            line_items=[{"title": "Beratung", "amount": "100 EUR"}],
        )
    )
    reminder_ids = service.create_dsgvo_reminders()
    doc_records = service.list_documents()

    assert Path(angebot.file_path).exists()
    assert len(reminder_ids) == 2
    assert any(record.kind == "angebot" for record in doc_records)
    assert any(Path(record.file_path).is_relative_to(Path(profile.documents_root)) for record in doc_records)


def test_german_business_tools_emit_output_labels(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    documents = DocumentService(memory, Path(profile.documents_root), Path(profile.archives_root))
    service = GermanBusinessService(memory.connection, platform, profile, local_data, documents)

    angebot_tool = CreateAngebotTool(service)
    reminder_tool = CreateDsgvoReminderTool(service)

    angebot_result = asyncio.run(
        angebot_tool.run(
            ToolRequest(
                tool_name="create_angebot",
                arguments={"customer_name": "Beispiel GmbH", "offer_number": "ANG-1", "line_items": [{"title": "Beratung", "amount": "100"}]},
                user_utterance="create offer",
                reason="test",
            )
        )
    )
    reminder_result = asyncio.run(
        reminder_tool.run(
            ToolRequest(
                tool_name="create_dsgvo_reminders",
                arguments={},
                user_utterance="create reminders",
                reason="test",
            )
        )
    )

    assert angebot_result.data["output_label"] == "draft"
    assert reminder_result.data["output_label"] == "reminder"


def test_sync_service_copies_documents_to_nas_target(tmp_path: Path):
    platform, profile, memory, _ = _create_profile(tmp_path)
    documents_dir = Path(profile.documents_root)
    documents_dir.mkdir(parents=True, exist_ok=True)
    (documents_dir / "note.txt").write_text("profile payload", encoding="utf-8")
    service = SyncService(platform, profile)

    message = service.sync_to_target(
        target=SyncTarget(
            kind="nas",
            label="NAS",
            path_or_url=str(tmp_path / "nas-target"),
            enabled=True,
        ),
        data_classes=["documents"],
    )

    assert "Synced" in message
    assert (tmp_path / "nas-target" / "documents" / "note.txt").exists()
