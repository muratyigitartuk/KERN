from __future__ import annotations

from datetime import datetime, timezone

from app.backup import BackupService
from app.email_service import EmailService
from app.platform import PlatformStore
from app.tools.base import Tool
from app.types import BackupTarget, CurrentContextSnapshot, ProfileSummary, RuntimeSnapshot, ToolRequest, ToolResult


class CreateBackupTool(Tool):
    name = "create_backup"

    def __init__(self, backup_service: BackupService, platform: PlatformStore, profile: ProfileSummary) -> None:
        self.backup_service = backup_service
        self.platform = platform
        self.profile = profile

    async def run(self, request: ToolRequest) -> ToolResult:
        self.platform.assert_profile_unlocked(self.profile.slug, "backup", "profile_backup")
        password = str(request.arguments.get("password", "") or "").strip()
        if not password:
            return ToolResult(
                status="failed",
                display_text="Backup password is required.",
                spoken_text="I need a backup password before I can create the encrypted backup.",
                suggested_follow_up="Repeat the request with a password, for example: create an encrypted backup with password ...",
            )
        label = str(request.arguments.get("label", "") or "Manual backup").strip() or "Manual backup"
        targets = self.platform.list_backup_targets(self.profile.slug)
        target = targets[0] if targets else BackupTarget(kind="local_folder", path=self.profile.backups_root, label=label, writable=True)
        job = self.platform.create_job(
            "profile_backup",
            "Create encrypted backup",
            profile_slug=self.profile.slug,
            detail="Preparing encrypted backup.",
            payload={"target_path": target.path, "target_kind": target.kind, "label": label},
        )
        self.platform.update_checkpoint(job.id, "planned", {"target_path": target.path, "target_kind": target.kind, "label": label})
        try:
            backup_path = self.backup_service.create_encrypted_profile_backup(
                self.profile,
                target,
                password,
                platform_store=self.platform,
            )
            self.platform.update_checkpoint(job.id, "written", {"path": str(backup_path)})
            self.platform.update_job(
                job.id,
                status="completed",
                progress=1.0,
                detail=f"Backup written to {backup_path.name}.",
                checkpoint_stage="written",
                recoverable=False,
                result={"path": str(backup_path), "label": label},
            )
            self.platform.record_audit(
                "backup",
                "profile_backup",
                "success",
                f"Encrypted profile backup created at {backup_path.name}.",
                profile_slug=self.profile.slug,
                details={"label": label},
            )
            return ToolResult(
                status="observed",
                display_text=f"Created encrypted backup '{backup_path.name}'.",
                spoken_text="I created the encrypted backup.",
                side_effects=["backup_created"],
                data={"path": str(backup_path), "label": label},
            )
        except Exception as exc:
            self.platform.update_job(
                job.id,
                status="failed",
                detail=str(exc),
                checkpoint_stage="failed",
                recoverable=False,
                error_code="backup_failed",
                error_message=str(exc),
                result={"label": label},
            )
            self.platform.record_audit(
                "backup",
                "profile_backup",
                "failure",
                f"Encrypted backup failed: {exc}",
                profile_slug=self.profile.slug,
                details={"label": label},
            )
            raise


class ListBackupsTool(Tool):
    name = "list_backups"

    def __init__(self, backup_service: BackupService, platform: PlatformStore, profile: ProfileSummary) -> None:
        self.backup_service = backup_service
        self.platform = platform
        self.profile = profile

    async def run(self, request: ToolRequest) -> ToolResult:
        self.platform.assert_profile_unlocked(self.profile.slug, "backup", "list_backups")
        targets = self.platform.list_backup_targets(self.profile.slug) or [
            BackupTarget(kind="local_folder", path=self.profile.backups_root, label="Local backups", writable=True)
        ]
        backups: list[dict[str, object]] = []
        for target in targets:
            for backup_path in self.backup_service.list_backups(self.profile, target):
                info = self.backup_service.inspect_backup(backup_path)
                info["target_label"] = target.label
                backups.append(info)
        backups.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        self.platform.record_audit(
            "backup",
            "list_backups",
            "success",
            "Listed available backups.",
            profile_slug=self.profile.slug,
            details={"count": len(backups)},
        )
        if not backups:
            return ToolResult(
                status="observed",
                display_text="No backups are available yet.",
                spoken_text="There are no backups available yet.",
                data={"backups": []},
            )
        newest = backups[0]
        summary = "; ".join(
            f"{str(item.get('path', '')).split(chr(92))[-1]} ({item.get('created_at', 'unknown')})" for item in backups[:3]
        )
        return ToolResult(
            status="observed",
            display_text=f"Available backups: {summary}. Newest is {str(newest.get('path', '')).split(chr(92))[-1]}.",
            spoken_text="I listed the available backups.",
            evidence=[f"Found {len(backups)} backup(s)."],
            data={"backups": backups, "newest": newest},
        )


