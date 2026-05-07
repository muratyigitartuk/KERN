from __future__ import annotations

from typing import Any

from app.local_data import LocalDataService
from app.tools.base import Tool
from app.types import CalendarActionPlan, ToolRequest, ToolResult


class CalendarService:
    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    async def get_today_events(self):
        return self.data.list_today_events()

    def create_event(
        self,
        title: str,
        starts_at,
        ends_at=None,
        importance: int = 0,
    ) -> int:
        return self.data.create_event(title, starts_at, ends_at=ends_at, importance=importance)

    def delete_event(self, event_id: int) -> bool:
        return self.data.delete_event(event_id)

    def schedule_meeting(self, plan: CalendarActionPlan) -> CalendarActionPlan:
        event_id = self.create_event(plan.title, plan.starts_at, plan.ends_at, importance=3)
        return plan.model_copy(update={"event_id": event_id, "invite_status": "draft_only"})


class TodayCalendarTool(Tool):
    name = "get_today_calendar"

    def __init__(self, service: CalendarService) -> None:
        self.service = service

    def parameter_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, request: ToolRequest) -> ToolResult:
        events = await self.service.get_today_events()
        if not events:
            return ToolResult(
                success=True,
                status="observed",
                display_text="No events today.",
                evidence=["No calendar events found for today."],
                data={"events": []},
            )
        summary = ", ".join(event.title for event in events[:3])
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Today's agenda: {summary}",
            evidence=[f"Loaded {len(events)} calendar event(s)."],
            data={"events": [event.model_dump(mode='json') for event in events]},
        )
