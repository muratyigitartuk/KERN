from __future__ import annotations

import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from app.config import settings
from app.documents import DocumentService
from app.local_data import LocalDataService
from app.path_safety import ensure_path_within_roots, validate_user_import_path
from app.spreadsheet import SpreadsheetParser
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult

if TYPE_CHECKING:
    from app.rag import RAGPipeline


class IngestDocumentTool(Tool):
    name = "ingest_document"

    def __init__(self, service: DocumentService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path of the document to ingest"},
                "file_path": {"type": "string", "description": "Alternative file path parameter"},
                "source": {"type": "string", "description": "Source label for the document"},
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        path = str(request.arguments.get("path", "") or request.arguments.get("file_path", "")).strip()
        if not path:
            return ToolResult(status="failed", display_text="I need a document path.")
        record = self.service.ingest_file(path, source=str(request.arguments.get("source", "manual") or "manual"))
        return ToolResult(
            status="observed",
            display_text=f"Ingested {record.title}.",
            evidence=[f"Document stored at {record.file_path}."],
            side_effects=["document_ingested"],
            data={"document": record.model_dump(mode="json")},
        )

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()


class SearchDocumentsTool(Tool):
    name = "search_documents"

    def __init__(self, service: DocumentService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for documents"},
                "scope": {
                    "type": "string",
                    "enum": ["profile", "profile_plus_archive"],
                    "description": "Search scope for documents",
                },
            },
            "required": ["query"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        query = str(request.arguments.get("query", "")).strip()
        scope = str(request.arguments.get("scope", "profile_plus_archive") or "profile_plus_archive")
        if not query:
            return ToolResult(status="failed", display_text="I need a search query.")
        hits = self.service.search(query, scope=scope, limit=6)
        sensitive_hits = [
            hit
            for hit in hits
            if str(hit.metadata.get("classification") or "").strip().lower() in {"confidential", "finance", "legal", "hr"}
        ]
        if settings.policy_mode == "corporate" and settings.policy_restrict_sensitive_reads and sensitive_hits:
            return ToolResult(
                status="failed",
                display_text="Sensitive document hits are restricted in corporate mode. Narrow the query or use a less sensitive corpus.",
                data={"restricted_hits": len(sensitive_hits)},
            )
        if not hits:
            return ToolResult(
                status="observed",
                display_text="No matching documents found.",
                evidence=["No matching document chunks."],
                data={"hits": []},
            )
        summary = "; ".join(f"{hit.metadata.get('title', 'document')}: {hit.text[:120]}" for hit in hits[:3])
        return ToolResult(
            status="observed",
            display_text=f"Document hits: {summary}",
            evidence=[f"Matched {len(hits)} document chunk(s)."],
            data={"hits": [hit.model_dump(mode="json") for hit in hits]},
        )


class ImportConversationArchiveTool(Tool):
    name = "import_conversation_archive"

    def __init__(self, service: DocumentService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path of the conversation archive"},
                "file_path": {"type": "string", "description": "Alternative file path parameter"},
                "source": {"type": "string", "description": "Source label (e.g. chatgpt, other)"},
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        path = str(request.arguments.get("path", "") or request.arguments.get("file_path", "")).strip()
        source = str(request.arguments.get("source", "other") or "other")
        if not path:
            return ToolResult(status="failed", display_text="I need an archive path.")
        try:
            if self.service.profile is not None:
                path = str(validate_user_import_path(path, self.service.profile))
            else:
                path = str(ensure_path_within_roots(path, roots=[self.service.documents_root, self.service.archives_root], reject_symlink=True))
        except ValueError as exc:
            return ToolResult(status="failed", display_text=f"Path denied: {exc}")
        record = self.service.import_conversation_archive(path, source=source)
        return ToolResult(
            status="observed",
            display_text=f"Imported archive {record.title}.",
            evidence=[f"Imported {record.imported_turns} archived turn(s)."],
            side_effects=["conversation_archive_imported"],
            data={"archive": record.model_dump(mode="json")},
        )


class ListDocumentsTool(Tool):
    name = "list_documents"

    def __init__(self, service: DocumentService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Optional category filter for documents"},
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        category = str(request.arguments.get("category", "")).strip() or None
        records = self.service.list_documents(limit=8, category=category)
        sensitive_records = [
            record
            for record in records
            if str(record.classification or "").strip().lower() in {"confidential", "finance", "legal", "hr"}
        ]
        if settings.policy_mode == "corporate" and settings.policy_restrict_sensitive_reads and sensitive_records:
            return ToolResult(
                status="failed",
                display_text="Sensitive document listings are restricted in corporate mode. Filter more narrowly.",
                data={"restricted_documents": len(sensitive_records)},
            )
        if not records:
            return ToolResult(status="observed", display_text="No indexed documents yet.", data={"documents": []})
        summary = ", ".join(record.title for record in records[:4])
        return ToolResult(
            status="observed",
            display_text=f"Indexed documents: {summary}",
            data={"documents": [record.model_dump(mode="json") for record in records]},
        )


class SetMemoryScopeTool(Tool):
    name = "set_memory_scope"

    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["off", "session", "profile", "profile_plus_archive"],
                    "description": "Memory scope level",
                },
                "value": {"type": "string", "description": "Alternative parameter for scope"},
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        scope = str(request.arguments.get("scope", "")).strip() or str(request.arguments.get("value", "")).strip()
        allowed = {"off", "session", "profile", "profile_plus_archive"}
        if scope not in allowed:
            return ToolResult(
                status="failed",
                display_text="Memory scope must be off, session, profile, or profile_plus_archive.",
            )
        self.data.set_preference("memory_scope", scope)
        return ToolResult(
            status="observed",
            display_text=f"Memory scope set to {scope}.",
            side_effects=["memory_scope_updated"],
            data={"scope": scope},
        )


class BulkIngestTool(Tool):
    name = "bulk_ingest"

    def __init__(self, service: DocumentService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "folder_path": {"type": "string", "description": "Folder path to ingest recursively"},
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths to ingest",
                },
                "recursive": {"type": "boolean", "description": "Recurse into subdirectories (default: true)"},
                "category": {"type": "string", "description": "Optional category tag for all ingested documents"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags to apply to all ingested documents",
                },
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        folder_path = str(request.arguments.get("folder_path", "") or "").strip()
        file_paths = request.arguments.get("file_paths") or []
        recursive = bool(request.arguments.get("recursive", True))
        category = str(request.arguments.get("category", "") or "").strip() or None
        raw_tags = request.arguments.get("tags") or []
        tag_list: list[str] = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []

        if folder_path:
            records = self.service.ingest_folder(
                Path(folder_path), recursive=recursive, source="bulk", category=category, tags=tag_list,
            )
        elif file_paths:
            paths = [Path(str(p)) for p in file_paths]
            records = self.service.ingest_batch(paths, source="bulk", category=category, tags=tag_list)
        else:
            return ToolResult(
                status="failed",
                display_text="Provide either folder_path or file_paths to bulk ingest.",
            )

        return ToolResult(
            status="observed",
            display_text=f"Ingested {len(records)} document(s).",
            evidence=[f"Processed {len(records)} file(s)."],
            side_effects=["documents_ingested"],
            data={"count": len(records), "documents": [{"id": r.id, "title": r.title} for r in records]},
        )

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()


class CompareDocumentsTool(Tool):
    name = "compare_documents"

    def __init__(self, service: DocumentService, rag_pipeline: "RAGPipeline | None") -> None:
        self.service = service
        self._rag = rag_pipeline

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "document_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "IDs of the documents to compare",
                },
                "left_document": {
                    "type": "string",
                    "description": "Name of the left document when comparing by document title",
                },
                "right_document": {
                    "type": "string",
                    "description": "Name of the right document when comparing by document title",
                },
                "query": {"type": "string", "description": "Question or comparison request across the documents"},
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        document_ids = [str(item).strip() for item in (request.arguments.get("document_ids") or []) if str(item).strip()]
        left_document = str(request.arguments.get("left_document", "") or "").strip()
        right_document = str(request.arguments.get("right_document", "") or "").strip()
        query = str(request.arguments.get("query", "") or "").strip()

        if (left_document or right_document) and not document_ids:
            resolved_ids: list[str] = []
            unresolved: list[str] = []
            for document_name in [left_document, right_document]:
                if not document_name:
                    continue
                record = self._resolve_document_by_name(document_name)
                if record is None:
                    unresolved.append(document_name)
                    continue
                resolved_ids.append(record.id)
            document_ids = resolved_ids
            if not query and left_document and right_document:
                query = (
                    f"Compare {left_document} and {right_document} and summarize the key differences relevant to the user request."
                )
            if unresolved:
                return ToolResult(
                    status="failed",
                    display_text=f"Could not resolve document(s): {', '.join(unresolved)}.",
                    data={"unresolved_documents": unresolved},
                )

        if not document_ids or not query:
            return ToolResult(
                status="failed",
                display_text="Provide document IDs or named documents and a query to compare documents.",
            )
        if self._rag is None:
            return ToolResult(
                status="failed",
                display_text="RAG pipeline is not available.",
            )
        result = await self._rag.answer_multi_document(query, [str(d) for d in document_ids])
        return ToolResult(
            status="observed",
            display_text=result.answer or "No relevant content found.",
            evidence=[f"Retrieved from {len(result.sources)} source(s)."],
            data={
                "answer": result.answer,
                "sources": [s.model_dump(mode="json") for s in result.sources],
                "document_ids": document_ids,
                "left_document": left_document or None,
                "right_document": right_document or None,
            },
        )

    def availability(self) -> tuple[bool, str | None]:
        if self._rag is None:
            return False, "RAG pipeline is not initialised."
        return True, None

    def _resolve_document_by_name(self, document_name: str):
        target = self._normalize_document_name(document_name)
        if not target:
            return None
        candidates = self.service.list_documents(limit=500, audit=False)
        exact_match = next(
            (
                record
                for record in candidates
                if self._normalize_document_name(record.title) == target
            ),
            None,
        )
        if exact_match is not None:
            return exact_match
        path_match = next(
            (
                record
                for record in candidates
                if self._normalize_document_name(Path(record.file_path).stem) == target
            ),
            None,
        )
        if path_match is not None:
            return path_match
        fuzzy_hits = self.service.search(document_name, scope="profile_plus_archive", limit=6)
        for hit in fuzzy_hits:
            title = str(hit.metadata.get("title") or "").strip()
            if self._normalize_document_name(title) == target:
                source_id = str(hit.metadata.get("document_id") or hit.source_id or "").strip()
                if source_id:
                    return next((record for record in candidates if record.id == source_id), None)
        return None

    def _normalize_document_name(self, value: str) -> str:
        cleaned = value.strip().lower()
        cleaned = Path(cleaned).stem
        cleaned = re.sub(r"[^0-9a-z_\\-]+", "", cleaned)
        return cleaned


class SummarizeDocumentTool(Tool):
    name = "summarize_document"

    def __init__(self, service: DocumentService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "document_name": {
                    "type": "string",
                    "description": "Name of the local document to summarize",
                },
            },
            "required": ["document_name"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        document_name = str(request.arguments.get("document_name", "") or "").strip()
        if not document_name:
            return ToolResult(
                status="failed",
                display_text="Provide a document_name to summarize.",
            )
        record = self._resolve_document_by_name(document_name)
        if record is None:
            return ToolResult(
                status="failed",
                display_text=f"Could not resolve document '{document_name}'.",
                data={"document_name": document_name},
            )
        text = self.service.extract_text(record.file_path)
        summary = self._summarize_text(text, record.title)
        return ToolResult(
            status="observed",
            display_text=summary,
            evidence=[f"Summarized local document {record.title}."],
            data={
                "document_id": record.id,
                "document_name": record.title,
                "summary": summary,
            },
        )

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    def _resolve_document_by_name(self, document_name: str):
        target = self._normalize_document_name(document_name)
        if not target:
            return None
        candidates = self.service.list_documents(limit=500, audit=False)
        for record in candidates:
            if self._normalize_document_name(record.title) == target:
                return record
            if self._normalize_document_name(Path(record.file_path).stem) == target:
                return record
        fuzzy_hits = self.service.search(document_name, scope="profile_plus_archive", limit=6)
        for hit in fuzzy_hits:
            title = str(hit.metadata.get("title") or "").strip()
            if self._normalize_document_name(title) == target:
                source_id = str(hit.metadata.get("document_id") or hit.source_id or "").strip()
                if source_id:
                    return next((record for record in candidates if record.id == source_id), None)
        return None

    def _normalize_document_name(self, value: str) -> str:
        cleaned = value.strip().lower()
        cleaned = Path(cleaned).stem
        cleaned = re.sub(r"[^0-9a-z_\\-]+", "", cleaned)
        return cleaned

    def _summarize_text(self, text: str, title: str) -> str:
        normalized_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]
        if not normalized_lines:
            return f"{title}: Keine verwertbaren Inhalte gefunden."
        summary_points: list[str] = []
        for line in normalized_lines:
            cleaned = re.sub(r"\\s+", " ", line).strip()
            if len(cleaned) < 8:
                continue
            if cleaned not in summary_points:
                summary_points.append(cleaned)
            if len(summary_points) >= 3:
                break
        if not summary_points:
            compact = re.sub(r"\\s+", " ", text).strip()
            compact = compact[:240].rstrip()
            return f"{title}: {compact}" if compact else f"{title}: Keine verwertbaren Inhalte gefunden."
        return f"{title}: " + " | ".join(summary_points)


class QuerySpreadsheetTool(Tool):
    name = "query_spreadsheet"

    def __init__(self, service: DocumentService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the CSV or Excel spreadsheet file"},
                "query": {"type": "string", "description": "Question or aggregation request (e.g. 'sum of column Revenue')"},
            },
            "required": ["file_path", "query"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        file_path = str(request.arguments.get("file_path", "") or "").strip()
        query = str(request.arguments.get("query", "") or "").strip()
        if not file_path or not query:
            return ToolResult(
                status="failed",
                display_text="Provide file_path and a query to analyse a spreadsheet.",
            )
        if self.service.platform and self.service.profile:
            self.service.platform.assert_profile_unlocked(
                self.service.profile.slug,
                "documents",
                "query_spreadsheet",
            )

        try:
            if self.service.profile is not None:
                path = validate_user_import_path(file_path, self.service.profile)
            else:
                path = ensure_path_within_roots(file_path, roots=[self.service.documents_root, self.service.archives_root], reject_symlink=True)
        except ValueError as exc:
            return ToolResult(status="failed", display_text=f"Path denied: {exc}")
        indexed_row = self.service.memory.connection.execute(
            """
            SELECT metadata_json
            FROM document_records
            WHERE profile_slug = ? AND file_path = ?
            LIMIT 1
            """,
            (self.service.profile.slug if self.service.profile else "default", str(path)),
        ).fetchone()
        if indexed_row:
            metadata = json.loads(indexed_row["metadata_json"] or "{}")
            classification = str(metadata.get("classification") or "internal").strip().lower()
            if (
                settings.policy_mode == "corporate"
                and settings.policy_restrict_sensitive_reads
                and classification in {"confidential", "finance", "legal", "hr"}
            ):
                return ToolResult(
                    status="failed",
                    display_text=f"{classification.title()} spreadsheet reads are restricted in corporate mode.",
                    data={"classification": classification},
                )

        with self.service.artifacts.temporary_plaintext(path) as plaintext_path:
            suffix = plaintext_path.suffix.lower()
            if suffix == ".csv":
                data: list[dict] = SpreadsheetParser.parse_csv(plaintext_path)
            elif suffix in {".xlsx", ".xls"}:
                sheets = SpreadsheetParser.parse_excel(plaintext_path)
                data = [row for rows in sheets.values() for row in rows]
            else:
                return ToolResult(status="failed", display_text="Only CSV and Excel files are supported.")

        if not data:
            return ToolResult(status="observed", display_text="The spreadsheet is empty.")

        answer = SpreadsheetParser.query_dataframe(data, query)
        natural = SpreadsheetParser.to_natural_language(data, title=path.stem)
        query_kind = "summary"
        lowered_query = query.lower()
        if "group by" in lowered_query:
            query_kind = "grouped_analysis"
        elif "sum" in lowered_query or "total" in lowered_query:
            query_kind = "aggregation_sum"
        elif "average" in lowered_query or "avg" in lowered_query or "mean" in lowered_query:
            query_kind = "aggregation_average"
        elif "count" in lowered_query or "how many" in lowered_query:
            query_kind = "aggregation_count"
        elif "where " in lowered_query or "contains " in lowered_query:
            query_kind = "filtered_rows"
        return ToolResult(
            status="observed",
            display_text=answer,
            evidence=[natural[:500]],
            data={
                "answer": answer,
                "rows": len(data),
                "columns": list(data[0].keys()) if data else [],
                "query_kind": query_kind,
                "file_path": str(path),
            },
        )

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()
