from __future__ import annotations

from app.routines import RoutineService
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult


class RunRoutineTool(Tool):
    name = "run_routine"

    def __init__(self, service: RoutineService) -> None:
        self.service = service

    async def run(self, request: ToolRequest) -> ToolResult:
        name = str(request.arguments.get("name", "")).strip()
        if not name:
            return ToolResult(
                success=False,
                status="failed",
                display_text="No routine specified.",
            )
        return self.service.run(name)
