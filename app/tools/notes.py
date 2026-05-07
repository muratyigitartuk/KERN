from __future__ import annotations

from typing import Any

from app.local_data import LocalDataService
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult


class NoteTool(Tool):
    name = "create_note"

    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Text content of the note"},
            },
            "required": ["content"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        content = str(request.arguments.get("content", "")).strip()
        if not content:
            return ToolResult(
                success=False,
                status="failed",
                display_text="No note content was provided.",
            )
        note_id = self.data.create_note(content)
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Saved note #{note_id}.",
            evidence=[f"Created note #{note_id}."],
            side_effects=["note_saved"],
            data={"note_id": note_id},
        )


class ListNotesTool(Tool):
    name = "list_notes"

    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    def parameter_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, request: ToolRequest) -> ToolResult:
        notes = self.data.list_notes(limit=5)
        if not notes:
            return ToolResult(
                success=True,
                status="observed",
                display_text="No notes saved yet.",
                evidence=["Notes list is empty."],
                data={"notes": []},
            )
        summary = "; ".join(notes[:3])
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Recent notes: {summary}",
            evidence=[f"Loaded {len(notes)} note(s)."],
            data={"notes": notes},
        )
