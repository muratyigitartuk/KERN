from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.tools.base import Tool
from app.types import ToolRequest, ToolResult

if TYPE_CHECKING:
    from app.scheduler import SchedulerService


class CreateScheduleTool(Tool):
    name = "create_schedule"

    def __init__(self, get_scheduler) -> None:
        self._get = get_scheduler  # callable → SchedulerService | None

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Human-readable name for the scheduled task"},
                "cron_expression": {
                    "type": "string",
                    "description": "Cron expression or preset: '0 8 * * *' (daily 08:00), '0 8 * * 1' (weekly), '0 8 1 * *' (monthly)",
                },
                "action_type": {
                    "type": "string",
                    "enum": ["custom_prompt", "summarize_emails", "generate_report"],
                    "description": "Type of action to run",
                },
                "prompt": {"type": "string", "description": "Prompt to run for custom_prompt action type"},
                "urgent_only": {"type": "boolean", "description": "Only urgent messages for summarize_emails"},
                "today_only": {"type": "boolean", "description": "Only today for summarize_emails"},
                "max_retries": {"type": "integer", "description": "Retry budget before the scheduler gives up until the next cron run"},
            },
            "required": ["title", "cron_expression"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        scheduler = self._get()
        if scheduler is None:
            return ToolResult(status="failed", display_text="Scheduler is not available.", spoken_text="Scheduling is not enabled.")
        title = str(request.arguments.get("title", "")).strip()
        cron = str(request.arguments.get("cron_expression", "0 8 * * *")).strip()
        action_type = str(request.arguments.get("action_type", "custom_prompt") or "custom_prompt")
        prompt = str(request.arguments.get("prompt", "") or "").strip()
        max_retries = int(request.arguments.get("max_retries", 2) or 2)
        if not title:
            return ToolResult(status="failed", display_text="A title is required.", spoken_text="Please provide a title for the schedule.")
        action_payload: dict[str, Any]
        if action_type == "summarize_emails":
            action_payload = {
                "urgent_only": bool(request.arguments.get("urgent_only", False)),
                "today_only": bool(request.arguments.get("today_only", False)),
            }
        elif action_type == "generate_report":
            action_payload = {"prompt": prompt} if prompt else {}
        elif action_type != "custom_prompt":
            return ToolResult(
                status="failed",
                display_text=f"Unsupported action type: {action_type}.",
                spoken_text="That scheduled action type is not supported.",
            )
        else:
            action_payload = {"prompt": prompt} if prompt else {}
        task = scheduler.create_task(title, cron, action_type, action_payload, max_retries=max_retries)
        return ToolResult(
            status="observed",
            display_text=f"Schedule created: {title} ({cron})",
            spoken_text=f"I've scheduled {title}.",
            side_effects=["schedule_created"],
            data={"task": task},
        )

    def availability(self) -> tuple[bool, str | None]:
        return self._get() is not None, None if self._get() is not None else "Scheduler not available."


class ListSchedulesTool(Tool):
    name = "list_schedules"

    def __init__(self, get_scheduler) -> None:
        self._get = get_scheduler

    def parameter_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, request: ToolRequest) -> ToolResult:
        scheduler = self._get()
        if scheduler is None:
            return ToolResult(status="failed", display_text="Scheduler is not available.", spoken_text="Scheduling is not enabled.")
        tasks = scheduler.list_tasks()
        if not tasks:
            return ToolResult(status="observed", display_text="No scheduled tasks.", spoken_text="No scheduled tasks yet.", data={"tasks": []})
        summary = "; ".join(f"{t['title']} ({t['cron_expression']})" for t in tasks[:5])
        return ToolResult(
            status="observed",
            display_text=f"Schedules: {summary}",
            spoken_text=f"You have {len(tasks)} scheduled task(s).",
            data={"tasks": tasks},
        )

    def availability(self) -> tuple[bool, str | None]:
        return self._get() is not None, None if self._get() is not None else "Scheduler not available."


class ManageScheduleTool(Tool):
    name = "manage_schedule"

    def __init__(self, get_scheduler) -> None:
        self._get = get_scheduler

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["enable", "disable", "delete"],
                    "description": "Management action to perform",
                },
                "schedule_id": {"type": "string", "description": "ID of the scheduled task"},
            },
            "required": ["action", "schedule_id"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        scheduler = self._get()
        if scheduler is None:
            return ToolResult(status="failed", display_text="Scheduler is not available.", spoken_text="Scheduling is not enabled.")
        action = str(request.arguments.get("action", "")).strip()
        schedule_id = str(request.arguments.get("schedule_id", "")).strip()
        if not action or not schedule_id:
            return ToolResult(status="failed", display_text="action and schedule_id are required.", spoken_text="I need an action and a schedule ID.")
        if action == "delete":
            ok = scheduler.delete_task(schedule_id)
            return ToolResult(
                status="observed" if ok else "failed",
                display_text="Schedule deleted." if ok else "Schedule not found.",
                spoken_text="Done." if ok else "I couldn't find that schedule.",
                side_effects=["schedule_deleted"] if ok else [],
            )
        enabled = action == "enable"
        ok = scheduler.toggle_task(schedule_id, enabled)
        label = "enabled" if enabled else "disabled"
        return ToolResult(
            status="observed" if ok else "failed",
            display_text=f"Schedule {label}." if ok else "Schedule not found.",
            spoken_text=f"Schedule {label}." if ok else "I couldn't find that schedule.",
            side_effects=[f"schedule_{label}"] if ok else [],
        )

    def availability(self) -> tuple[bool, str | None]:
        return self._get() is not None, None if self._get() is not None else "Scheduler not available."


class WatchFolderTool(Tool):
    name = "watch_folder"

    def __init__(self, get_file_watcher) -> None:
        self._get = get_file_watcher  # callable → FileWatcher | None

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "folder_path": {"type": "string", "description": "Absolute path to the folder to watch for new files"},
            },
            "required": ["folder_path"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        watcher = self._get()
        folder_path = str(request.arguments.get("folder_path", "") or "").strip()
        if not folder_path:
            return ToolResult(status="failed", display_text="folder_path is required.", spoken_text="Tell me which folder to watch.")
        path = Path(folder_path)
        if not path.is_dir():
            return ToolResult(status="failed", display_text=f"Not a valid directory: {folder_path}", spoken_text="That directory doesn't exist.")
        if watcher is None:
            return ToolResult(status="failed", display_text="File watcher is not available.", spoken_text="File watching is not active.")
        if not watcher.add_directory(path):
            return ToolResult(status="failed", display_text=f"Unable to watch: {folder_path}", spoken_text="I couldn't add that folder to the watch list.")
        return ToolResult(
            status="observed",
            display_text=f"Now watching: {path.name}",
            spoken_text=f"I'll monitor {path.name} for new files.",
            side_effects=["watch_rule_added"],
            data={"folder": str(path)},
        )

    def availability(self) -> tuple[bool, str | None]:
        watcher = self._get()
        return watcher is not None, None if watcher is not None else "File watching is not active."
