from __future__ import annotations

import contextlib
import email
import hashlib
import logging
import email.utils
import imaplib
import ipaddress
import json
import re
import time
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage as OutgoingEmail
from pathlib import Path
from sqlite3 import Connection
from urllib.parse import quote, urlparse
from uuid import uuid4

logger = logging.getLogger(__name__)

from app.artifacts import ArtifactStore
from app.documents import DocumentService
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.path_safety import ensure_path_within_roots, sanitize_filename
from app.platform import PlatformStore
from app.tools.calendar import CalendarService
from app.types import CalendarActionPlan, EmailAccount, EmailDraft, EmailMessage, EmailReminderSuggestion, NotificationChannel, ProfileSummary, WorkflowDomainEvent


class EmailService:
    def __init__(
        self,
        connection: Connection,
        platform: PlatformStore | None,
        profile: ProfileSummary,
        data: LocalDataService,
        calendar: CalendarService,
        documents: DocumentService,
        *,
        imap_host: str | None = None,
        smtp_host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        email_address: str | None = None,
        ntfy_base_url: str | None = None,
        ntfy_topic: str | None = None,
    ) -> None:
        self.connection = connection
        self.memory = MemoryRepository(connection, profile_slug=profile.slug)
        self.platform = platform
        self.profile = profile
        self.data = data
        self.calendar = calendar
        self.documents = documents
        self.imap_host = imap_host
        self.smtp_host = smtp_host
        self.username = username
        self.password = password
        self.email_address = email_address or username
        self.ntfy_base_url = ntfy_base_url.rstrip("/") if ntfy_base_url else None
        self.ntfy_topic = ntfy_topic
        self.artifacts = ArtifactStore(platform, profile)
        self._ensure_schema()
        self._ensure_bootstrap_account()

    def _record_correspondence_domain_event(
        self,
        *,
        event_type: str,
        detail: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        payload = {
            "workflow_type": "correspondence_follow_up",
            "event_type": event_type,
            "detail": detail,
            "metadata": metadata or {},
        }
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        workflow_id = hashlib.sha1(f"workflow|{self.profile.slug}|correspondence".encode("utf-8")).hexdigest()
        organization_id = self.profile.organization_id if isinstance(self.profile.organization_id, str) else None
        self.memory.record_workflow_domain_event(
            WorkflowDomainEvent(
                id=f"wde-{workflow_id}-{fingerprint[:16]}",
                profile_slug=self.profile.slug,
                organization_id=organization_id,
                workspace_slug=self.profile.slug,
                workflow_id=workflow_id,
                workflow_type="correspondence_follow_up",
                event_type=event_type,
                detail=detail,
                fingerprint=fingerprint,
                metadata=metadata or {},
            )
        )

    def availability(self) -> tuple[bool, str | None]:
        if self.platform and self.platform.is_profile_locked(self.profile.slug):
            return False, "Unlock the active profile to access email."
        accounts = self.memory.list_email_accounts()
        if not accounts:
            return False, "Configure a profile email account to enable email features."
        statuses = {account.sync_status for account in accounts}
        degraded = "degraded" in statuses
        configured = "configured" in statuses
        message = f"{len(accounts)} account(s)"
        if configured:
            message += " / sync configured"
        elif degraded:
            message += " / sync degraded"
        else:
            message += " / drafts only"
        return True, message

    def _ensure_schema(self) -> None:
        with self.connection:
            self.connection.execute("DROP INDEX IF EXISTS idx_mailbox_messages_profile_message_id_unique")
            self.connection.execute(
                """
                DELETE FROM mailbox_messages
                WHERE message_id IS NOT NULL
                  AND rowid NOT IN (
                    SELECT MIN(rowid)
                    FROM mailbox_messages
                    WHERE message_id IS NOT NULL
                    GROUP BY profile_slug, COALESCE(account_id, ''), message_id
                  )
                """
            )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_mailbox_messages_profile_message_id_unique
                ON mailbox_messages(profile_slug, account_id, message_id)
                WHERE message_id IS NOT NULL
                """
            )

    def create_or_update_account(
        self,
        *,
        label: str,
        email_address: str,
        imap_host: str,
        smtp_host: str,
        username: str | None = None,
        password: str | None = None,
        password_ref: str | None = None,
        account_id: str | None = None,
    ) -> EmailAccount:
        self._ensure_unlocked("account_saved")
        if password and self.platform:
            secret = self.platform.store_secret(self.profile.slug, f"email:{label}:password", password)
            password_ref = secret.id
        account = EmailAccount(
            id=account_id or str(uuid4()),
            profile_slug=self.profile.slug,
            label=label,
            email_address=email_address,
            imap_host=imap_host,
            smtp_host=smtp_host,
            sync_status="configured" if imap_host and username and password_ref else "degraded" if imap_host else "idle",
        )
        self.memory.upsert_email_account(account, username=username, password_ref=password_ref)
        if self.platform:
            self.platform.record_audit(
                "email",
                "account_saved",
                "success",
                f"Saved email account {label}.",
                profile_slug=self.profile.slug,
                details={"account_id": account.id},
            )
        return account

    def list_accounts(self, *, audit: bool = True) -> list[EmailAccount]:
        self._ensure_unlocked("list_email_accounts")
        accounts = self.memory.list_email_accounts()
        enriched: list[EmailAccount] = []
        draft_count = self.memory.count_email_drafts()
        for account in accounts:
            details = self.memory.get_email_account_details(account.id) or {}
            password = self._resolve_password(details.get("password_ref"))
            state = self._get_account_state(account.id)
            sync_status = "configured" if account.imap_host and details.get("username") and password else "degraded" if account.imap_host else "idle"
            enriched.append(account.model_copy(update={"sync_status": sync_status}))
            health = "ready" if sync_status == "configured" else "degraded" if sync_status == "degraded" else "drafts_only"
            enriched[-1] = enriched[-1].model_copy(
                update={
                    "last_sync_at": datetime.fromisoformat(state["last_sync_at"]) if state.get("last_sync_at") else None,
                    "last_failure": state.get("last_failure"),
                    "draft_count": draft_count,
                    "health": health,
                }
            )
        if audit and self.platform:
            self.platform.record_audit(
                "email",
                "list_email_accounts",
                "success",
                "Listed email accounts.",
                profile_slug=self.profile.slug,
                details={"count": len(enriched)},
            )
        return enriched

    def save_draft(self, draft: EmailDraft) -> EmailDraft:
        self._ensure_unlocked("draft_saved")
        normalized = draft.model_copy(
            update={
                "attachments": self._normalize_draft_attachments(draft.attachments),
                "status": draft.status or "draft",
            }
        )
        stored = self.memory.save_email_draft(normalized)
        if self.platform:
            self.platform.record_audit(
                "email",
                "draft_saved",
                "success",
                f"Saved draft '{stored.subject or '(no subject)'}'.",
                profile_slug=self.profile.slug,
                details={"draft_id": stored.id},
            )
        self._record_correspondence_domain_event(
            event_type="draft_saved",
            detail=f"Saved draft '{stored.subject or '(no subject)'}'.",
            metadata={"draft_id": stored.id, "subject": stored.subject, "to": stored.to},
        )
        return stored

    def list_drafts(self, limit: int = 12, status: str | None = None, *, audit: bool = True) -> list[EmailDraft]:
        self._ensure_unlocked("list_drafts")
        drafts = self.memory.list_email_drafts(limit=limit, status=status)
        if audit and self.platform:
            self.platform.record_audit(
                "email",
                "list_drafts",
                "success",
                "Listed email drafts.",
                profile_slug=self.profile.slug,
                details={"count": len(drafts), "status": status},
            )
        return drafts

    def send_draft(self, draft_id: str, account_id: str | None = None) -> str:
        self._ensure_unlocked("send_email")
        draft = self.memory.get_email_draft(draft_id)
        if not draft:
            raise RuntimeError("Draft not found.")
        return self.send_email(draft, draft_id=draft_id, account_id=account_id)

    def sync_mailbox(self, limit: int = 8, folder: str = "INBOX", account_id: str | None = None) -> list[EmailMessage]:
        self._ensure_unlocked("sync_mailbox")
        account = self._resolve_account(account_id)
        if not account:
            raise RuntimeError("No email account is configured for mailbox sync.")
        account_details = self.memory.get_email_account_details(account.id) or {}
        username = account_details.get("username")
        password = self._resolve_password(account_details.get("password_ref"))
        if not (account.imap_host and username and password):
            raise RuntimeError("IMAP is not configured for this profile account.")

        job = self.platform.create_job(
            "mailbox_sync",
            f"Sync mailbox {account.label}",
            profile_slug=self.profile.slug,
            detail=f"Connecting to {folder}.",
            payload={"account_id": account.id, "folder": folder, "limit": limit},
        ) if self.platform else None
        if self.platform and job:
            self.platform.update_job(job.id, status="running", progress=0.1, checkpoint_stage="connect")
        try:
            messages = self._retry_with_backoff(
                "mailbox sync",
                lambda attempt: self._sync_mailbox_once(account=account, username=username, password=password, folder=folder, limit=limit, job_id=job.id if job else None, attempt=attempt),
                (imaplib.IMAP4.error, OSError, TimeoutError),
            )
            self._set_account_state(account.id, last_sync_at=datetime.now(timezone.utc).isoformat(), last_failure=None)
            return messages or self.memory.list_mailbox_messages(limit=limit, account_id=account.id)
        except Exception as exc:
            self._set_account_state(account.id, last_sync_at=datetime.now(timezone.utc).isoformat(), last_failure=str(exc))
            if self.platform and job:
                self.platform.update_job(job.id, status="failed", progress=0.0, detail=str(exc), checkpoint_stage="failed", error_code="mailbox_sync_failed", error_message=str(exc), result={"account_id": account.id})
                self.platform.record_audit("email", "sync_mailbox", "failure", str(exc), profile_slug=self.profile.slug, details={"account_id": account.id})
            raise RuntimeError(f"Mailbox sync failed: {exc}") from exc

    def read_recent_email(self, limit: int = 5, *, audit: bool = True) -> list[EmailMessage]:
        self._ensure_unlocked("read_email")
        mailbox_messages = self.memory.list_mailbox_messages(limit=limit)
        if mailbox_messages:
            if audit and self.platform:
                self.platform.record_audit(
                    "email",
                    "read_email",
                    "success",
                    "Read cached email messages.",
                    profile_slug=self.profile.slug,
                    details={"count": len(mailbox_messages), "source": "cache"},
                )
            return mailbox_messages
        messages = self.sync_mailbox(limit=limit)
        if audit and self.platform:
            self.platform.record_audit(
                "email",
                "read_email",
                "success",
                "Read synchronized email messages.",
                profile_slug=self.profile.slug,
                details={"count": len(messages), "source": "live_sync"},
            )
        return messages

    def list_indexed_messages(self, limit: int = 10, *, audit: bool = True) -> list[EmailMessage]:
        self._ensure_unlocked("list_mailbox_messages")
        mailbox_messages = self.memory.list_mailbox_messages(limit=limit)
        detailed: list[EmailMessage] = []
        for message in mailbox_messages:
            details = self.memory.get_mailbox_message_details(message.id) or {}
            detailed.append(
                message.model_copy(
                    update={
                        "account_id": details.get("account_id"),
                        "message_id": details.get("message_id"),
                        "folder": details.get("folder", "INBOX"),
                        "body_preview": str(details.get("body_text", ""))[:500],
                        "attachment_paths": details.get("attachment_paths", []),
                    }
                )
            )
        if audit and self.platform:
            self.platform.record_audit(
                "email",
                "list_mailbox_messages",
                "success",
                "Listed indexed mailbox messages.",
                profile_slug=self.profile.slug,
                details={"count": len(detailed)},
            )
        return detailed

    def send_email(self, draft: EmailDraft, draft_id: str | None = None, account_id: str | None = None) -> str:
        self._ensure_unlocked("send_email")
        account = self._resolve_account(account_id, require_smtp=True)
        if not account:
            raise RuntimeError("No SMTP-enabled profile account is configured.")
        details = self.memory.get_email_account_details(account.id) or {}
        username = details.get("username")
        password = self._resolve_password(details.get("password_ref"))
        sender_address = account.email_address
        if not (account.smtp_host and username and password and sender_address):
            if draft_id:
                self.memory.mark_email_draft_status(draft_id, "failed")
            if self.platform:
                self.platform.record_audit("email", "send_email", "failure", "SMTP is not configured.", profile_slug=self.profile.slug, details={"draft_id": draft_id})
            raise RuntimeError("SMTP is not configured for this profile account.")

        outgoing = OutgoingEmail()
        outgoing["From"] = sender_address
        outgoing["To"] = ", ".join(draft.to)
        if draft.cc:
            outgoing["Cc"] = ", ".join(draft.cc)
        outgoing["Subject"] = self._sanitize_header_value(draft.subject)
        outgoing.set_content(draft.body)
        for attachment in draft.attachments:
            attachment_path = ensure_path_within_roots(
                attachment,
                roots=[self.profile.attachments_root, self.profile.documents_root, self.profile.archives_root],
                reject_symlink=True,
            )
            outgoing.add_attachment(
                self.artifacts.read_bytes(attachment_path),
                maintype="application",
                subtype="octet-stream",
                filename=attachment_path.name,
            )
        try:
            self._retry_with_backoff(
                "smtp send",
                lambda _attempt: self._send_email_once(account.smtp_host, username, password, outgoing),
                (smtplib.SMTPException, OSError, TimeoutError),
            )
            if draft_id:
                self.memory.mark_email_draft_status(draft_id, "sent", sent_at=datetime.now(timezone.utc))
            if self.platform:
                self.platform.record_audit("email", "send_email", "success", f"Sent email '{draft.subject}'.", profile_slug=self.profile.slug, details={"draft_id": draft_id, "account_id": account.id})
            self._record_correspondence_domain_event(
                event_type="draft_sent" if draft_id else "email_sent",
                detail=f"Sent email '{draft.subject}'.",
                metadata={"draft_id": draft_id, "account_id": account.id, "subject": draft.subject, "to": draft.to},
            )
            return draft.subject
        except Exception as exc:
            if draft_id:
                self.memory.mark_email_draft_status(draft_id, "failed")
            if self.platform:
                self.platform.record_audit("email", "send_email", "failure", str(exc), profile_slug=self.profile.slug, details={"draft_id": draft_id, "account_id": account.id})
            self._record_correspondence_domain_event(
                event_type="draft_failed" if draft_id else "email_failed",
                detail=str(exc),
                metadata={"draft_id": draft_id, "account_id": account.id if account else None},
            )
            raise RuntimeError(f"Email send failed: {exc}") from exc

    def create_reminder_from_email(self, reminder_service, message_id: str | None = None) -> tuple[str, datetime]:
        self._ensure_unlocked("email_to_reminder")
        message = self._resolve_message(message_id)
        due_at = self._extract_due_date(message) or (datetime.now() + timedelta(days=1))
        title = f"Email follow-up: {message.subject}"
        if hasattr(reminder_service, "create_reminder"):
            reminder_service.create_reminder(title, due_at)
        else:
            reminder_service(title, due_at)
        if self.platform:
            self.platform.record_audit("email", "email_to_reminder", "success", f"Created reminder from email '{message.subject}'.", profile_slug=self.profile.slug, details={"message_id": message.id})
        return title, due_at

    def suggest_reminders_from_email(self, limit: int = 5) -> list[dict[str, object]]:
        self._ensure_unlocked("email_reminder_suggestions")
        candidates: list[dict[str, object]] = []
        for message in self.list_indexed_messages(limit=limit * 2):
            details = self.memory.get_mailbox_message_details(message.id) or {}
            due_at = self._extract_due_date(message)
            if due_at is None:
                continue
            score, rationale = self._score_reminder_candidate(message, details, due_at)
            if score <= 0:
                continue
            state = self.memory.get_value("email_suggestion_state", message.id, "suggested") or "suggested"
            candidates.append(
                {
                    "message_id": message.id,
                    "title": f"Email follow-up: {message.subject}",
                    "due_at": due_at.isoformat(),
                    "status": state,
                    "rationale": rationale,
                    "score": score,
                }
            )
        ranked = sorted(candidates, key=lambda item: (-float(item["score"]), str(item["due_at"]), str(item["title"])))
        return ranked[:limit]

    def list_reminder_suggestions(self, limit: int = 5, *, audit: bool = True) -> list[EmailReminderSuggestion]:
        suggestions = [
            EmailReminderSuggestion(
                message_id=str(item["message_id"]),
                title=str(item["title"]),
                due_at=datetime.fromisoformat(str(item["due_at"])),
                status=str(item.get("status", "suggested")),
                rationale=str(item.get("rationale", "")),
            )
            for item in self.suggest_reminders_from_email(limit=limit)
        ]
        if audit and self.platform:
            self.platform.record_audit(
                "email",
                "list_email_reminder_suggestions",
                "success",
                "Listed email reminder suggestions.",
                profile_slug=self.profile.slug,
                details={"count": len(suggestions)},
            )
        return suggestions

    def apply_reminder_suggestion(self, reminder_service, message_id: str, accepted: bool) -> dict[str, object]:
        self._ensure_unlocked("apply_email_reminder_suggestion")
        state = "accepted" if accepted else "rejected"
        self.memory.set_value("email_suggestion_state", message_id, state)
        if not accepted:
            if self.platform:
                self.platform.record_audit("email", "email_reminder_suggestion_rejected", "info", f"Rejected email reminder suggestion for {message_id}.", profile_slug=self.profile.slug)
            return {"message_id": message_id, "status": state}
        title, due_at = self.create_reminder_from_email(reminder_service, message_id)
        return {"message_id": message_id, "status": state, "title": title, "due_at": due_at.isoformat()}

    def schedule_meeting_and_invite(self, plan: CalendarActionPlan, send_invite: bool = True) -> dict[str, object]:
        self._ensure_unlocked("schedule_meeting")
        job = self.platform.create_job(
            "schedule_meeting_invite",
            f"Schedule {plan.title}",
            profile_slug=self.profile.slug,
            detail="Scheduling calendar event.",
            payload={"title": plan.title, "send_invite": send_invite},
        ) if self.platform else None
        if self.platform and job:
            self.platform.update_job(job.id, status="running", progress=0.1, checkpoint_stage="planned")
        scheduled_plan = self.calendar.schedule_meeting(plan)
        event_id = scheduled_plan.event_id
        draft_id = None
        invite_sent = False
        try:
            if self.platform and job:
                self.platform.update_checkpoint(
                    job.id,
                    "event_scheduled",
                    {"event_id": event_id},
                )
                self.platform.update_job(
                    job.id,
                    status="running",
                    progress=0.4,
                    checkpoint_stage="event_scheduled",
                    detail="Preparing invite draft.",
                )
            if plan.draft and plan.invite_recipients:
                draft = plan.draft.model_copy(update={"to": plan.invite_recipients, "status": "ready"})
                stored = self.save_draft(draft)
                draft_id = stored.id
                if self.platform and job and draft_id:
                    self.platform.update_checkpoint(
                        job.id,
                        "draft_saved",
                        {"event_id": event_id, "draft_id": draft_id},
                    )
                    self.platform.update_job(
                        job.id,
                        status="running",
                        progress=0.7,
                        checkpoint_stage="draft_saved",
                        detail="Dispatching invite.",
                    )
                if send_invite and draft_id:
                    self.send_draft(draft_id)
                    invite_sent = True
                    if self.platform and job:
                        self.platform.update_checkpoint(
                            job.id,
                            "invite_sent",
                            {"event_id": event_id, "draft_id": draft_id},
                        )
            if self.platform:
                self.platform.record_audit("calendar", "schedule_meeting", "success", f"Scheduled meeting {plan.title}.", profile_slug=self.profile.slug, details={"event_id": event_id, "draft_id": draft_id})
                if job:
                    self.platform.update_job(
                        job.id,
                        status="completed",
                        recoverable=False,
                        progress=1.0,
                        checkpoint_stage="completed",
                        detail=f"Scheduled meeting {plan.title}.",
                        result={"event_id": event_id, "draft_id": draft_id, "invite_sent": invite_sent},
                    )
            return {
                "event_id": event_id,
                "draft_id": draft_id,
                "invite_sent": invite_sent,
                "invite_status": "sent" if invite_sent else "draft_only",
            }
        except Exception as exc:
            if event_id and hasattr(self.calendar, "delete_event"):
                with contextlib.suppress(Exception):  # cleanup — best-effort
                    self.calendar.delete_event(event_id)
            if draft_id:
                with contextlib.suppress(Exception):  # cleanup — best-effort
                    self.memory.delete_email_draft(draft_id)
            if self.platform:
                self.platform.record_audit(
                    "calendar",
                    "schedule_meeting",
                    "failure",
                    str(exc),
                    profile_slug=self.profile.slug,
                    details={"event_id": event_id, "draft_id": draft_id, "rolled_back": bool(event_id)},
                )
                if job:
                    self.platform.update_checkpoint(
                        job.id,
                        "rolled_back",
                        {"event_id": event_id, "draft_id": draft_id},
                    )
                    self.platform.update_job(
                        job.id,
                        status="failed",
                        recoverable=False,
                        progress=0.0,
                        checkpoint_stage="rolled_back",
                        detail=str(exc),
                        error_code="schedule_meeting_failed",
                        error_message=str(exc),
                        result={"event_id": event_id, "draft_id": draft_id},
                    )
            raise

    def send_ntfy_notification(self, title: str, message: str) -> None:
        self._ensure_unlocked("send_ntfy")
        if not (self.ntfy_base_url and self.ntfy_topic):
            raise RuntimeError("ntfy is not configured.")
        parsed = urlparse(self.ntfy_base_url)
        if parsed.scheme != "https":
            raise RuntimeError("ntfy endpoint must use HTTPS.")
        host = parsed.hostname or ""
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip and (ip.is_private or ip.is_loopback or ip.is_link_local):
            raise RuntimeError("ntfy endpoint must not use private or loopback addresses.")
        request = urllib.request.Request(
            f"{self.ntfy_base_url}/{quote(self.ntfy_topic, safe='')}",
            data=message.encode("utf-8"),
            method="POST",
            headers={"Title": self._sanitize_header_value(title)},
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except urllib.error.URLError as exc:
            if self.platform:
                self.platform.record_audit("notifications", "send_ntfy", "failure", str(exc), profile_slug=self.profile.slug)
            raise RuntimeError(f"ntfy send failed: {exc}") from exc
        if self.platform:
            self.platform.record_audit("notifications", "send_ntfy", "success", f"Sent ntfy notification {title}.", profile_slug=self.profile.slug)

    def list_notification_channels(self) -> list[NotificationChannel]:
        self._ensure_unlocked("list_notification_channels")
        if not (self.ntfy_base_url and self.ntfy_topic):
            return []
        return [NotificationChannel(label="Self-hosted ntfy", endpoint=f"{self.ntfy_base_url}/{self.ntfy_topic}", enabled=True)]

    def _ensure_bootstrap_account(self) -> None:
        if not (self.email_address and (self.imap_host or self.smtp_host)):
            return
        if self.memory.list_email_accounts():
            return
        self.create_or_update_account(
            label=self.email_address,
            email_address=self.email_address,
            imap_host=self.imap_host or "",
            smtp_host=self.smtp_host or "",
            username=self.username,
            password=self.password,
            password_ref="env:KERN_EMAIL_PASSWORD" if self.password and not self.platform else None,
            account_id=self._default_account_id(),
        )

    def _get_account_state(self, account_id: str) -> dict[str, str | None]:
        raw = self.memory.get_value("email_account_state", account_id, "")
        if not raw:
            return {"last_sync_at": None, "last_failure": None}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"last_sync_at": None, "last_failure": None}

    def _set_account_state(self, account_id: str, *, last_sync_at: str | None, last_failure: str | None) -> None:
        self.memory.set_value(
            "email_account_state",
            account_id,
            json.dumps({"last_sync_at": last_sync_at, "last_failure": last_failure}),
        )

    def _default_account_id(self) -> str:
        accounts = self.memory.list_email_accounts()
        if accounts:
            return accounts[0].id
        return self.email_address or self.username or "default-email-account"

    def _resolve_account(self, account_id: str | None, require_smtp: bool = False) -> EmailAccount | None:
        accounts = self.list_accounts()
        if account_id:
            for account in accounts:
                if account.id == account_id:
                    return account
        if require_smtp:
            return next((account for account in accounts if account.smtp_host), accounts[0] if accounts else None)
        return accounts[0] if accounts else None

    def _resolve_message(self, message_id: str | None) -> EmailMessage:
        if message_id:
            details = self.memory.get_mailbox_message_details(message_id)
            if details:
                return EmailMessage(
                    id=str(details["id"]),
                    account_id=details.get("account_id"),
                    message_id=details.get("message_id"),
                    subject=str(details["subject"]),
                    sender=str(details["sender"]),
                    recipients=list(details.get("recipients", [])),
                    received_at=datetime.fromisoformat(str(details["received_at"])),
                    has_attachments=bool(details.get("has_attachments")),
                    folder=str(details.get("folder", "INBOX")),
                    body_preview=str(details.get("body_text", ""))[:500],
                    attachment_paths=list(details.get("attachment_paths", [])),
                )
        indexed = self.list_indexed_messages(limit=1)
        if not indexed:
            raise RuntimeError("No email messages are indexed yet.")
        return indexed[0]

    def _extract_due_date(self, message: EmailMessage) -> datetime | None:
        details = self.memory.get_mailbox_message_details(message.id)
        text = f"{message.subject} {details.get('body_text', '') if details else message.body_preview}".lower()
        if "morgen" in text or "tomorrow" in text:
            return datetime.now() + timedelta(days=1)
        if "nächste woche" in text or "next week" in text:
            return datetime.now() + timedelta(days=7)
        match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if match:
            return datetime.fromisoformat(match.group(1))
        german = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", text)
        if german:
            day, month, year = german.groups()
            year = f"20{year}" if len(year) == 2 else year
            return datetime(int(year), int(month), int(day))
        weekday_map = {
            "monday": 0,
            "montag": 0,
            "tuesday": 1,
            "dienstag": 1,
            "wednesday": 2,
            "mittwoch": 2,
            "thursday": 3,
            "donnerstag": 3,
            "friday": 4,
            "freitag": 4,
        }
        for token, weekday in weekday_map.items():
            if token in text:
                now = datetime.now()
                delta = (weekday - now.weekday()) % 7 or 7
                return now + timedelta(days=delta)
        return None

    def _score_reminder_candidate(self, message: EmailMessage, details: dict[str, object], due_at: datetime) -> tuple[float, str]:
        text = f"{message.subject} {details.get('body_text', '')}".lower()
        score = 0.0
        reasons: list[str] = []
        if any(token in text for token in ["deadline", "due", "fällig", "until", "bis ", "by "]):
            score += 1.1
            reasons.append("deadline language detected")
        if any(token in text for token in ["follow up", "follow-up", "remind", "reply", "respond", "review", "send", "submit"]):
            score += 0.7
            reasons.append("follow-up action language detected")
        if message.has_attachments:
            score += 0.2
            reasons.append("message includes attachments")
        if due_at.date() <= (datetime.now() + timedelta(days=14)).date():
            score += 0.4
            reasons.append("due date is soon")
        if not reasons:
            score += 0.3
            reasons.append("explicit date detected")
        return score, ", ".join(reasons).capitalize() + "."

    def _store_message(self, parsed, raw_bytes: bytes, *, account_id: str, folder: str = "INBOX") -> EmailMessage:
        external_message_id = parsed.get("Message-ID")
        existing_id = self.memory.find_mailbox_message_by_external_id(external_message_id, account_id=account_id) if external_message_id else None
        if existing_id:
            return self._resolve_message(existing_id)

        message_id = str(uuid4())
        received_at = self._parse_message_datetime(parsed) or datetime.now(timezone.utc)
        raw_path = self.artifacts.write_bytes(Path(self.profile.archives_root) / f"email-{message_id}.eml", raw_bytes)
        attachment_paths: list[str] = []
        indexed_attachment_paths: list[str] = []
        try:
            preview = self._extract_plaintext(parsed)
            for attachment_name, payload in self._extract_attachments(parsed):
                safe_attachment_name = sanitize_filename(attachment_name)
                attachment_path = self.artifacts.write_bytes(
                    Path(self.profile.attachments_root) / f"{message_id}-{safe_attachment_name}",
                    payload,
                )
                attachment_paths.append(str(attachment_path))
                try:
                    self.documents.ingest_document(str(attachment_path), source="email_attachment")
                    indexed_attachment_paths.append(str(attachment_path))
                except Exception as exc:
                    if self.platform:
                        self.platform.record_audit(
                            "email",
                            "attachment_ingest",
                            "failure",
                            str(exc),
                            profile_slug=self.profile.slug,
                            details={"message_id": message_id, "attachment": attachment_name},
                        )
            recipients = email.utils.getaddresses(parsed.get_all("to", []) + parsed.get_all("cc", []))
            sender = parsed.get("from", "unknown")
            subject = parsed.get("subject", "(no subject)")
            message = EmailMessage(
                id=message_id,
                account_id=account_id,
                message_id=external_message_id,
                subject=subject,
                sender=sender,
                recipients=[addr for _, addr in recipients if addr],
                received_at=received_at,
                has_attachments=bool(attachment_paths),
                folder=folder,
                body_preview=preview[:500],
                attachment_paths=attachment_paths,
            )
            self.memory.append_mailbox_message(
                message,
                account_id=account_id,
                folder=folder,
                body_text=preview,
                attachment_paths=attachment_paths,
                metadata={"raw_path": str(raw_path)},
                message_id=external_message_id,
            )
            return message
        except Exception as exc:
            logger.error("Failed to store message: %s", exc, exc_info=True)
            with contextlib.suppress(Exception):  # cleanup — best-effort
                Path(raw_path).unlink(missing_ok=True)
            for attachment_path in attachment_paths:
                with contextlib.suppress(Exception):  # cleanup — best-effort
                    Path(attachment_path).unlink(missing_ok=True)
            for attachment_path in indexed_attachment_paths:
                with contextlib.suppress(Exception):  # cleanup — best-effort
                    self._delete_indexed_attachment(attachment_path)
            raise

    def _parse_message_datetime(self, parsed) -> datetime | None:
        raw = parsed.get("Date")
        if not raw:
            return None
        parsed_dt = email.utils.parsedate_to_datetime(raw)
        if parsed_dt is None:
            return None
        if parsed_dt.tzinfo is None:
            return parsed_dt
        return parsed_dt.astimezone(timezone.utc).replace(tzinfo=None)

    def _extract_plaintext(self, parsed) -> str:
        if parsed.is_multipart():
            for part in parsed.walk():
                if part.get_content_type() == "text/plain" and "attachment" not in (part.get("Content-Disposition") or ""):
                    try:
                        return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                    except Exception as exc:
                        logger.debug("Failed to decode email part: %s", exc)
                        continue
            return ""
        payload = parsed.get_payload(decode=True)
        if not payload:
            return ""
        return payload.decode(parsed.get_content_charset() or "utf-8", errors="ignore")

    def _extract_attachments(self, parsed) -> list[tuple[str, bytes]]:
        attachments: list[tuple[str, bytes]] = []
        if not parsed.is_multipart():
            return attachments
        for part in parsed.walk():
            disposition = part.get("Content-Disposition") or ""
            filename = part.get_filename()
            if "attachment" not in disposition.lower() or not filename:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            attachments.append((sanitize_filename(filename), payload))
        return attachments

    def _resolve_password(self, password_ref: str | None) -> str | None:
        if self.platform and password_ref:
            return self.platform.resolve_secret(password_ref, profile_slug=self.profile.slug)
        return None

    def _sanitize_header_value(self, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise RuntimeError("Header values must not contain control characters.")
        return value

    def _normalize_draft_attachments(self, attachments: list[str]) -> list[str]:
        normalized: list[str] = []
        for attachment in attachments:
            approved = ensure_path_within_roots(
                attachment,
                roots=[self.profile.attachments_root, self.profile.documents_root, self.profile.archives_root],
                reject_symlink=True,
            )
            normalized.append(str(approved))
        return normalized

    def _delete_indexed_attachment(self, attachment_path: str) -> None:
        rows = self.connection.execute(
            "SELECT id FROM document_records WHERE profile_slug = ? AND file_path = ?",
            (self.profile.slug, attachment_path),
        ).fetchall()
        for row in rows:
            self.connection.execute("DELETE FROM document_chunks WHERE document_id = ?", (row["id"],))
            self.connection.execute(
                "DELETE FROM document_records WHERE id = ? AND profile_slug = ?",
                (row["id"], self.profile.slug),
            )
        self.connection.commit()

    def _sync_mailbox_once(
        self,
        *,
        account: EmailAccount,
        username: str,
        password: str,
        folder: str,
        limit: int,
        job_id: str | None,
        attempt: int,
    ) -> list[EmailMessage]:
        mailbox = imaplib.IMAP4_SSL(account.imap_host)
        try:
            if self.platform and job_id:
                self.platform.update_job(job_id, detail=f"Connecting to {folder}. Attempt {attempt}.", progress=0.15, checkpoint_stage="connect")
            mailbox.login(username, password)
            if self.platform and job_id:
                self.platform.update_checkpoint(job_id, "authenticated", {"account_id": account.id, "attempt": attempt})
            status, _ = mailbox.select(folder)
            if status != "OK":
                raise RuntimeError(f"Unable to open mailbox folder {folder}.")
            status, ids = mailbox.search(None, "ALL")
            if status != "OK":
                raise RuntimeError("Mailbox search failed.")
            message_ids = ids[0].split()[-limit:]
            messages: list[EmailMessage] = []
            for offset, message_id in enumerate(reversed(message_ids), start=1):
                fetch_status, payload = mailbox.fetch(message_id, "(RFC822)")
                if fetch_status != "OK" or not payload or not payload[0]:
                    continue
                raw_bytes = payload[0][1]
                parsed = email.message_from_bytes(raw_bytes)
                stored = self._store_message(parsed, raw_bytes, account_id=account.id, folder=folder)
                messages.append(stored)
                if self.platform and job_id:
                    self.platform.update_job(job_id, status="running", progress=min(0.9, 0.15 + (offset / max(1, len(message_ids))) * 0.7), detail=f"Indexed {offset}/{len(message_ids)} message(s).")
            if self.platform and job_id:
                self.platform.update_checkpoint(job_id, "messages_indexed", {"count": len(messages), "attempt": attempt})
                self.platform.update_job(job_id, status="completed", progress=1.0, detail=f"Synchronized {len(messages)} message(s).", result={"account_id": account.id, "count": len(messages)})
                self.platform.record_audit("email", "sync_mailbox", "success", f"Synchronized {len(messages)} messages from {folder}.", profile_slug=self.profile.slug, details={"account_id": account.id})
            return messages
        finally:
            with contextlib.suppress(Exception):  # cleanup — best-effort
                mailbox.close()
            with contextlib.suppress(Exception):  # cleanup — best-effort
                mailbox.logout()

    def _send_email_once(self, smtp_host: str, username: str, password: str, outgoing: OutgoingEmail) -> None:
        with smtplib.SMTP(smtp_host, 587, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(outgoing)

    def _retry_with_backoff(self, label: str, operation, transient_errors: tuple[type[BaseException], ...]) -> object:
        delay = 0.25
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                return operation(attempt)
            except transient_errors as exc:
                last_exc = exc
                if attempt >= 3:
                    break
                time.sleep(delay)
                delay *= 2
        if last_exc is not None:
            raise RuntimeError(f"{label} failed after 3 attempts: {last_exc}") from last_exc
        raise RuntimeError(f"{label} failed.")

    def recover_jobs(self) -> None:
        if not self.platform:
            return
        for job in self.platform.list_jobs(self.profile.slug, limit=20):
            if not job.recoverable:
                continue
            if job.job_type == "mailbox_sync":
                account_id = str(job.payload.get("account_id", "") or "").strip() or None
                folder = str(job.payload.get("folder", "INBOX") or "INBOX")
                limit = int(job.payload.get("limit", 8) or 8)
                account = self._resolve_account(account_id)
                if not account:
                    self.platform.update_job(
                        job.id,
                        status="failed",
                        recoverable=False,
                        error_code="mailbox_recovery_failed",
                        error_message="Email account not found.",
                        detail="Email account not found for recovery.",
                    )
                    continue
                account_details = self.memory.get_email_account_details(account.id) or {}
                username = account_details.get("username")
                password = self._resolve_password(account_details.get("password_ref"))
                if not (account.imap_host and username and password):
                    self.platform.update_job(
                        job.id,
                        status="failed",
                        recoverable=False,
                        error_code="mailbox_recovery_failed",
                        error_message="Mailbox credentials unavailable.",
                        detail="Mailbox recovery requires configured credentials.",
                    )
                    continue
                try:
                    self.platform.update_job(job.id, status="running", recoverable=True, detail="Resuming mailbox sync.", checkpoint_stage=job.checkpoint_stage or "resume", progress=0.1)
                    messages = self._retry_with_backoff(
                        "mailbox sync recovery",
                        lambda attempt: self._sync_mailbox_once(
                            account=account,
                            username=username,
                            password=password,
                            folder=folder,
                            limit=limit,
                            job_id=job.id,
                            attempt=attempt,
                        ),
                        (imaplib.IMAP4.error, OSError, TimeoutError),
                    )
                    self._set_account_state(account.id, last_sync_at=datetime.now(timezone.utc).isoformat(), last_failure=None)
                    self.platform.update_job(
                        job.id,
                        status="completed",
                        recoverable=False,
                        detail=f"Recovered mailbox sync for {len(messages)} message(s).",
                        checkpoint_stage="recovered",
                        progress=1.0,
                        result={"account_id": account.id, "count": len(messages)},
                    )
                except Exception as exc:
                    self._set_account_state(account.id, last_sync_at=datetime.now(timezone.utc).isoformat(), last_failure=str(exc))
                    self.platform.update_job(
                        job.id,
                        status="failed",
                        recoverable=False,
                        detail=str(exc),
                        checkpoint_stage="failed",
                        error_code="mailbox_recovery_failed",
                        error_message=str(exc),
                        result={"account_id": account.id},
                    )
                continue
            if job.job_type != "schedule_meeting_invite":
                continue
            checkpoint_payload: dict[str, object] = {}
            for checkpoint in self.platform.list_checkpoints(job.id):
                payload_row = self.platform.connection.execute(
                    """
                    SELECT payload_json
                    FROM recovery_checkpoints
                    WHERE job_id = ? AND stage = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (job.id, checkpoint.stage),
                ).fetchone()
                if payload_row:
                    try:
                        checkpoint_payload.update(json.loads(payload_row["payload_json"] or "{}"))
                    except Exception as exc:
                        logger.warning("Failed to parse checkpoint payload JSON: %s", exc)
            event_id = checkpoint_payload.get("event_id")
            draft_id = str(checkpoint_payload.get("draft_id", "") or "").strip() or None
            if str(job.checkpoint_stage or "") == "invite_sent":
                self.platform.update_job(
                    job.id,
                    status="completed",
                    recoverable=False,
                    checkpoint_stage="recovered",
                    progress=1.0,
                    detail="Meeting invite flow had already dispatched the invite before interruption.",
                    result={"event_id": event_id, "draft_id": draft_id, "invite_sent": True},
                )
                continue
            if event_id and hasattr(self.calendar, "delete_event"):
                with contextlib.suppress(Exception):  # cleanup — best-effort
                    self.calendar.delete_event(int(event_id))
            if draft_id:
                with contextlib.suppress(Exception):  # cleanup — best-effort
                    self.memory.delete_email_draft(draft_id)
            self.platform.update_job(
                job.id,
                status="rolled_back",
                recoverable=False,
                checkpoint_stage="rolled_back",
                progress=1.0,
                detail="Rolled back interrupted meeting scheduling flow.",
                result={"event_id": event_id, "draft_id": draft_id},
            )
            self.platform.record_audit(
                "calendar",
                "schedule_meeting_recovery",
                "warning",
                "Rolled back interrupted meeting scheduling flow.",
                profile_slug=self.profile.slug,
                details={"job_id": job.id, "event_id": event_id, "draft_id": draft_id},
            )

    def _ensure_unlocked(self, action: str) -> None:
        if self.platform:
            self.platform.assert_profile_unlocked(self.profile.slug, "email", action)
