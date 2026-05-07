from __future__ import annotations

import re
from typing import Any, TYPE_CHECKING

from app.documents import DocumentService
from app.local_data import LocalDataService
from app.retrieval import RetrievalService
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult

if TYPE_CHECKING:
    from app.memory import MemoryRepository


def _derive_fact_key(value: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return (base[:32] or "memory_note").strip("_")


def _infer_memory_kind(key: str, value: str) -> str:
    lowered = f"{key} {value}".lower()
    if any(token in lowered for token in ("prefer", "preference", "preferred", "response style", "editor")):
        return "preference"
    if any(token in lowered for token in ("decision", "decided", "agreed")):
        return "decision"
    if any(token in lowered for token in ("todo", "follow up", "next step", "need to", "should")):
        return "commitment"
    return "fact"


class RememberFactTool(Tool):
    name = "remember_fact"

    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short label for the fact"},
                "value": {"type": "string", "description": "The fact content to remember"},
                "facts": {
                    "type": "array",
                    "description": "Batch of facts to remember",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "value": {"type": "string"},
                            "memory_kind": {"type": "string"},
                            "entity_key": {"type": "string"},
                        },
                        "required": ["value"],
                    },
                },
                "memory_kind": {"type": "string", "description": "Optional memory type such as preference, fact, decision, or commitment"},
                "entity_key": {"type": "string", "description": "Optional linked entity or subject for the memory item"},
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        facts_payload = request.arguments.get("facts")
        if isinstance(facts_payload, list) and facts_payload:
            stored: list[dict[str, str]] = []
            for item in facts_payload:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", "")).strip() or _derive_fact_key(str(item.get("value", "")).strip())
                value = str(item.get("value", "")).strip()
                if not value:
                    continue
                memory_kind = str(item.get("memory_kind", "")).strip() or _infer_memory_kind(key, value)
                entity_key = str(item.get("entity_key", "")).strip() or None
                self.data.remember_fact(
                    key,
                    value,
                    memory_kind=memory_kind,
                    entity_key=entity_key,
                    provenance={"origin": "remember_fact_tool"},
                )
                stored.append({"key": key, "value": value, "memory_kind": memory_kind})
            if not stored:
                return ToolResult(
                    success=False,
                    status="failed",
                    display_text="I need something concrete to remember.",
                )
            summary = "; ".join(f"{item['key']}: {item['value']}" for item in stored)
            return ToolResult(
                success=True,
                status="observed",
                display_text=f"Remembered: {summary}.",
                evidence=[f"Stored {len(stored)} fact(s)."],
                side_effects=["memory_written"],
                data={"facts": stored},
            )
        key = str(request.arguments.get("key", "")).strip()
        value = str(request.arguments.get("value", "")).strip()
        if not value:
            return ToolResult(
                success=False,
                status="failed",
                display_text="I need something concrete to remember.",
            )
        if not key:
            key = _derive_fact_key(value)
        memory_kind = str(request.arguments.get("memory_kind", "")).strip() or _infer_memory_kind(key, value)
        entity_key = str(request.arguments.get("entity_key", "")).strip() or None
        self.data.remember_fact(
            key,
            value,
            memory_kind=memory_kind,
            entity_key=entity_key,
            provenance={"origin": "remember_fact_tool"},
        )
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Remembered {memory_kind} {key}: {value}.",
            evidence=[f"Stored {memory_kind} under {key}."],
            side_effects=["memory_written"],
            data={"key": key, "value": value, "memory_kind": memory_kind, "entity_key": entity_key},
        )


