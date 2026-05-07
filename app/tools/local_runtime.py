from __future__ import annotations

from datetime import datetime
from typing import Any

from app.local_data import LocalDataService
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult


class SetPreferenceTool(Tool):
    name = "set_preference"

    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Preference key to set"},
                "value": {"type": "string", "description": "Preference value"},
            },
            "required": ["key", "value"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        key = str(request.arguments.get("key", "")).strip()
        value = str(request.arguments.get("value", "")).strip()
        if not key or not value:
            return ToolResult(
                success=False,
                status="failed",
                display_text="Preference update is missing data.",
            )
        self.data.set_preference(key, value)
        if key == "preferred_title":
            return ToolResult(
                success=True,
                status="observed",
                display_text=f"Updated preferred title to {value}.",
                evidence=[f"Preference {key} stored."],
                side_effects=["preference_updated"],
                data={"key": key, "value": value},
            )
        if key == "muted":
            muted = value.lower() == "true"
            return ToolResult(
                success=True,
                status="observed",
                display_text="Paused assistant interruptions." if muted else "Resumed assistant interruptions.",
                evidence=[f"Muted set to {muted}."],
                side_effects=["preference_updated"],
                data={"key": key, "value": value, "runtime_muted": muted},
            )
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Updated {key}.",
            evidence=[f"Preference {key} stored."],
            side_effects=["preference_updated"],
            data={"key": key, "value": value},
        )


class ReadStatusTool(Tool):
    name = "read_status"

    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    def parameter_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, request: ToolRequest) -> ToolResult:
        status = self.data.status_summary()
        title = status["preferred_title"]
        task_count = status["pending_task_count"]
        event_count = status["today_event_count"]
        reminder_count = status["pending_reminder_count"]
        summary = (
            f"Status is stable. You have {task_count} pending tasks, {event_count} events today, "
            f"and {reminder_count} reminders pending."
        )
        if status["muted"]:
            summary = f"{summary} Assistant interruptions are currently paused."
        return ToolResult(
            success=True,
            status="observed",
            display_text=summary,
            evidence=["Read runtime summary from local memory."],
            data=status,
        )


class GenerateMorningBriefTool(Tool):
    name = "generate_morning_brief"

    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    def parameter_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, request: ToolRequest) -> ToolResult:
        brief = self.data.build_morning_brief()
        pieces = ["Good morning.", brief.focus_suggestion]
        if brief.next_event:
            starts_at = datetime.fromisoformat(brief.next_event["starts_at"])
            pieces.append(f"Your next event is {brief.next_event['title']} at {starts_at.strftime('%H:%M')}.")
        elif brief.events:
            pieces.append("You have no more events ahead today.")
        else:
            pieces.append("Your calendar is light today.")
        if brief.tasks:
            pieces.append(f"Your top task is {brief.tasks[0]['title']}.")
        if brief.reminders:
            pieces.append(f"You also have {len(brief.reminders)} reminders pending.")
        text = " ".join(pieces[:5])
        return ToolResult(
            success=True,
            status="observed",
            display_text=text,
            evidence=["Generated morning brief from local reminders, tasks, and calendar."],
            data={"morning_brief": brief.model_dump(mode="json")},
        )
