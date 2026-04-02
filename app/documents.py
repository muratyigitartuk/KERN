from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

from app.artifacts import ArtifactStore
from app.config import settings
from app.metrics import metrics
from app.memory import MemoryRepository
from app.ocr import OCRPageResult, get_ocr_backend, ocr_backend_available
from app.platform import PlatformStore
from app.retrieval import RetrievalService
from app.types import ConversationArchiveRecord, DocumentChunk, DocumentRecord, ProfileSummary, RetrievalHit


class DocumentService:
    def __init__(
        self,
        memory_or_connection: MemoryRepository | sqlite3.Connection,
        platform_or_documents_root: PlatformStore | Path,
        profile_or_archives_root: ProfileSummary | Path,
        retrieval: RetrievalService | None = None,
    ) -> None:
        self.platform = platform_or_documents_root if isinstance(platform_or_documents_root, PlatformStore) else None
        if isinstance(profile_or_archives_root, ProfileSummary):
            self.profile = profile_or_archives_root
            self.documents_root = Path(self.profile.documents_root)
            self.archives_root = Path(self.profile.archives_root)
            self.memory = memory_or_connection if isinstance(memory_or_connection, MemoryRepository) else MemoryRepository(
                memory_or_connection,
                profile_slug=self.profile.slug,
            )
        else:
            self.profile = None
            self.documents_root = Path(platform_or_documents_root)
            self.archives_root = Path(profile_or_archives_root)
            self.memory = memory_or_connection if isinstance(memory_or_connection, MemoryRepository) else MemoryRepository(memory_or_connection)
        self.artifacts = ArtifactStore(self.platform, self.profile)
        self.knowledge_graph = None  # optional KnowledgeGraphService, set by runtime
        self.retrieval = retrieval or RetrievalService(
            self.memory,
            platform=self.platform,
            profile_slug=self.profile.slug if self.profile else self.memory.profile_slug,
        )

    def ingest_file(
        self,
        file_path: str | Path,
        *,
        source: str = "manual",
        category: str | None = None,
        tags: list[str] | None = None,
        _job_id: str | None = None,
        known_file_hash: str | None = None,
    ) -> DocumentRecord:
        self._ensure_unlocked("ingest_document")
        path = Path(file_path).expanduser().resolve()
        job_id = _job_id
        owned_path: Path | None = None
        if job_id is None and self.platform and self.profile:
            job = self.platform.create_job(
                "document_ingest",
                f"Ingest {path.name}",
                profile_slug=self.profile.slug if self.profile else None,
                detail="Extracting document text.",
                payload={"file_path": str(path), "source": source},
            )
            job_id = job.id
        try:
            owned_path = self._copy_into_profile_storage(path, self.documents_root)
            if self.platform and self.profile and job_id:
                self.platform.update_checkpoint(
                    job_id,
                    "artifact_copied",
                    {"stored_path": str(owned_path), "original_path": str(path)},
                )
            text, extraction_metadata = self._extract_text_with_metadata(owned_path)
            inferred_category = category or self._infer_category(owned_path, text)
            imported_at = datetime.now(timezone.utc)
            file_hash = known_file_hash or self._compute_file_hash(owned_path)
            metadata = self._build_metadata(owned_path, text, inferred_category, source)
            metadata.update(extraction_metadata)
            classification = str(metadata.get("classification") or "internal")
            data_class = "regulated_business" if inferred_category in {"finance", "legal"} else "operational"
            metadata.setdefault("data_class", data_class)
            metadata.setdefault("retention_state", "standard")
            metadata.setdefault(
                "provenance",
                {
                    "origin": "document_ingest",
                    "source": source,
                    "workspace_slug": self.profile.slug if self.profile else self.memory.profile_slug,
                },
            )
            metadata["original_path"] = str(path)
            metadata["stored_path"] = str(owned_path)
            metadata["file_hash"] = file_hash
            record = DocumentRecord(
                id=str(uuid4()),
                profile_slug=self.profile.slug if self.profile else "default",
                organization_id=self.profile.organization_id if self.profile else None,
                workspace_id=self.profile.workspace_id if self.profile else None,
                title=owned_path.stem,
                source=source,
                file_type=owned_path.suffix.lower().lstrip(".") or "unknown",
                file_path=str(owned_path),
                file_hash=file_hash,
                category=inferred_category,
                classification=classification,
                data_class=data_class,
                retention_state=str(metadata.get("retention_state") or "standard"),
                provenance=dict(metadata.get("provenance") or {}),
                tags=tags or self._default_tags(owned_path, inferred_category),
                archived=False,
                created_at=imported_at,
                imported_at=imported_at,
            )
            chunks = self._chunk_text(record.id, text)
            self.memory.upsert_document_record(record, chunks=chunks, metadata=metadata)
            if self.platform and self.profile and job_id:
                self.platform.update_checkpoint(
                    job_id,
                    "document_indexed",
                    {"document_id": record.id, "chunk_count": len(chunks), "stored_path": str(owned_path)},
                )
                self.platform.update_job(job_id, status="completed", detail=f"Indexed {record.title}.", progress=1.0, result={"document_id": record.id})
                self.platform.record_audit("documents", "ingest_document", "success", f"Indexed document {record.title}.", profile_slug=self.profile.slug, details={"path": str(path)})
            metrics.inc("kern_documents_ingested_total")
            if self.knowledge_graph and text:
                try:
                    self.knowledge_graph.extract_from_document(record.id, text[:8000])
                except Exception as exc:
                    logger.debug("knowledge graph extraction failed for %s: %s", record.id, exc)
            return record
        except Exception as exc:
            if owned_path is not None and owned_path.exists() and owned_path != path:
                with contextlib.suppress(Exception):  # cleanup — best-effort
                    owned_path.unlink(missing_ok=True)
            if self.platform and self.profile and job_id:
                self.platform.update_job(job_id, status="failed", detail=str(exc), error_code="document_ingest_failed", error_message=str(exc), result={"path": str(path)})
                self.platform.update_checkpoint(job_id, "rolled_back", {"path": str(owned_path) if owned_path else str(path)})
                self.platform.record_audit("documents", "ingest_document", "failure", str(exc), profile_slug=self.profile.slug, details={"path": str(path)})
            raise

    def search(self, query: str, scope: str = "profile_plus_archive", limit: int = 8) -> list[RetrievalHit]:
        self._ensure_unlocked("search_documents")
        if scope in {"off", "session"}:
            return []
        hits = self.retrieval.retrieve(query, scope=scope, limit=limit)
        if self.platform and self.profile:
            self.platform.record_audit(
                "documents",
                "search_documents",
                "success",
                f"Searched documents for '{query}'.",
                profile_slug=self.profile.slug,
                details={"scope": scope, "hits": len(hits)},
            )
        return hits

    def list_documents(self, limit: int = 20, category: str | None = None, *, audit: bool = True) -> list[DocumentRecord]:
        self._ensure_unlocked("list_documents")
        records = self.memory.list_document_records(limit=limit, category=category)
        if audit and self.platform and self.profile:
            self.platform.record_audit(
                "documents",
                "list_documents",
                "success",
                f"Listed {len(records)} document(s).",
                profile_slug=self.profile.slug,
                details={"limit": limit, "category": category, "count": len(records)},
            )
        return records

    def ingest_document(self, file_path: str | Path, **kwargs) -> DocumentRecord:
        return self.ingest_file(file_path, **kwargs)

    def search_documents(self, query: str, limit: int = 8, scope: str = "profile_plus_archive") -> list[RetrievalHit]:
        return self.search(query, scope=scope, limit=limit)

    def ingest_batch(
        self,
        file_paths: list[str | Path],
        *,
        source: str = "batch",
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> list[DocumentRecord]:
        """Ingest a list of files, deduplicating by file hash."""
        self._ensure_unlocked("ingest_document")
        results: list[DocumentRecord] = []
        parent_job = None
        if self.platform and self.profile:
            parent_job = self.platform.create_job(
                "document_batch_ingest",
                f"Batch ingest {len(file_paths)} file(s)",
                profile_slug=self.profile.slug,
                detail=f"Ingesting {len(file_paths)} file(s).",
                payload={"count": len(file_paths), "source": source},
            )
        for i, file_path in enumerate(file_paths):
            path = Path(file_path).expanduser().resolve()
            if not path.exists():
                continue
            known_file_hash = self._try_compute_file_hash(path)
            if self._is_duplicate_by_hash(path, known_file_hash=known_file_hash):
                continue
            try:
                record = self.ingest_file(
                    path,
                    source=source,
                    category=category,
                    tags=tags,
                    known_file_hash=known_file_hash,
                )
                results.append(record)
                if self.platform and self.profile and parent_job:
                    progress = (i + 1) / len(file_paths)
                    self.platform.update_job(
                        parent_job.id,
                        status="running",
                        progress=progress,
                        detail=f"Ingested {i + 1}/{len(file_paths)}: {path.name}",
                    )
            except Exception as exc:
                logger.debug("batch ingest failed for %s: %s", path.name, exc)
        if self.platform and self.profile and parent_job:
            self.platform.update_job(
                parent_job.id,
                status="completed",
                progress=1.0,
                detail=f"Batch ingestion complete. {len(results)}/{len(file_paths)} file(s) indexed.",
                result={"indexed": len(results), "total": len(file_paths)},
            )
            self.platform.record_audit(
                "documents",
                "ingest_batch",
                "success",
                f"Batch ingested {len(results)} of {len(file_paths)} file(s).",
                profile_slug=self.profile.slug,
                details={"indexed": len(results), "total": len(file_paths)},
            )
        return results

    def ingest_batch_report(
        self,
        file_paths: list[str | Path],
        *,
        source: str = "batch",
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, object]:
        self._ensure_unlocked("ingest_document")
        indexed_records: list[DocumentRecord] = []
        items: list[dict[str, object]] = []
        parent_job = None
        if self.platform and self.profile:
            parent_job = self.platform.create_job(
                "document_batch_ingest",
                f"Batch ingest {len(file_paths)} file(s)",
                profile_slug=self.profile.slug,
                detail=f"Ingesting {len(file_paths)} file(s).",
                payload={"count": len(file_paths), "source": source},
            )

        for i, file_path in enumerate(file_paths):
            path = Path(file_path).expanduser().resolve()
            if not path.exists():
                items.append({
                    "name": path.name,
                    "status": "rejected",
                    "detail": "File no longer exists in the temporary staging area.",
                })
                continue
            known_file_hash = self._try_compute_file_hash(path)
            duplicate = self._find_duplicate_by_hash(path, known_file_hash=known_file_hash)
            if duplicate is not None:
                items.append({
                    "name": path.name,
                    "status": "duplicate",
                    "detail": f"Already indexed locally as '{duplicate.title}'.",
                    "document": {
                        "id": duplicate.id,
                        "title": duplicate.title,
                        "category": duplicate.category,
                        "file_type": duplicate.file_type,
                    },
                })
                continue
            try:
                record = self.ingest_file(
                    path,
                    source=source,
                    category=category,
                    tags=tags,
                    known_file_hash=known_file_hash,
                )
                indexed_records.append(record)
                items.append({
                    "name": path.name,
                    "status": "indexed",
                    "detail": "Indexed into the active local profile.",
                    "document": {
                        "id": record.id,
                        "title": record.title,
                        "category": record.category,
                        "file_type": record.file_type,
                    },
                })
                if self.platform and self.profile and parent_job:
                    progress = (i + 1) / max(len(file_paths), 1)
                    self.platform.update_job(
                        parent_job.id,
                        status="running",
                        progress=progress,
                        detail=f"Ingested {i + 1}/{len(file_paths)}: {path.name}",
                    )
            except Exception as exc:
                logger.debug("batch ingest failed for %s: %s", path.name, exc)
                items.append({
                    "name": path.name,
                    "status": "failed",
                    "detail": str(exc) or "KERN could not index this file.",
                })

        if self.platform and self.profile and parent_job:
            self.platform.update_job(
                parent_job.id,
                status="completed",
                progress=1.0,
                detail=f"Batch ingestion complete. {len(indexed_records)}/{len(file_paths)} file(s) indexed.",
                result={"indexed": len(indexed_records), "total": len(file_paths)},
            )
            self.platform.record_audit(
                "documents",
                "ingest_batch",
                "success",
                f"Batch ingested {len(indexed_records)} of {len(file_paths)} file(s).",
                profile_slug=self.profile.slug,
                details={"indexed": len(indexed_records), "total": len(file_paths)},
            )

        return {
            "records": indexed_records,
            "items": items,
            "indexed": len(indexed_records),
            "duplicates": sum(1 for item in items if item["status"] == "duplicate"),
            "failed": sum(1 for item in items if item["status"] == "failed"),
        }

    def ingest_folder(
        self,
        folder_path: str | Path,
        *,
        recursive: bool = True,
        source: str = "folder",
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> list[DocumentRecord]:
        """Walk a directory tree and ingest all supported documents."""
        self._ensure_unlocked("ingest_document")
        folder = Path(folder_path).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError(f"Not a directory: {folder}")
        supported = {".pdf", ".docx", ".txt", ".md", ".csv", ".json", ".log", ".xlsx", ".xls"}
        pattern = "**/*" if recursive else "*"
        file_paths = [p for p in folder.glob(pattern) if p.is_file() and p.suffix.lower() in supported]
        return self.ingest_batch(file_paths, source=source, category=category, tags=tags)

    def _is_duplicate_by_hash(self, path: Path, *, known_file_hash: str | None = None) -> bool:
        """Check if a file with the same hash is already indexed."""
        try:
            file_hash = known_file_hash or self._compute_file_hash(path)
            row = self.memory.connection.execute(
                "SELECT id FROM document_records WHERE file_hash = ? LIMIT 1",
                (file_hash,),
            ).fetchone()
            return row is not None
        except Exception as exc:
            logger.debug("duplicate hash check failed for %s: %s", path, exc)
            return False

    def _find_duplicate_by_hash(self, path: Path, *, known_file_hash: str | None = None) -> DocumentRecord | None:
        try:
            file_hash = known_file_hash or self._compute_file_hash(path)
            row = self.memory.connection.execute(
                """
                SELECT id, profile_slug, title, source, file_type, file_path, file_hash, category, tags_json, archived, metadata_json, created_at, imported_at
                FROM document_records
                WHERE file_hash = ? AND profile_slug = ?
                ORDER BY imported_at DESC, id DESC
                LIMIT 1
                """,
                (file_hash, self.memory.profile_slug),
            ).fetchone()
            if not row:
                return None
            return DocumentRecord(
                id=row["id"],
                profile_slug=row["profile_slug"],
                title=row["title"],
                source=row["source"],
                file_type=row["file_type"],
                file_path=row["file_path"],
                file_hash=row["file_hash"] if "file_hash" in row.keys() else None,
                category=row["category"],
                classification=str(json.loads(row["metadata_json"] or "{}").get("classification") or "internal"),
                tags=json.loads(row["tags_json"] or "[]"),
                archived=bool(row["archived"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                imported_at=datetime.fromisoformat(row["imported_at"]),
            )
        except Exception as exc:
            logger.debug("duplicate hash lookup failed for %s: %s", path, exc)
            return None

    def _try_compute_file_hash(self, path: Path) -> str | None:
        try:
            return self._compute_file_hash(path)
        except Exception as exc:
            logger.debug("hash precompute failed for %s: %s", path, exc)
            return None

    def _compute_file_hash(self, path: Path) -> str:
        h = hashlib.sha256()
        with self.artifacts.temporary_plaintext(path) as plaintext_path:
            with plaintext_path.open("rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
        return h.hexdigest()

    def availability(self) -> tuple[bool, str | None]:
        if self.profile and self.platform and self.platform.is_profile_locked(self.profile.slug):
            return False, "Unlock the active profile to access documents."
        notes: list[str] = []
        degraded = False
        if self._import_available("fitz"):
            if settings.ocr_enabled:
                if ocr_backend_available(settings.ocr_engine):
                    notes.append("PDF ready / OCR fallback ready")
                else:
                    notes.append("PDF ready / OCR fallback unavailable")
                    degraded = True
            else:
                notes.append("PDF ready / OCR fallback disabled")
        else:
            notes.append("PDF parser unavailable")
            degraded = True
        if self._import_available("docx"):
            notes.append("DOCX ready")
        else:
            notes.append("DOCX parser unavailable")
            degraded = True
        if self._import_available("openpyxl"):
            notes.append("XLSX ready")
        else:
            notes.append("XLSX unavailable")
        notes.append("Text/Markdown/CSV ready")
        roots_ready = self.documents_root.exists() and self.archives_root.exists()
        if not roots_ready:
            notes.append("Storage roots unavailable")
        if degraded and roots_ready:
            notes.append("Some document formats are unavailable.")
        return roots_ready, " / ".join(notes)

    def import_conversation_archive(self, file_path: str | Path, source: str, *, _job_id: str | None = None) -> ConversationArchiveRecord:
        self._ensure_unlocked("import_conversation_archive")
        path = Path(file_path).expanduser().resolve()
        job_id = _job_id
        owned_path: Path | None = None
        if job_id is None and self.platform and self.profile:
            job = self.platform.create_job(
                "archive_import",
                f"Import {path.name}",
                profile_slug=self.profile.slug if self.profile else None,
                detail="Reading archive payload.",
                payload={"file_path": str(path), "source": source},
            )
            job_id = job.id
        try:
            owned_path = self._copy_into_profile_storage(path, self.archives_root)
            if self.platform and self.profile and job_id:
                self.platform.update_checkpoint(
                    job_id,
                    "artifact_copied",
                    {"stored_path": str(owned_path), "original_path": str(path)},
                )
            payload = json.loads(self.artifacts.read_text(owned_path, encoding="utf-8"))
            lines = self._flatten_archive_payload(payload)
            title = owned_path.stem
            if isinstance(payload, dict) and isinstance(payload.get("title"), str) and payload.get("title", "").strip():
                title = payload["title"].strip()
            archived_at = datetime.now(timezone.utc)
            record = ConversationArchiveRecord(
                id=str(uuid4()),
                profile_slug=self.profile.slug if self.profile else "default",
                source=source,
                title=title,
                file_path=str(owned_path),
                archived_at=archived_at,
                imported_turns=len(lines),
            )
            chunks = self._chunk_text(f"archive:{record.id}", "\n".join(lines))
            self.memory.upsert_conversation_archive(record, chunks=chunks)
            if self.platform and self.profile and job_id:
                self.platform.update_checkpoint(
                    job_id,
                    "archive_indexed",
                    {"archive_id": record.id, "turns": record.imported_turns, "stored_path": str(owned_path)},
                )
                self.platform.update_job(job_id, status="completed", detail=f"Imported archive {record.title}.", progress=1.0, result={"archive_id": record.id})
                self.platform.record_audit("documents", "import_conversation_archive", "success", f"Imported archive {record.title}.", profile_slug=self.profile.slug, details={"source": source, "turns": record.imported_turns})
            return record
        except Exception as exc:
            if owned_path is not None and owned_path.exists() and owned_path != path:
                with contextlib.suppress(Exception):  # cleanup — best-effort
                    owned_path.unlink(missing_ok=True)
            if self.platform and self.profile and job_id:
                self.platform.update_job(job_id, status="failed", detail=str(exc), error_code="archive_import_failed", error_message=str(exc), result={"path": str(path)})
                self.platform.update_checkpoint(job_id, "rolled_back", {"path": str(owned_path) if owned_path else str(path)})
                self.platform.record_audit("documents", "import_conversation_archive", "failure", str(exc), profile_slug=self.profile.slug, details={"path": str(path)})
            raise

    def list_archives(self, limit: int = 20, *, audit: bool = True) -> list[ConversationArchiveRecord]:
        self._ensure_unlocked("list_conversation_archives")
        archives = self.memory.list_conversation_archives(limit=limit)
        if audit and self.platform and self.profile:
            self.platform.record_audit(
                "documents",
                "list_conversation_archives",
                "success",
                f"Listed {len(archives)} archive(s).",
                profile_slug=self.profile.slug,
                details={"limit": limit, "count": len(archives)},
            )
        return archives

    def recover_jobs(self) -> None:
        if not (self.platform and self.profile):
            return
        for job in self.platform.list_jobs(self.profile.slug, limit=20):
            if not job.recoverable or job.job_type not in {"document_ingest", "archive_import"}:
                continue
            source_path = str(job.payload.get("file_path", "") or "").strip()
            checkpoint_payload: dict[str, object] = {}
            for checkpoint in self.platform.list_checkpoints(job.id):
                payload_row = self.platform.connection.execute(
                    """
                    SELECT payload_json
                    FROM recovery_checkpoints
                    WHERE job_id = ? AND stage = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (job.id, checkpoint.stage),
                ).fetchone()
                if payload_row:
                    try:
                        checkpoint_payload.update(json.loads(payload_row["payload_json"] or "{}"))
                    except Exception as exc:
                        logger.debug("checkpoint payload parse failed for job %s: %s", job.id, exc)
            preferred_path = str(checkpoint_payload.get("stored_path") or "").strip()
            recovery_path = preferred_path if preferred_path and Path(preferred_path).exists() else source_path
            if not recovery_path:
                self.platform.update_job(
                    job.id,
                    status="failed",
                    recoverable=False,
                    error_code="document_recovery_failed",
                    error_message="Document recovery is missing a file path.",
                    detail="Document recovery requires a file path.",
                )
                continue
            try:
                self.platform.update_job(
                    job.id,
                    status="running",
                    recoverable=True,
                    detail="Resuming document ingestion.",
                    checkpoint_stage=job.checkpoint_stage or "resume",
                    progress=0.1,
                )
                if job.job_type == "document_ingest":
                    record = self.ingest_file(
                        recovery_path,
                        source=str(job.payload.get("source", "manual") or "manual"),
                        _job_id=job.id,
                    )
                    self.platform.update_job(
                        job.id,
                        status="completed",
                        recoverable=False,
                        detail=f"Recovered document ingest for {record.title}.",
                        checkpoint_stage="recovered",
                        progress=1.0,
                        result={"document_id": record.id},
                    )
                else:
                    record = self.import_conversation_archive(
                        recovery_path,
                        source=str(job.payload.get("source", "archive") or "archive"),
                        _job_id=job.id,
                    )
                    self.platform.update_job(
                        job.id,
                        status="completed",
                        recoverable=False,
                        detail=f"Recovered archive import for {record.title}.",
                        checkpoint_stage="recovered",
                        progress=1.0,
                        result={"archive_id": record.id},
                    )
            except Exception as exc:
                self.platform.update_job(
                    job.id,
                    status="failed",
                    recoverable=False,
                    detail=str(exc),
                    checkpoint_stage="failed",
                    error_code="document_recovery_failed",
                    error_message=str(exc),
                    result={"file_path": recovery_path},
                )

    def _ensure_unlocked(self, action: str) -> None:
        if self.profile and self.platform:
            self.platform.assert_profile_unlocked(self.profile.slug, "documents", action)

    def _extract_text(self, path: Path) -> str:
        text, _ = self._extract_text_with_metadata(path)
        return text

    def extract_text(self, path: str | Path) -> str:
        return self._extract_text(Path(path))

    def _extract_text_with_metadata(self, path: Path) -> tuple[str, dict[str, object]]:
        metadata = self._default_ocr_metadata()
        suffix = self._logical_suffix(path)
        if suffix in {".txt", ".md", ".json", ".log"}:
            return self.artifacts.read_text(path, encoding="utf-8"), metadata
        if suffix == ".csv":
            from app.spreadsheet import SpreadsheetParser
            try:
                return SpreadsheetParser.extract_text_from_csv(path), metadata
            except Exception as exc:
                logger.debug("CSV spreadsheet parse failed for %s, falling back to raw text: %s", path, exc)
            return self.artifacts.read_text(path, encoding="utf-8"), metadata
        if suffix in {".xlsx", ".xls"}:
            from app.spreadsheet import SpreadsheetParser
            return SpreadsheetParser.extract_text_from_excel(path), metadata
        if suffix == ".pdf":
            try:
                import fitz  # type: ignore
            except ImportError as exc:
                raise RuntimeError("PyMuPDF is required for PDF ingestion.") from exc
            with self.artifacts.temporary_plaintext(path) as temporary_path:
                doc = fitz.open(temporary_path)
                try:
                    return self._extract_pdf_text_with_fallback(doc)
                finally:
                    doc.close()
        if suffix == ".docx":
            try:
                from docx import Document  # type: ignore
            except ImportError as exc:
                raise RuntimeError("python-docx is required for DOCX ingestion.") from exc
            with self.artifacts.temporary_plaintext(path) as temporary_path:
                doc = Document(str(temporary_path))
                return "\n".join(paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()), metadata
        raise RuntimeError(f"Unsupported document type: {suffix or 'unknown'}")

    def _extract_pdf_text_with_fallback(self, doc) -> tuple[str, dict[str, object]]:
        metadata = self._default_ocr_metadata()
        page_texts: list[str] = []
        ocr_confidences: list[float] = []
        ocr_page_indices: list[int] = []
        ocr_backend = None
        if settings.ocr_enabled and ocr_backend_available(settings.ocr_engine):
            ocr_backend = get_ocr_backend(settings.ocr_engine, settings.ocr_lang)
        for page_number, page in enumerate(doc, start=1):
            native_text = page.get_text("text") or ""
            if not self._pdf_page_requires_ocr(native_text):
                page_texts.append(native_text.strip())
                continue
            if ocr_backend is None:
                page_texts.append(native_text.strip())
                continue
            ocr_result = self._run_pdf_page_ocr(page, page_number, ocr_backend)
            combined_text = (ocr_result.text or native_text).strip()
            page_texts.append(combined_text)
            if ocr_result.text.strip():
                ocr_page_indices.append(page_number)
                if ocr_result.confidence_avg is not None:
                    ocr_confidences.append(ocr_result.confidence_avg)
        metadata["ocr_used"] = bool(ocr_page_indices)
        metadata["ocr_pages"] = len(ocr_page_indices)
        metadata["ocr_page_indices"] = ocr_page_indices
        metadata["ocr_confidence_avg"] = sum(ocr_confidences) / len(ocr_confidences) if ocr_confidences else None
        confidence_avg = metadata["ocr_confidence_avg"]
        metadata["ocr_low_confidence"] = bool(
            confidence_avg is not None and confidence_avg < settings.ocr_low_confidence_threshold
        )
        merged_text = "\n\n".join(part for part in page_texts if part.strip()).strip()
        return merged_text, metadata

    def _run_pdf_page_ocr(self, page, page_number: int, backend) -> OCRPageResult:
        try:
            import fitz  # type: ignore
        except ImportError as exc:
            raise RuntimeError("PyMuPDF is required for OCR page rendering.") from exc
        with tempfile.NamedTemporaryFile(suffix=f"-page-{page_number}.png", delete=False) as handle:
            image_path = Path(handle.name)
        try:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            pixmap.save(str(image_path))
            return backend.extract_image(image_path)
        finally:
            image_path.unlink(missing_ok=True)

    def _pdf_page_requires_ocr(self, text: str) -> bool:
        signal = len(re.sub(r"\s+", "", text or ""))
        return signal < settings.ocr_min_text_chars_per_page

    def _default_ocr_metadata(self) -> dict[str, object]:
        return {
            "ocr_used": False,
            "ocr_engine": settings.ocr_engine,
            "ocr_pages": 0,
            "ocr_page_indices": [],
            "ocr_confidence_avg": None,
            "ocr_low_confidence": False,
            "ocr_mode": "fallback",
        }

    def _infer_category(self, path: Path, text: str) -> str:
        lowered = f"{path.name.lower()} {text[:1200].lower()}"
        if any(token in lowered for token in ["rechnung", "invoice", "betrag", "ust-id"]):
            return "invoice"
        if any(token in lowered for token in ["angebot", "offer", "quotation"]):
            return "offer"
        if any(token in lowered for token in ["steuer", "tax", "finanzamt"]):
            return "tax"
        if any(token in lowered for token in ["vertrag", "contract", "agreement"]):
            return "contract"
        return "document"

    def _default_tags(self, path: Path, category: str | None) -> list[str]:
        tags = [path.suffix.lower().lstrip(".") or "file"]
        if category:
            tags.append(category)
        return sorted(set(filter(None, tags)))

    def _build_metadata(self, path: Path, text: str, category: str | None, source: str) -> dict[str, object]:
        metadata: dict[str, object] = {
            "source": source,
            "category": category or "document",
            "classification": self._infer_classification(path, text, category),
            "size_bytes": path.stat().st_size if path.exists() else 0,
            "text_length": len(text),
            "line_count": len([line for line in text.splitlines() if line.strip()]),
        }
        if category == "invoice":
            metadata["tracking_state"] = "received"
            due_date = self._extract_due_date(text, ("due", "payment due", "fällig", "faellig"))
            if due_date:
                metadata["due_date"] = due_date
        elif category == "offer":
            metadata["tracking_state"] = "draft"
        elif category == "contract":
            metadata["tracking_state"] = "active"
            due_date = self._extract_due_date(text, ("expires", "expiry", "valid until", "gültig bis", "gueltig bis"))
            if due_date:
                metadata["due_date"] = due_date
        else:
            metadata["tracking_state"] = "indexed"
        return metadata

    def _infer_classification(self, path: Path, text: str, category: str | None) -> str:
        lowered = f"{path.name.lower()} {text[:3000].lower()}"
        if category in {"invoice", "tax"} or any(token in lowered for token in ["iban", "ust-id", "tax id", "finanzamt"]):
            return "finance"
        if category in {"contract"} or any(token in lowered for token in ["nda", "agreement", "vertraulich", "confidential"]):
            return "legal" if "contract" in (category or "") or "agreement" in lowered else "confidential"
        if any(token in lowered for token in ["employee", "bewerbung", "salary", "gehalt", "hr", "personnel"]):
            return "hr"
        if any(token in lowered for token in ["internal use only", "internal", "strategy", "roadmap"]):
            return "internal"
        return "public" if category in {"offer", "document"} and "public" in lowered else "internal"

    def _extract_due_date(self, text: str, cues: tuple[str, ...]) -> str | None:
        lowered = text.lower()
        patterns = [
            re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})"),
            re.compile(r"(?P<date>\d{1,2}[./]\d{1,2}[./]\d{2,4})"),
        ]
        for cue in cues:
            index = lowered.find(cue)
            if index < 0:
                continue
            window = text[index : index + 120]
            for pattern in patterns:
                match = pattern.search(window)
                if match:
                    parsed = self._normalize_date(match.group("date"))
                    if parsed:
                        return parsed
        return None

    def _normalize_date(self, raw: str) -> str | None:
        cleaned = raw.strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y"):
            with contextlib.suppress(ValueError):
                return datetime.strptime(cleaned, fmt).date().isoformat()
        return None

    def _import_available(self, module_name: str) -> bool:
        try:
            __import__(module_name)
        except ImportError:
            return False
        return True

    def _chunk_text(self, document_id: str, text: str, max_chars: int = 900) -> list[DocumentChunk]:
        cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not cleaned:
            return []
        paragraphs = [part.strip() for part in cleaned.split("\n\n") if part.strip()]
        chunks: list[DocumentChunk] = []
        buffer = ""
        chunk_index = 0
        for paragraph in paragraphs:
            candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
            if len(candidate) <= max_chars:
                buffer = candidate
                continue
            if buffer:
                chunks.append(DocumentChunk(document_id=document_id, chunk_index=chunk_index, text=buffer))
                chunk_index += 1
            if len(paragraph) <= max_chars:
                buffer = paragraph
                continue
            start = 0
            while start < len(paragraph):
                piece = paragraph[start : start + max_chars]
                chunks.append(DocumentChunk(document_id=document_id, chunk_index=chunk_index, text=piece))
                chunk_index += 1
                start += max_chars - 120
            buffer = ""
        if buffer:
            chunks.append(DocumentChunk(document_id=document_id, chunk_index=chunk_index, text=buffer))
        return chunks

    def _flatten_archive_payload(self, payload: object) -> list[str]:
        lines: list[str] = []
        if isinstance(payload, list):
            for entry in payload:
                lines.extend(self._flatten_archive_payload(entry))
            return lines
        if isinstance(payload, dict):
            role = str(payload.get("role") or payload.get("author") or payload.get("sender") or "").strip()
            content = payload.get("content") or payload.get("text") or payload.get("message")
            if isinstance(content, list):
                fragments: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("value") or item.get("content")
                        if text:
                            fragments.append(str(text))
                    elif item:
                        fragments.append(str(item))
                content = "\n".join(fragments)
            elif isinstance(content, dict):
                content = content.get("text") or content.get("value") or json.dumps(content)
            if role or content:
                lines.append(f"{role or 'message'}: {str(content or '').strip()}".strip())
            for value in payload.values():
                if isinstance(value, (dict, list)):
                    lines.extend(self._flatten_archive_payload(value))
            return lines
        if payload:
            return [str(payload)]
        return []

    def _copy_into_profile_storage(self, source_path: Path, destination_root: Path) -> Path:
        return self.artifacts.import_file(source_path, destination_root)

    def _logical_suffix(self, path: Path) -> str:
        suffixes = path.suffixes
        if suffixes and suffixes[-1] == ArtifactStore.ENCRYPTED_SUFFIX:
            if len(suffixes) >= 2:
                return suffixes[-2].lower()
            return ""
        return path.suffix.lower()
