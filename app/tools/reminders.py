from __future__ import annotations

from datetime import datetime
from typing import Any

from app.reminders import ReminderService
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult


class CreateReminderTool(Tool):
    name = "create_reminder"

    def __init__(self, service: ReminderService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Reminder title or description"},
                "due_at": {"type": "string", "description": "ISO 8601 datetime when the reminder is due"},
                "kind": {"type": "string", "description": "Type of reminder (e.g. reminder, timer)"},
            },
            "required": ["title", "due_at"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        title = str(request.arguments.get("title", "")).strip()
        due_at_raw = str(request.arguments.get("due_at", "")).strip()
        kind = str(request.arguments.get("kind", "reminder")).strip() or "reminder"
        if not title or not due_at_raw:
            return ToolResult(
                success=False,
                status="failed",
                display_text="Reminder is missing a title or time.",
            )
        normalized_due_at = due_at_raw[:-1] + "+00:00" if due_at_raw.endswith("Z") else due_at_raw
        due_at = datetime.fromisoformat(normalized_due_at)
        reminder_id = self.service.create_reminder(title, due_at, kind=kind)
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Created {kind} #{reminder_id} for {title}.",
            evidence=[f"Stored {kind} #{reminder_id}."],
            side_effects=["reminder_created", "open_loop_created"],
            data={"reminder_id": reminder_id, "due_at": due_at.isoformat(), "kind": kind},
        )


class SnoozeReminderTool(Tool):
    name = "snooze_reminder"

    def __init__(self, service: ReminderService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "integer", "description": "ID of the reminder to snooze"},
                "minutes": {"type": "integer", "description": "Number of minutes to snooze"},
            },
            "required": ["reminder_id"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        reminder_id = int(request.arguments.get("reminder_id", 0))
        minutes = int(request.arguments.get("minutes", 10))
        if reminder_id <= 0:
            return ToolResult(
                success=False,
                status="failed",
                display_text="No reminder was selected.",
            )
        self.service.snooze(reminder_id, minutes=minutes)
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Snoozed reminder #{reminder_id} by {minutes} minutes.",
            evidence=[f"Updated reminder #{reminder_id} due time."],
            side_effects=["reminder_snoozed"],
            data={"reminder_id": reminder_id, "minutes": minutes},
        )


class DismissReminderTool(Tool):
    name = "dismiss_reminder"

    def __init__(self, service: ReminderService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "integer", "description": "ID of the reminder to dismiss"},
            },
            "required": ["reminder_id"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        reminder_id = int(request.arguments.get("reminder_id", 0))
        if reminder_id <= 0:
            return ToolResult(
                success=False,
                status="failed",
                display_text="No reminder was selected.",
            )
        self.service.dismiss(reminder_id)
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Dismissed reminder #{reminder_id}.",
            evidence=[f"Marked reminder #{reminder_id} complete."],
            side_effects=["reminder_dismissed", "open_loop_resolved"],
            data={"reminder_id": reminder_id},
        )