class RestoreBackupTool(Tool):
    name = "restore_backup"

    def __init__(self, backup_service: BackupService, platform: PlatformStore, profile: ProfileSummary) -> None:
        self.backup_service = backup_service
        self.platform = platform
        self.profile = profile

    async def run(self, request: ToolRequest) -> ToolResult:
        self.platform.assert_profile_unlocked(self.profile.slug, "backup", "restore_backup")
        backup_path = str(request.arguments.get("backup_path", "") or "").strip()
        password = str(request.arguments.get("password", "") or "").strip()
        restore_root = str(request.arguments.get("restore_root", "") or "").strip()
        if not backup_path or not password or not restore_root:
            return ToolResult(
                status="failed",
                display_text="backup_path, password, and restore_root are required.",
                spoken_text="I need the backup path, password, and restore destination.",
            )
        validation = self.backup_service.validate_backup(backup_path, password)
        if not validation.valid:
            return ToolResult(
                status="failed",
                display_text="; ".join(validation.errors) or "Backup validation failed.",
                spoken_text="The encrypted backup could not be validated.",
                data={"validation": validation.model_dump(mode="json")},
            )
        restored = self.backup_service.restore_encrypted_profile_backup(
            backup_path,
            password,
            restore_root,
        )
        self.platform.record_audit(
            "backup",
            "restore_backup",
            "success",
            f"Backup restored to {restored}.",
            profile_slug=self.profile.slug,
        )
        return ToolResult(
            status="observed",
            display_text=f"Backup restored to {restored}.",
            spoken_text="I restored the encrypted backup.",
            side_effects=["backup_restored"],
            data={"path": str(restored), "validation": validation.model_dump(mode="json")},
        )


class ReadAuditEventsTool(Tool):
    name = "read_audit_events"

    def __init__(self, platform: PlatformStore, profile: ProfileSummary) -> None:
        self.platform = platform
        self.profile = profile

    def parameter_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 8, "description": "Number of events to return"},
                "query": {"type": "string", "description": "Free-text filter across category, action, message"},
                "category": {"type": "string", "description": "Filter by audit category (e.g. security, backup, runtime)"},
                "date_from": {"type": "string", "description": "ISO date lower bound (e.g. 2024-01-01)"},
                "date_to": {"type": "string", "description": "ISO date upper bound (e.g. 2024-12-31)"},
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        self.platform.assert_profile_unlocked(self.profile.slug, "audit", "read_audit_events")
        limit = int(request.arguments.get("limit", 8) or 8)
        query = str(request.arguments.get("query", "") or "").strip().lower()
        category = str(request.arguments.get("category", "") or "").strip() or None
        date_from = str(request.arguments.get("date_from", "") or "").strip() or None
        date_to = str(request.arguments.get("date_to", "") or "").strip() or None

        events = self.platform.list_audit_events(
            self.profile.slug,
            limit=max(limit * 3, 50),
            category=category,
            date_from=date_from,
            date_to=date_to,
        )
        if query:
            terms = [term.strip() for term in query.replace(" or ", ",").split(",") if term.strip()]
            if terms:
                events = [
                    event
                    for event in events
                    if any(
                        term in " ".join(
                            [
                                event.category,
                                event.action,
                                event.status,
                                event.message,
                                " ".join(f"{key} {value}" for key, value in event.details.items()),
                            ]
                        ).lower()
                        for term in terms
                    )
                ]
        events = events[:limit]
        self.platform.record_audit(
            "audit",
            "read_audit_events",
            "success",
            "Read audit events.",
            profile_slug=self.profile.slug,
            details={"count": len(events), "query": query, "category": category},
        )
        if not events:
            return ToolResult(
                status="observed",
                display_text="No audit events matched that filter.",
                spoken_text="No audit events matched that filter.",
                data={"events": []},
            )
        summary = "; ".join(f"[{event.category}] {event.action}: {event.message}" for event in events[:3])
        return ToolResult(
            status="observed",
            display_text=f"Recent audit events: {summary}",
            spoken_text="I loaded the recent audit events.",
            evidence=[f"Loaded {len(events)} audit event(s)."],
            data={"events": [event.model_dump(mode="json") for event in events]},
        )