class RecallMemoryTool(Tool):
    name = "recall_memory"

    def __init__(
        self,
        data: LocalDataService,
        document_service: DocumentService | None = None,
        retrieval_service: RetrievalService | None = None,
    ) -> None:
        self.data = data
        self.document_service = document_service
        self.retrieval_service = retrieval_service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to recall facts or documents"},
            },
            "required": ["query"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        query = str(request.arguments.get("query", "")).strip()
        normalized_query = self._normalize_query(query)
        memory_scope = self.data.get_preference("memory_scope", "profile") or "profile"
        facts = self.data.recall_facts(normalized_query, limit=5, scope=memory_scope)
        document_hits = []
        retrieval_hits = []
        if self.retrieval_service and normalized_query and memory_scope in {"profile", "profile_plus_archive"}:
            retrieval_hits = self.retrieval_service.retrieve(normalized_query, scope=memory_scope, limit=5)
        elif self.document_service and normalized_query and memory_scope in {"profile", "profile_plus_archive"}:
            document_hits = self.document_service.search(normalized_query, scope=memory_scope, limit=3)
        if not facts and self._is_preference_query(query):
            facts = self._preference_facts(limit=5)
        if not facts and not document_hits and not retrieval_hits:
            return ToolResult(
                success=True,
                status="observed",
                display_text="I do not have anything remembered for that yet.",
                evidence=["No matching facts found."],
                data={"facts": [], "document_hits": [], "retrieval_hits": []},
            )
        summary = "; ".join(f"{fact.memory_kind} / {fact.key}: {fact.value}" for fact in facts[:3])
        if retrieval_hits:
            retrieval_summary = "; ".join(f"{hit.metadata.get('title', 'entry')}: {hit.text[:80]}" for hit in retrieval_hits[:2])
            summary = f"{summary}; retrieval: {retrieval_summary}" if summary else f"retrieval: {retrieval_summary}"
        elif document_hits:
            document_summary = "; ".join(
                f"{hit.metadata.get('title', 'document')}: {hit.text[:80]}"
                for hit in document_hits[:2]
            )
            summary = f"{summary}; documents: {document_summary}" if summary else f"documents: {document_summary}"
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Remembered: {summary}",
            evidence=[f"Matched {len(facts)} fact(s) and {len(document_hits)} document hit(s)."],
            data={
                "facts": [fact.model_dump(mode='json') for fact in facts],
                "document_hits": [hit.model_dump(mode="json") for hit in document_hits],
                "retrieval_hits": [hit.model_dump(mode="json") for hit in retrieval_hits],
                "memory_scope": memory_scope,
            },
        )

    def _normalize_query(self, query: str) -> str:
        lowered = query.lower().strip()
        aliases = {
            "my working preferences": "preferences",
            "working preferences": "preferences",
            "what do you remember about me": "preferences",
            "preferences": "preferences",
        }
        return aliases.get(lowered, lowered)

    def _is_preference_query(self, query: str) -> bool:
        lowered = query.lower().strip()
        return any(token in lowered for token in {"preference", "preferences", "working preferences", "about me"})

    def _preference_facts(self, limit: int) -> list:
        facts = self.data.list_facts(limit=25)
        preference_tokens = ("prefer", "preferred", "preference", "response style", "editor", "concise")
        return [
            fact
            for fact in facts
            if fact.memory_kind == "preference" or any(token in f"{fact.key} {fact.value}".lower() for token in preference_tokens)
        ][:limit]


class SearchConversationHistoryTool(Tool):
    name = "search_conversation_history"

    def __init__(self, memory: "MemoryRepository") -> None:
        self.memory = memory

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for in conversation history"},
                "date_from": {"type": "string", "description": "ISO date lower bound (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "ISO date upper bound (YYYY-MM-DD)"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
            "required": ["query"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        query = str(request.arguments.get("query", "")).strip()
        date_from = str(request.arguments.get("date_from", "") or "").strip() or None
        date_to = str(request.arguments.get("date_to", "") or "").strip() or None
        limit = int(request.arguments.get("limit", 20))
        hits = self.memory.search_conversation_history(query, date_from=date_from, date_to=date_to, limit=limit)
        if not hits:
            return ToolResult(
                success=True,
                status="observed",
                display_text="No matching conversation history found.",
                data={"hits": []},
            )
        lines = [f"[{h['date']}] {h['content'][:120]}" for h in hits[:10]]
        return ToolResult(
            success=True,
            status="observed",
            display_text="\n".join(lines),
            data={"hits": hits},
        )


class BuildTopicTimelineTool(Tool):
    name = "build_topic_timeline"

    def __init__(self, memory: "MemoryRepository") -> None:
        self.memory = memory

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic or keyword to build a timeline for"},
            },
            "required": ["topic"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        topic = str(request.arguments.get("topic", "")).strip()
        timeline = self.memory.build_topic_timeline(topic)
        if not timeline:
            return ToolResult(
                success=True,
                status="observed",
                display_text=f"No conversation history found for topic: {topic}",
                data={"timeline": []},
            )
        summary_lines = []
        for group in timeline[:5]:
            count = len(group["entries"])
            date = group["date"]
            snippet = group["entries"][0]["content"][:80] if group["entries"] else ""
            summary_lines.append(f"{date} ({count} turn(s)): {snippet}â€¦")
        return ToolResult(
            success=True,
            status="observed",
            display_text="\n".join(summary_lines),
            data={"timeline": timeline},
        )
