from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.documents import DocumentService
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.retrieval import RetrievalService
from app.types import DocumentRecord


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SampleWorkspaceService:
    def __init__(
        self,
        *,
        local_data: LocalDataService,
        documents: DocumentService,
        memory: MemoryRepository,
        retrieval: RetrievalService | None = None,
    ) -> None:
        self.local_data = local_data
        self.documents = documents
        self.memory = memory
        self.retrieval = retrieval
        self.assets_root = Path(__file__).resolve().parent / "sample_workspace_assets"

    def manifest(self) -> dict[str, object]:
        path = self.assets_root / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def seed(self) -> list[DocumentRecord]:
        state = self.local_data.sample_workspace_state()
        if state["active"] and state["seeded"] and state["document_ids"]:
            records = [
                record
                for record in self.memory.list_document_records(limit=100, include_archived=False)
                if record.id in {str(item) for item in state["document_ids"]}
            ]
            if records:
                return records
        temp_root = Path(tempfile.mkdtemp(prefix="kern_sample_workspace_"))
        try:
            copied_paths: list[Path] = []
            for fixture in sorted((self.assets_root / "fixtures").glob("*")):
                if not fixture.is_file():
                    continue
                target = temp_root / fixture.name
                shutil.copy2(fixture, target)
                copied_paths.append(target)
            records = self.documents.ingest_batch(
                copied_paths,
                source="sample_workspace",
                category="sample_workspace",
                tags=["sample_workspace", "demo"],
            )
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
        self.local_data.update_sample_workspace_state(
            active=True,
            seeded=True,
            document_ids=[record.id for record in records],
            started_at=_utcnow(),
            exited_at="",
        )
        self.local_data.update_onboarding_state(
            selected_path="sample_workspace",
            sample_workspace_active=True,
            sample_workspace_seeded=True,
            starter_workflow="sample_workspace",
            completed=False,
        )
        self._refresh_retrieval()
        return records

    def exit(self) -> int:
        state = self.local_data.sample_workspace_state()
        document_ids = [str(item) for item in state.get("document_ids", []) if str(item).strip()]
        archived_count = self.memory.set_documents_archived(document_ids, archived=True) if document_ids else 0
        self.local_data.update_sample_workspace_state(
            active=False,
            exited_at=_utcnow(),
        )
        self.local_data.update_onboarding_state(
            selected_path="real_documents",
            sample_workspace_active=False,
            starter_workflow="",
            completed=False,
        )
        self._refresh_retrieval()
        return archived_count

    def _refresh_retrieval(self) -> None:
        if self.retrieval is None:
            return
        scope = self.local_data.memory_scope()
        if getattr(self.retrieval, "rebuild_index", None):
            self.retrieval.rebuild_index(scope)
        if getattr(self.retrieval, "rebuild_vec_index", None):
            try:
                self.retrieval.rebuild_vec_index(scope)
            except Exception:
                return
