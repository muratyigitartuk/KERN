from __future__ import annotations

from pathlib import Path

from app.database import connect
from app.document_intelligence import DocumentIntelligenceService
from app.freeform_intelligence import FreeformIntelligenceService
from app.intelligence import IntelligenceService
from app.memory import MemoryRepository
from app.retrieval import RetrievalService
from app.types import DocumentChunk, DocumentRecord, ProfileSummary


def _profile(tmp_path: Path) -> ProfileSummary:
    return ProfileSummary(
        slug="default",
        title="Default",
        profile_root=str(tmp_path),
        db_path=str(tmp_path / "kern.db"),
        documents_root=str(tmp_path / "documents"),
        attachments_root=str(tmp_path / "attachments"),
        archives_root=str(tmp_path / "archives"),
        meetings_root=str(tmp_path / "meetings"),
        backups_root=str(tmp_path / "backups"),
    )


def test_freeform_accepts_explicit_uploaded_document_drafting(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    profile = _profile(tmp_path)
    memory.upsert_document_record(
        DocumentRecord(
            id="doc-1",
            profile_slug="default",
            title="pilot-contract-unique",
            source="upload",
            file_type="txt",
            file_path=str(tmp_path / "documents" / "pilot-contract-unique.txt"),
        ),
        chunks=[
            DocumentChunk(
                document_id="doc-1",
                chunk_index=0,
                text="Kunde: Beispiel GmbH. Preisrahmen: 48.000 EUR. Pilotstart: 15. Mai 2026.",
            )
        ],
    )
    retrieval = RetrievalService(memory)
    intelligence = IntelligenceService(None, memory, profile)  # type: ignore[arg-type]
    service = FreeformIntelligenceService(
        memory,
        profile,
        retrieval=retrieval,
        intelligence=intelligence,
        document_intelligence=DocumentIntelligenceService(memory, profile, retrieval=retrieval, intelligence=intelligence),
        preparation_packet_getter=lambda **_: None,
        recommendation_lister=lambda **_: [],
    )

    intent = service.classify_intent(
        "Aus den hochgeladenen Dokumenten: Formuliere einen Angebotsabsatz fuer die Beispiel GmbH.",
        organization_id=None,
        workspace_slug="default",
        actor_user_id=None,
    )

    assert intent.task_family == "document_qa"
    assert intent.clarification_required is False
    assert intent.selected_document_ids == ["doc-1"]


def test_freeform_keeps_plain_chat_reply_requests_out_of_prepared_work(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    profile = _profile(tmp_path)
    retrieval = RetrievalService(memory)
    intelligence = IntelligenceService(None, memory, profile)  # type: ignore[arg-type]
    service = FreeformIntelligenceService(
        memory,
        profile,
        retrieval=retrieval,
        intelligence=intelligence,
        document_intelligence=DocumentIntelligenceService(memory, profile, retrieval=retrieval, intelligence=intelligence),
        preparation_packet_getter=lambda **_: None,
        recommendation_lister=lambda **_: [],
    )

    intent = service.classify_intent(
        "reply with one short sentence",
        organization_id=None,
        workspace_slug="default",
        actor_user_id=None,
    )

    assert intent.task_family == "general_chat_fallback"
    assert intent.clarification_required is False