class ExportAuditTrailTool(Tool):
    name = "export_audit_trail"

    def __init__(self, platform: PlatformStore, profile: ProfileSummary) -> None:
        self.platform = platform
        self.profile = profile

    async def run(self, request: ToolRequest) -> ToolResult:
        self.platform.assert_profile_unlocked(self.profile.slug, "audit", "export_audit_trail")
        payload = self.platform.export_audit_trail(self.profile.slug)
        self.platform.record_audit(
            "audit",
            "export_audit_trail",
            "success",
            "Audit trail exported.",
            profile_slug=self.profile.slug,
        )
        return ToolResult(
            status="observed",
            display_text="Exported the audit trail for the active profile.",
            spoken_text="I exported the audit trail.",
            side_effects=["audit_exported"],
            data={"audit_export": payload},
        )


class ReadRuntimeSnapshotTool(Tool):
    name = "read_runtime_snapshot"

    def __init__(self, snapshot_getter, platform: PlatformStore, profile: ProfileSummary) -> None:
        self.snapshot_getter = snapshot_getter
        self.platform = platform
        self.profile = profile

    async def run(self, request: ToolRequest) -> ToolResult:
        self.platform.assert_profile_unlocked(self.profile.slug, "runtime", "read_runtime_snapshot")
        snapshot: RuntimeSnapshot = self.snapshot_getter()
        job_counts = ", ".join(
            f"{key}:{value}" for key, value in (snapshot.background_job_counts or {}).items() if value
        )
        domain_summary = ", ".join(
            f"{name}:{status.reason}" for name, status in (snapshot.domain_statuses or {}).items()
        )
        text = (
            f"Active profile is {(snapshot.active_profile.title if snapshot.active_profile else self.profile.title)}. "
            f"Memory scope is {snapshot.memory_scope or 'profile'}. "
            f"Assistant state is {snapshot.assistant_state}. "
            f"Job counts: {job_counts or 'none'}. "
            f"Domains: {domain_summary or 'not refreshed yet'}."
        )
        self.platform.record_audit(
            "runtime",
            "read_runtime_snapshot",
            "success",
            "Read runtime snapshot.",
            profile_slug=self.profile.slug,
        )
        return ToolResult(
            status="observed",
            display_text=text,
            spoken_text="I loaded the runtime snapshot.",
            data={"snapshot": snapshot.model_dump(mode="json")},
        )


class ReadProfileSecurityTool(Tool):
    name = "read_profile_security"

    def __init__(self, platform: PlatformStore, profile: ProfileSummary, memory_scope_getter) -> None:
        self.platform = platform
        self.profile = profile
        self.memory_scope_getter = memory_scope_getter

    async def run(self, request: ToolRequest) -> ToolResult:
        self.platform.assert_profile_unlocked(self.profile.slug, "security", "read_profile_security")
        security = self.platform.get_profile_security_state(self.profile.slug)
        memory_scope = self.memory_scope_getter()
        text = (
            f"Active profile is {self.profile.title}. "
            f"Memory scope is {memory_scope}. "
            f"Database encryption is {security.get('db_encryption_mode', 'off')}. "
            f"Artifact encryption is {security.get('artifact_encryption_migration_state', 'not enabled')}."
        )
        self.platform.record_audit(
            "security",
            "read_profile_security",
            "success",
            "Read active profile and security state.",
            profile_slug=self.profile.slug,
        )
        return ToolResult(
            status="observed",
            display_text=text,
            spoken_text="I loaded the current profile and security state.",
            data={"security": security, "profile": self.profile.model_dump(mode="json"), "memory_scope": memory_scope},
        )


