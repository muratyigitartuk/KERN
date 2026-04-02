from __future__ import annotations

import webbrowser
from datetime import datetime, timedelta, timezone
from typing import Any

from app.local_data import LocalDataService
from app.network_safety import build_browser_search_url
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


class BrowserSearchTool(Tool):
    name = "browser_search"

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to open in the browser"},
            },
            "required": ["query"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        query = str(request.arguments.get("query", "")).strip()
        if not query:
            return ToolResult(
                success=False,
                status="failed",
                display_text="I need a search query.",
                spoken_text="Tell me what to search for.",
            )
        opened = webbrowser.open(build_browser_search_url(query))
        return ToolResult(
            success=bool(opened),
            status="attempted" if opened else "failed",
            display_text=f"Opened a browser search for {query}." if opened else f"Failed to open a browser search for {query}.",
            spoken_text=f"I opened a browser search for {query}." if opened else "I could not open the browser search.",
            evidence=["Sent request to default browser."] if opened else [],
            side_effects=["browser_open_request"] if opened else [],
            data={"query": query, "attempted": bool(opened)},
        )


class FocusModeTool(Tool):
    name = "focus_mode"

    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "Duration of the focus block in minutes"},
                "title": {"type": "string", "description": "Label for the focus block"},
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        minutes = int(request.arguments.get("minutes", 50))
        title = str(request.arguments.get("title", "Focus block")).strip() or "Focus block"
        due_at = datetime.now() + timedelta(minutes=max(5, minutes))
        reminder_id = self.data.create_reminder(f"End {title.lower()}", due_at, kind="timer")
        self.data.set_assistant_mode("focus")
        self.data.set_focus_until(due_at)
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Focus mode started for {minutes} minutes.",
            spoken_text=f"Focus mode is active for {minutes} minutes.",
            evidence=[f"Timer #{reminder_id} set for {due_at.strftime('%H:%M')}."],
            side_effects=["focus_mode_started", "timer_created"],
            data={"minutes": minutes, "reminder_id": reminder_id, "focus_until": due_at.isoformat()},
        )


class SystemStatusTool(Tool):
    name = "system_status"

    def availability(self) -> tuple[bool, str | None]:
        if psutil is None:
            return True, "Limited runtime-only status. Install psutil for CPU, memory, and battery metrics."
        return True, None

    def parameter_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, request: ToolRequest) -> ToolResult:
        status: dict[str, object] = {"source": "base"}
        evidence: list[str] = []
        if psutil is not None:
            status["cpu_percent"] = psutil.cpu_percent(interval=0.0)
            status["memory_percent"] = round(psutil.virtual_memory().percent, 1)
            battery = psutil.sensors_battery()
            if battery is not None:
                status["battery_percent"] = battery.percent
                status["power_plugged"] = battery.power_plugged
            evidence.append("Collected metrics via psutil.")
        else:
            evidence.append("psutil not installed; limited status only.")

        spoken_bits = []
        if "cpu_percent" in status:
            spoken_bits.append(f"CPU is at {status['cpu_percent']} percent")
        if "memory_percent" in status:
            spoken_bits.append(f"memory is at {status['memory_percent']} percent")
        if "battery_percent" in status:
            spoken_bits.append(f"battery is at {status['battery_percent']} percent")
        spoken = ", ".join(spoken_bits) if spoken_bits else "Only the base runtime status is available right now."
        return ToolResult(
            success=True,
            status="observed",
            display_text=spoken,
            spoken_text=spoken,
            evidence=evidence,
            data=status,
        )
