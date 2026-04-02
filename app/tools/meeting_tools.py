from __future__ import annotations

from app.meetings import MeetingService
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult


class StartMeetingRecordingTool(Tool):
    name = "start_meeting_recording"

    def __init__(self, service: MeetingService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        title = str(request.arguments.get("title", "")).strip() or "Meeting"
        record = self.service.start_recording(title)
        return ToolResult(
            status="attempted",
            display_text=f"Started recording '{record.title}'.",
            spoken_text=f"I started recording {record.title}.",
            side_effects=["meeting_recording_started"],
            data={"meeting": record.model_dump(mode="json")},
        )


class StopMeetingRecordingTool(Tool):
    name = "stop_meeting_recording"

    def __init__(self, service: MeetingService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        record = self.service.stop_recording()
        return ToolResult(
            status="observed",
            display_text=f"Stopped recording '{record.title}'.",
            spoken_text=f"I stopped recording {record.title}.",
            side_effects=["meeting_recording_stopped"],
            data={"meeting": record.model_dump(mode="json")},
        )
