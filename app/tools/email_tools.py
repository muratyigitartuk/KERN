from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.email_service import EmailService
from app.tools.base import Tool
from app.types import CalendarActionPlan, EmailDraft, ToolRequest, ToolResult


class ReadEmailTool(Tool):
    name = "read_email"

    def __init__(self, service: EmailService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        messages = self.service.read_recent_email(limit=int(request.arguments.get("limit", 5)))
        if not messages:
            return ToolResult(status="observed", display_text="No email messages available.", spoken_text="No email messages are available.", data={"messages": []})
        summary = "; ".join(f"{msg.sender}: {msg.subject}" for msg in messages[:3])
        return ToolResult(
            status="observed",
            display_text=f"Recent email: {summary}",
            spoken_text="I loaded your recent email.",
            evidence=[f"Loaded {len(messages)} email message(s)."],
            data={"messages": [msg.model_dump(mode="json") for msg in messages]},
        )


class ComposeEmailTool(Tool):
    name = "compose_email"

    def __init__(self, service: EmailService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        draft = EmailDraft(
            id=str(request.arguments.get("draft_id", "")).strip() or None,
            to=list(request.arguments.get("to", [])),
            cc=list(request.arguments.get("cc", [])),
            subject=str(request.arguments.get("subject", "")),
            body=str(request.arguments.get("body", "")),
            attachments=list(request.arguments.get("attachments", [])),
        )
        send_now = bool(request.arguments.get("send_now", False))
        if send_now:
            stored = self.service.save_draft(draft.model_copy(update={"status": "ready"}))
            subject = self.service.send_draft(stored.id or "")
            status = "observed"
            display = f"Sent email '{subject}'."
            spoken = f"I sent the email {subject}."
            side_effects = ["email_sent"]
            data = {"draft": stored.model_dump(mode="json")}
        else:
            stored = self.service.save_draft(draft)
            status = "observed"
            display = f"Saved draft '{stored.subject or '(no subject)'}'."
            spoken = "I saved the email draft."
            side_effects = ["email_draft_saved"]
            data = {"draft": stored.model_dump(mode="json")}
        return ToolResult(
            status=status,
            display_text=display,
            spoken_text=spoken,
            side_effects=side_effects,
            data=data,
        )


class EmailReminderTool(Tool):
    name = "create_email_reminder"

    def __init__(self, service: EmailService, reminder_service) -> None:
        self.service = service
        self.reminder_service = reminder_service

    async def run(self, request: ToolRequest) -> ToolResult:
        message_id = request.arguments.get("message_id")
        accepted = request.arguments.get("accepted")
        if accepted is None:
            suggestions = self.service.suggest_reminders_from_email(limit=int(request.arguments.get("limit", 5)))
            if message_id:
                suggestions = [item for item in suggestions if item["message_id"] == message_id]
            return ToolResult(
                status="observed",
                display_text="Reminder suggestions ready for review.",
                spoken_text="I prepared reminder suggestions from your email.",
                evidence=[f"Prepared {len(suggestions)} suggestion(s)."],
                data={"suggestions": suggestions},
            )
        result = self.service.apply_reminder_suggestion(self.reminder_service, str(message_id or ""), bool(accepted))
        if bool(accepted):
            display = f"Accepted reminder suggestion '{result.get('title', '')}'."
            spoken = "I accepted the reminder suggestion."
            side_effects = ["reminder_created"]
        else:
            display = "Rejected email reminder suggestion."
            spoken = "I rejected the email reminder suggestion."
            side_effects = ["reminder_rejected"]
        return ToolResult(
            status="observed",
            display_text=display,
            spoken_text=spoken,
            side_effects=side_effects,
            data={"result": result},
        )


class ScheduleMeetingInviteTool(Tool):
    name = "schedule_meeting_and_invite"

    def __init__(self, service: EmailService) -> None:
        self.service = service

    async def run(self, request: ToolRequest) -> ToolResult:
        title = str(request.arguments.get("title", "")).strip() or "Meeting"
        starts_at_raw = str(request.arguments.get("starts_at", "")).strip()
        starts_at = datetime.fromisoformat(starts_at_raw) if starts_at_raw else datetime.now() + timedelta(days=1)
        duration_minutes = int(request.arguments.get("duration_minutes", 30))
        recipients = list(request.arguments.get("invite_recipients", []))
        draft = EmailDraft(
            to=recipients,
            subject=f"Einladung: {title}",
            body=f"Hallo,\n\nich habe ein Treffen für {starts_at.isoformat()} geplant.\n\nViele Grüße",
        )
        plan = CalendarActionPlan(
            title=title,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=duration_minutes),
            invite_recipients=recipients,
            draft=draft,
        )
        result = self.service.schedule_meeting_and_invite(plan)
        invite_status = str(result.get("invite_status", "draft_only"))
        side_effects = ["calendar_event_created"]
        if result.get("invite_sent"):
            side_effects.append("email_sent")
        elif result.get("draft_id"):
            side_effects.append("email_draft_saved")
        return ToolResult(
            status="observed",
            display_text=f"Scheduled '{title}' with invite status {invite_status}.",
            spoken_text=f"I scheduled {title} with invite status {invite_status}.",
            side_effects=side_effects,
            data={"plan": plan.model_dump(mode="json"), "result": result},
        )


class SendNtfyNotificationTool(Tool):
    name = "send_ntfy_notification"

    def __init__(self, service: EmailService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        if not (self.service.ntfy_base_url and self.service.ntfy_topic):
            return False, "Configure ntfy to enable mobile notifications."
        return True, None

    async def run(self, request: ToolRequest) -> ToolResult:
        title = str(request.arguments.get("title", "")).strip() or "KERN"
        message = str(request.arguments.get("message", "")).strip()
        if not message:
            return ToolResult(status="failed", display_text="Notification message is required.", spoken_text="I need a notification message.")
        self.service.send_ntfy_notification(title, message)
        return ToolResult(status="observed", display_text="Sent mobile notification.", spoken_text="I sent the mobile notification.")