class ReadCurrentContextTool(Tool):
    name = "read_current_context"

    def __init__(self, snapshot_getter, platform: PlatformStore, profile: ProfileSummary) -> None:
        self.snapshot_getter = snapshot_getter
        self.platform = platform
        self.profile = profile

    async def run(self, request: ToolRequest) -> ToolResult:
        self.platform.assert_profile_unlocked(self.profile.slug, "runtime", "read_current_context")
        snapshot: RuntimeSnapshot = self.snapshot_getter()
        context: CurrentContextSnapshot | None = snapshot.current_context or (
            snapshot.active_context_summary.current_context if snapshot.active_context_summary else None
        )
        if context is None:
            return ToolResult(
                status="observed",
                display_text="Current context is not available right now.",
                spoken_text="Current context is not available right now.",
                data={"current_context": None},
            )
        lines: list[str] = []
        if context.window and context.window.title:
            label = context.window.process_name or f"PID {context.window.process_id or 'unknown'}"
            lines.append(f"Foreground window: {label} - {context.window.title}")
        if context.clipboard and context.clipboard.has_text:
            lines.append(f"Clipboard: {context.clipboard.excerpt}")
        if context.media and context.media.title:
            artist = context.media.artist or "unknown artist"
            lines.append(f"Media: {context.media.title} by {artist}")
        if not lines:
            lines.append("No active window, clipboard text, or media context is available.")
        self.platform.record_audit(
            "runtime",
            "read_current_context",
            "success",
            "Read current local context.",
            profile_slug=self.profile.slug,
            details={"sources": context.sources},
        )
        return ToolResult(
            status="observed",
            display_text=" | ".join(lines),
            spoken_text="I loaded the current local context.",
            data={"current_context": context.model_dump(mode="json")},
        )


class SyncMailboxTool(Tool):
    name = "sync_mailbox"

    def __init__(self, service: EmailService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        messages = self.service.sync_mailbox(limit=int(request.arguments.get("limit", 8) or 8))
        urgent_only = bool(request.arguments.get("urgent_only", False))
        today_only = bool(request.arguments.get("today_only", False))
        filtered = _filter_mailbox_messages(messages, urgent_only=urgent_only, today_only=today_only)
        summary = "; ".join(f"{message.sender}: {message.subject}" for message in filtered[:3]) or "No urgent messages found."
        return ToolResult(
            status="observed",
            display_text=f"Mailbox synchronized. {summary}",
            spoken_text="I synchronized the mailbox.",
            side_effects=["mailbox_synced"],
            data={
                "messages": [message.model_dump(mode="json") for message in filtered],
                "synced_count": len(messages),
            },
        )


class ReadMailboxSummaryTool(Tool):
    name = "read_mailbox_summary"

    def __init__(self, service: EmailService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        messages = self.service.read_recent_email(limit=int(request.arguments.get("limit", 5) or 5))
        urgent_only = bool(request.arguments.get("urgent_only", False))
        today_only = bool(request.arguments.get("today_only", False))
        filtered = _filter_mailbox_messages(messages, urgent_only=urgent_only, today_only=today_only)
        if not filtered:
            return ToolResult(
                status="observed",
                display_text="No mailbox messages matched that filter.",
                spoken_text="No mailbox messages matched that filter.",
                data={"messages": []},
            )
        summary = "; ".join(f"{message.sender}: {message.subject}" for message in filtered[:3])
        return ToolResult(
            status="observed",
            display_text=f"Recent mailbox messages: {summary}",
            spoken_text="I loaded the recent mailbox messages.",
            data={"messages": [message.model_dump(mode="json") for message in filtered]},
        )


def _filter_mailbox_messages(messages, *, urgent_only: bool, today_only: bool):
    now = datetime.now(timezone.utc).date()
    filtered = []
    for message in messages:
        haystack = f"{message.subject} {message.body_preview}".lower()
        if urgent_only and not any(token in haystack for token in ("urgent", "asap", "important", "deadline", "today")):
            continue
        if today_only and message.received_at.date() != now:
            continue
        filtered.append(message)
    return filtered
