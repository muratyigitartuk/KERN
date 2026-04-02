from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from typing import Any

from app.network_safety import validate_public_https_url
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult

# Configurable whitelist of allowed application names.
# Override via KERN_ALLOWED_APPS env var (comma-separated).
_DEFAULT_ALLOWED_APPS = {
    "notepad", "calc", "calculator", "explorer", "mspaint", "paint",
    "wordpad", "taskmgr", "code", "excel",
    "winword", "word", "outlook", "onenote", "teams", "firefox",
    "chrome", "msedge", "edge", "brave", "thunderbird",
}

_allowed_apps_raw = os.environ.get("KERN_ALLOWED_APPS", "")
ALLOWED_APPS: set[str] = (
    {a.strip().lower() for a in _allowed_apps_raw.split(",") if a.strip()}
    if _allowed_apps_raw.strip()
    else _DEFAULT_ALLOWED_APPS
)


class OpenAppTool(Tool):
    name = "open_app"

    def availability(self) -> tuple[bool, str | None]:
        if os.name != "nt":
            return False, "Desktop app launching is only supported on Windows."
        return True, None

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Name of the application to launch"},
            },
            "required": ["app"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        app_name = str(request.arguments.get("app", "")).strip()
        if not app_name:
            return ToolResult(
                success=False,
                status="failed",
                display_text="No application specified.",
                spoken_text="I need an app name.",
            )
        # Validate against allowed applications whitelist
        normalized = app_name.lower().replace(".exe", "")
        if normalized not in ALLOWED_APPS:
            return ToolResult(
                success=False,
                status="failed",
                display_text=f"'{app_name}' is not in the allowed applications list.",
                spoken_text=f"{app_name} is not allowed. Ask your administrator to add it.",
                data={"app": app_name, "attempted": False, "reason": "not_whitelisted"},
            )
        executable = shutil.which(normalized) or shutil.which(f"{normalized}.exe")
        if not executable:
            return ToolResult(
                success=False,
                status="failed",
                display_text=f"Failed to resolve a safe launch path for {app_name}.",
                spoken_text=f"I could not resolve {app_name}.",
                data={"app": app_name, "attempted": False},
            )
        try:
            subprocess.Popen([executable], shell=False)
        except Exception:
            return ToolResult(
                success=False,
                status="failed",
                display_text=f"Failed to send a launch request for {app_name}.",
                spoken_text=f"I could not send the launch request for {app_name}.",
                data={"app": app_name, "attempted": False},
            )
        return ToolResult(
            status="attempted",
            display_text=f"Sent a launch request for {app_name}.",
            spoken_text=f"I sent the launch request for {app_name}.",
            evidence=[f"Start request issued for {app_name}."],
            side_effects=["launch_request"],
            data={"app": app_name, "attempted": True},
        )


class OpenWebsiteTool(Tool):
    name = "open_website"

    def availability(self) -> tuple[bool, str | None]:
        if os.name != "nt":
            return True, "Browser dispatch is supported, but desktop verification is limited outside Windows."
        return True, None

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open in the default browser"},
            },
            "required": ["url"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        url = str(request.arguments.get("url", "")).strip()
        if not url:
            return ToolResult(
                success=False,
                status="failed",
                display_text="No website specified.",
                spoken_text="I need a website.",
            )
        try:
            url = validate_public_https_url(url)
            opened = webbrowser.open(url)
        except Exception:
            return ToolResult(
                success=False,
                status="failed",
                display_text=f"Failed to send an open request for {url}.",
                spoken_text="I could not send the browser request.",
                data={"url": url, "attempted": False},
            )
        return ToolResult(
            status="attempted" if opened else "failed",
            display_text=f"Sent a browser open request for {url}." if opened else f"Attempted to open {url}.",
            spoken_text="I sent the browser request." if opened else "I could not confirm the browser request.",
            evidence=["Request sent to default browser."] if opened else [],
            side_effects=["browser_open_request"] if opened else [],
            data={"url": url, "attempted": bool(opened)},
        )
