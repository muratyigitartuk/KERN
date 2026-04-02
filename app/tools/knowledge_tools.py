from __future__ import annotations

from typing import Any, TYPE_CHECKING

from app.tools.base import Tool
from app.types import ToolRequest, ToolResult

if TYPE_CHECKING:
    from app.knowledge_graph import KnowledgeGraphService
    from app.documents import DocumentService


class QueryKnowledgeGraphTool(Tool):
    name = "query_knowledge_graph"

    def __init__(self, kg: "KnowledgeGraphService") -> None:
        self._kg = kg

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for entities in the knowledge graph"},
                "depth": {"type": "integer", "description": "Optional neighborhood depth for the top entity (default 1)"},
            },
            "required": ["query"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        query = str(request.arguments.get("query", "")).strip()
        depth = max(1, min(3, int(request.arguments.get("depth", 1) or 1)))
        entities = self._kg.search_entities(query, limit=10)
        if not entities:
            return ToolResult(
                success=True,
                status="observed",
                display_text=f"No entities found for: {query}",
                spoken_text="Nothing found in the knowledge graph.",
                data={"entities": []},
            )
        lines = [f"[{e['type']}] {e['name']}" for e in entities]
        neighborhood = self._kg.get_neighborhood(str(entities[0]["id"]), depth=depth)
        if neighborhood.get("entity"):
            relation_lines: list[str] = []
            for edge in neighborhood.get("edges", [])[:5]:
                other = edge["target"] if edge["source"]["id"] == entities[0]["id"] else edge["source"]
                evidence = edge.get("metadata", {}).get("evidence_samples", [])
                evidence_suffix = f" ({evidence[0][:80]})" if evidence else ""
                relation_lines.append(
                    f"- {edge['relationship']} -> {other['name']} [{other['type']}] depth {edge.get('depth', 1)}{evidence_suffix}"
                )
            if relation_lines:
                lines.append("")
                lines.append(f"Neighborhood for {entities[0]['name']}:")
                lines.extend(relation_lines)
        return ToolResult(
            success=True,
            status="observed",
            display_text="\n".join(lines),
            spoken_text=f"Found {len(entities)} entities matching your query.",
            data={"entities": entities, "neighborhood": neighborhood},
        )


class BuildKnowledgeGraphTool(Tool):
    name = "build_knowledge_graph"

    def __init__(self, kg: "KnowledgeGraphService", document_service: "DocumentService") -> None:
        self._kg = kg
        self._document_service = document_service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max documents to process (default 50)"},
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        limit = int(request.arguments.get("limit", 50))
        total = self._kg.build_from_documents(self._document_service, limit=limit)
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Knowledge graph built. Extracted {total} entities from documents.",
            spoken_text=f"Knowledge graph updated with {total} entities.",
            side_effects=["knowledge_graph_built"],
            data={"entity_count": total},
        )
