from __future__ import annotations

from datetime import datetime, timedelta

from app.local_data import LocalDataService
from app.types import ToolResult


class RoutineService:
    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    def run(self, name: str) -> ToolResult:
        normalized = name.strip().lower()
        if normalized == "morning":
            return self._morning()
        if normalized == "focus":
            return self._focus()
        if normalized == "shutdown":
            return self._shutdown()
        return ToolResult(
            success=False,
            status="failed",
            display_text=f"No local routine named {name}.",
            data={"routine": normalized},
        )

    def _morning(self) -> ToolResult:
        brief = self.data.build_morning_brief()
        steps = self.data.get_routine("morning")
        pieces = [brief.focus_suggestion]
        if brief.next_event:
            next_event = brief.next_event["title"]
            pieces.append(f"Your first event is {next_event}.")
        if brief.tasks:
            pieces.append(f"Your top task is {brief.tasks[0]['title']}.")
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Ran morning routine: {', '.join(steps)}.",
            evidence=[f"Morning routine used {len(steps)} configured step(s)."],
            data={"routine": "morning", "steps": steps, "morning_brief": brief.model_dump(mode="json")},
        )

    def _focus(self) -> ToolResult:
        steps = self.data.get_routine("focus")
        tasks = self.data.list_pending_tasks()
        top_task = tasks[0].title if tasks else "your highest-priority work"
        due_at = datetime.now() + timedelta(minutes=50)
        reminder_id = self.data.create_reminder("End focus block", due_at, kind="timer")
        self.data.set_assistant_mode("focus")
        self.data.set_focus_until(due_at)
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Ran focus routine and set timer #{reminder_id}.",
            evidence=[f"Focus timer #{reminder_id} due at {due_at.strftime('%H:%M')}."],
            side_effects=["focus_mode_started", "timer_created"],
            data={"routine": "focus", "steps": steps, "reminder_id": reminder_id, "due_at": due_at.isoformat()},
        )

    def _shutdown(self) -> ToolResult:
        steps = self.data.get_routine("shutdown")
        remaining = self.data.list_pending_tasks()
        count = len(remaining)
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Ran shutdown routine with {count} remaining tasks.",
            evidence=[f"Shutdown routine reviewed {count} remaining task(s)."],
            data={"routine": "shutdown", "steps": steps, "remaining_tasks": [task.model_dump(mode='json') for task in remaining[:5]]},
        )
