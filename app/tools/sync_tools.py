from __future__ import annotations

from app.path_safety import validate_user_import_path
from app.syncing import SyncService
from app.tools.base import Tool
from app.types import ToolRequest, ToolResult


class SyncToTargetTool(Tool):
    name = "sync_profile_data"

    def __init__(self, service: SyncService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        kind = str(request.arguments.get("kind", "nas") or "nas").strip()
        path_or_url = str(request.arguments.get("path_or_url", "")).strip()
        if not path_or_url:
            return ToolResult(status="failed", display_text="I need a sync path or URL.")
        label = str(request.arguments.get("label", "Sync target") or "Sync target")
        if kind != "nextcloud":
            try:
                path_or_url = str(
                    validate_user_import_path(
                        path_or_url,
                        self.service.profile,
                        roots=[self.service.profile.backups_root],
                        allow_create=True,
                    )
                )
            except ValueError as exc:
                return ToolResult(status="failed", display_text=f"Sync path denied: {exc}")
        target = self.service.upsert_target(
            kind if kind in {"nextcloud", "nas"} else "nas",
            label,
            path_or_url,
            username=str(request.arguments.get("username", "")).strip() or None,
            password=str(request.arguments.get("password", "")).strip() or None,
        )
        data_classes = list(request.arguments.get("data_classes", [])) or ["documents", "archives", "attachments", "meetings"]
        if kind == "nextcloud":
            source_file = str(request.arguments.get("source_file", "")).strip()
            if not source_file:
                return ToolResult(
                    status="failed",
                    display_text="Remote WebDAV is upload-only right now. Provide source_file for an explicit upload.",
                )
            try:
                source_file = str(validate_user_import_path(source_file, self.service.profile))
            except ValueError as exc:
                return ToolResult(status="failed", display_text=f"Source path denied: {exc}")
            destination = self.service.upload_webdav(source_file, path_or_url, target_id=target.id)
            return ToolResult(
                status="observed",
                display_text="Uploaded file to Nextcloud/WebDAV target.",
                side_effects=["sync_completed"],
                data={"destination": destination},
            )
        destination = self.service.sync_to_target(target, data_classes=data_classes)
        return ToolResult(
            status="observed",
            display_text=destination,
            side_effects=["sync_completed"],
            data={"destination": destination, "data_classes": data_classes},
        )
