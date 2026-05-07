from __future__ import annotations

from typing import Any

from app.local_data import LocalDataService
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult


class TaskService:
    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    async def get_pending_tasks(self):
        return self.data.list_pending_tasks()

    async def create_task(self, title: str) -> ToolResult:
        task_id = self.data.create_task(title, priority=2)
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Added local task: {title}",
            evidence=[f"Created task #{task_id}."],
            side_effects=["task_created", "open_loop_created"],
            data={"source": "local", "task_id": task_id},
        )

    async def complete_task(self, title: str) -> ToolResult:
        completed = self.data.complete_task(title)
        if not completed:
            return ToolResult(
                success=False,
                status="failed",
                display_text=f"I could not find an active task named {title}.",
                data={"source": "local"},
            )
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Completed task: {title}",
            evidence=[f"Marked task {title} complete."],
            side_effects=["task_completed", "open_loop_resolved"],
            data={"source": "local"},
        )


class PendingTasksTool(Tool):
    name = "get_pending_tasks"

    def __init__(self, service: TaskService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, request: ToolRequest) -> ToolResult:
        tasks = await self.service.get_pending_tasks()
        if not tasks:
            return ToolResult(
                success=True,
                status="observed",
                display_text="No pending tasks.",
                evidence=["Task list is empty."],
                data={"tasks": []},
            )
        summary = ", ".join(task.title for task in tasks[:3])
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Top tasks: {summary}",
            evidence=[f"Loaded {len(tasks)} active tasks."],
            data={"tasks": [task.model_dump(mode='json') for task in tasks]},
        )


class CreateTaskTool(Tool):
    name = "create_task"

    def __init__(self, service: TaskService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the task to create"},
            },
            "required": ["title"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        title = str(request.arguments.get("title", "")).strip()
        if not title:
            return ToolResult(
                success=False,
                status="failed",
                display_text="No task title provided.",
            )
        return await self.service.create_task(title)


class CompleteTaskTool(Tool):
    name = "complete_task"

    def __init__(self, service: TaskService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title of the task to mark complete"},
            },
            "required": ["title"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        title = str(request.arguments.get("title", "")).strip()
        if not title:
            return ToolResult(
                success=False,
                status="failed",
                display_text="No task title provided.",
            )
        return await self.service.complete_task(title)
