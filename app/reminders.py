from __future__ import annotations

from datetime import datetime, timedelta

from app.local_data import LocalDataService
from app.types import ReminderSummary

_NOTIFICATION_KEY_LIMIT = 2048


class ReminderService:
    def __init__(self, data: LocalDataService) -> None:
        self.data = data

    def parse_due_phrase(self, text: str) -> tuple[str, datetime] | None:
        lowered = text.lower().strip()
        if " in " in lowered and "minute" in lowered:
            prefix, suffix = text.split(" in ", 1)
            digits = "".join(character for character in suffix if character.isdigit())
            if digits:
                minutes = max(1, int(digits))
                title = prefix.replace("remind me", "").replace("set a timer for", "").strip(" ,.")
                title = title or "follow up"
                return title, datetime.now() + timedelta(minutes=minutes)
        if "tomorrow" in lowered:
            title = text.lower().replace("remind me", "").replace("tomorrow", "").strip(" ,.")
            return (title or "tomorrow follow up"), datetime.now() + timedelta(days=1)
        return None

    def create_reminder(self, title: str, due_at: datetime, kind: str = "reminder") -> int:
        return self.data.create_reminder(title, due_at, kind=kind)

    def due_reminders(self, now: datetime | None = None) -> list[ReminderSummary]:
        return self.data.list_due_reminders(now=now)

    def pending_reminders(self, limit: int = 10) -> list[ReminderSummary]:
        return self.data.list_pending_reminders(limit=limit)

    def snooze(self, reminder_id: int, minutes: int = 10) -> None:
        self.data.snooze_reminder(reminder_id, minutes=minutes)

    def dismiss(self, reminder_id: int) -> None:
        self.data.dismiss_reminder(reminder_id)

    def mark_announced(self, reminder_id: int) -> None:
        self.data.mark_reminder_announced(reminder_id)


class NotificationService:
    def __init__(self) -> None:
        self._sent_event_keys: set[str] = set()

    def due_reminder_messages(self, reminders: list[ReminderSummary], preferred_title: str) -> list[str]:
        messages: list[str] = []
        for reminder in reminders:
            messages.append(f"Reminder for you, {preferred_title}: {reminder.title}.")
        return messages

    def event_soft_alert_message(self, event_title: str, preferred_title: str, starts_at: datetime) -> str | None:
        key = f"{event_title}:{starts_at.isoformat()}"
        if key in self._sent_event_keys:
            return None
        self._sent_event_keys.add(key)
        if len(self._sent_event_keys) > _NOTIFICATION_KEY_LIMIT:
            self._sent_event_keys = set(list(self._sent_event_keys)[-_NOTIFICATION_KEY_LIMIT:])
        return f"Your next event is {event_title} at {starts_at.strftime('%H:%M')}, {preferred_title}."
