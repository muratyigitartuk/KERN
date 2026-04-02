from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

from app.memory import MemoryRepository
from app.types import CalendarEventSummary, ContextFact, MorningBrief, OpenLoop, ReminderSummary, TaskSummary


class LocalDataService:
    _DEFAULT_ONBOARDING_STATE = {
        "storage_confirmed": False,
        "model_choice": "",
        "starter_workflow": "",
        "selected_path": "",
        "sample_workspace_active": False,
        "sample_workspace_seeded": False,
        "completed": False,
    }

    def __init__(self, memory: MemoryRepository, default_title: str) -> None:
        self.memory = memory
        self.default_title = default_title

    def list_pending_tasks(self) -> list[TaskSummary]:
        return self.memory.list_local_tasks()

    def list_today_events(self) -> list[CalendarEventSummary]:
        return self.memory.list_local_events()

    def create_event(
        self,
        title: str,
        starts_at: datetime,
        ends_at: datetime | None = None,
        importance: int = 0,
    ) -> int:
        return self.memory.create_local_event(title, starts_at, ends_at=ends_at, importance=importance)

    def next_upcoming_event(self, now: datetime | None = None) -> CalendarEventSummary | None:
        now = now or datetime.now()
        for event in self.list_today_events():
            if event.starts_at >= now:
                return event
        return None

    def create_local_event(
        self,
        title: str,
        starts_at: datetime,
        ends_at: datetime | None = None,
        importance: int = 0,
    ) -> int:
        return self.memory.create_local_event(title, starts_at, ends_at=ends_at, importance=importance)

    def delete_event(self, event_id: int) -> bool:
        return self.memory.delete_local_event(event_id)

    def create_task(self, title: str, priority: int = 2) -> int:
        task_id = self.memory.create_local_task(title, priority=priority)
        self.memory.create_open_loop(
            title=title,
            details="Task pending",
            source="task",
            related_type="task",
            related_id=task_id,
        )
        return task_id

    def complete_task(self, title: str) -> bool:
        task_id = self.memory.complete_local_task_by_title(title)
        if task_id is None:
            return False
        self.memory.resolve_open_loop_by_relation("task", task_id)
        return True

    def create_note(self, content: str) -> int:
        return self.memory.create_note(content)

    def list_notes(self, limit: int = 10) -> list[str]:
        return self.memory.list_notes(limit=limit)

    def create_reminder(self, title: str, due_at: datetime, kind: str = "reminder") -> int:
        reminder_id = self.memory.create_reminder(title, due_at, kind=kind)
        self.memory.create_open_loop(
            title=title,
            details=f"{kind} pending",
            due_at=due_at,
            source="reminder",
            related_type="reminder",
            related_id=reminder_id,
        )
        return reminder_id

    def list_pending_reminders(self, limit: int = 10) -> list[ReminderSummary]:
        return self.memory.list_pending_reminders(limit=limit)

    def list_due_reminders(self, now: datetime | None = None) -> list[ReminderSummary]:
        return self.memory.list_due_reminders(now=now)

    def snooze_reminder(self, reminder_id: int, minutes: int = 10) -> None:
        next_due = datetime.now() + timedelta(minutes=minutes)
        self.memory.update_reminder_status(reminder_id, "snoozed", due_at=next_due)

    def dismiss_reminder(self, reminder_id: int) -> None:
        self.memory.update_reminder_status(reminder_id, "completed")
        self.memory.resolve_open_loop_by_relation("reminder", reminder_id, status="resolved")

    def mark_reminder_announced(self, reminder_id: int) -> None:
        self.memory.update_reminder_status(reminder_id, "announced")

    def cleanup_rollout_legacy_assistant_state(self) -> dict[str, int]:
        connection = self.memory.connection
        renamed_tasks = connection.execute(
            """
            UPDATE local_tasks
            SET title = 'Review KERN architecture'
            WHERE title = 'Review JARVIS architecture'
            """
        ).rowcount
        renamed_loops = connection.execute(
            """
            UPDATE open_loops
            SET title = 'Review KERN architecture', updated_at = ?
            WHERE title = 'Review JARVIS architecture'
            """,
            (datetime.now(timezone.utc).isoformat(),),
        ).rowcount
        reminder_rows = connection.execute(
            """
            SELECT id
            FROM local_reminders
            WHERE status IN ('pending', 'announced', 'snoozed')
              AND (
                lower(title) LIKE '%jarvis calibration%'
                OR lower(title) LIKE '%kern calibration profile%'
              )
            """
        ).fetchall()
        reminder_ids = [int(row["id"]) for row in reminder_rows]
        if reminder_ids:
            placeholders = ",".join("?" for _ in reminder_ids)
            connection.execute(
                f"UPDATE local_reminders SET status = 'completed' WHERE id IN ({placeholders})",
                reminder_ids,
            )
            connection.execute(
                f"""
                UPDATE open_loops
                SET status = 'dismissed', updated_at = ?
                WHERE status = 'open'
                  AND (
                    (related_type = 'reminder' AND related_id IN ({placeholders}))
                    OR lower(title) LIKE '%jarvis calibration%'
                    OR lower(title) LIKE '%kern calibration profile%'
                  )
                """,
                [datetime.now(timezone.utc).isoformat(), *reminder_ids],
            )
        else:
            connection.execute(
                """
                UPDATE open_loops
                SET status = 'dismissed', updated_at = ?
                WHERE status = 'open'
                  AND (
                    lower(title) LIKE '%jarvis calibration%'
                    OR lower(title) LIKE '%kern calibration profile%'
                  )
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )
        conversation_removed = connection.execute(
            """
            DELETE FROM conversation_log
            WHERE lower(content) LIKE '%open commitment:%jarvis calibration%'
               OR lower(content) LIKE '%open commitment:%kern calibration profile%'
               OR lower(content) LIKE '%check jarvis calibration%'
               OR lower(content) LIKE '%check kern calibration profile%'
            """
        ).rowcount
        connection.commit()
        return {
            "tasks_renamed": int(renamed_tasks or 0),
            "open_loops_renamed": int(renamed_loops or 0),
            "reminders_completed": len(reminder_ids),
            "conversation_entries_removed": int(conversation_removed or 0),
        }

    def set_preference(self, key: str, value: str) -> None:
        self.memory.set_value("preferences", key, value)

    def get_preference(self, key: str, default: str | None = None) -> str | None:
        return self.memory.get_value("preferences", key, default)

    def _get_json_preference(self, key: str, default: dict | None = None) -> dict:
        raw = self.get_preference(key, "")
        if not raw:
            return dict(default or {})
        try:
            value = json.loads(raw)
        except Exception as exc:
            logger.debug("Failed to parse JSON preference %r: %s", key, exc)
            return dict(default or {})
        return value if isinstance(value, dict) else dict(default or {})

    def _set_json_preference(self, key: str, value: dict) -> None:
        self.set_preference(key, json.dumps(value, sort_keys=True))

    def onboarding_state(self) -> dict[str, object]:
        state = self._get_json_preference("product_onboarding", self._DEFAULT_ONBOARDING_STATE)
        merged = dict(self._DEFAULT_ONBOARDING_STATE)
        merged.update({key: value for key, value in state.items() if key in merged})
        return merged

    def update_onboarding_state(self, **updates: object) -> dict[str, object]:
        state = self.onboarding_state()
        for key, value in updates.items():
            if key in self._DEFAULT_ONBOARDING_STATE and value is not None:
                state[key] = value
        if state.get("completed"):
            state["storage_confirmed"] = True
        self._set_json_preference("product_onboarding", state)
        return state

    def reset_onboarding_state(self) -> None:
        self._set_json_preference("product_onboarding", dict(self._DEFAULT_ONBOARDING_STATE))

    def support_bundle_state(self) -> dict[str, str]:
        state = self._get_json_preference("support_bundle_state", {})
        return {
            "last_export_at": str(state.get("last_export_at") or ""),
            "path": str(state.get("path") or ""),
        }

    def update_support_bundle_state(self, *, last_export_at: str, path: str) -> dict[str, str]:
        state = {"last_export_at": last_export_at, "path": path}
        self._set_json_preference("support_bundle_state", state)
        return state

    def sample_workspace_state(self) -> dict[str, object]:
        state = self._get_json_preference("sample_workspace_state", {})
        return {
            "active": bool(state.get("active", False)),
            "seeded": bool(state.get("seeded", False)),
            "document_ids": list(state.get("document_ids", []) or []),
            "started_at": str(state.get("started_at") or ""),
            "exited_at": str(state.get("exited_at") or ""),
        }

    def update_sample_workspace_state(self, **updates: object) -> dict[str, object]:
        state = self.sample_workspace_state()
        for key, value in updates.items():
            if key in state and value is not None:
                state[key] = value
        self._set_json_preference("sample_workspace_state", state)
        return state

    def reset_sample_workspace_state(self) -> None:
        self._set_json_preference(
            "sample_workspace_state",
            {"active": False, "seeded": False, "document_ids": [], "started_at": "", "exited_at": ""},
        )

    def update_history_state(self) -> dict[str, str]:
        state = self._get_json_preference("update_history_state", {})
        return {
            "last_attempt_at": str(state.get("last_attempt_at") or ""),
            "last_success_at": str(state.get("last_success_at") or ""),
            "last_backup_at": str(state.get("last_backup_at") or ""),
            "last_restore_attempt_at": str(state.get("last_restore_attempt_at") or ""),
            "last_status": str(state.get("last_status") or "idle"),
            "last_error": str(state.get("last_error") or ""),
        }

    def update_update_history_state(self, **updates: object) -> dict[str, str]:
        state = self.update_history_state()
        for key, value in updates.items():
            if key in state and value is not None:
                state[key] = str(value)
        self._set_json_preference("update_history_state", state)
        return state

    def get_proactive_feedback(self, alert_type: str, action_type: str | None = None) -> dict[str, int]:
        state = self._get_json_preference("proactive_feedback", {})
        bucket_key = f"{alert_type}:{action_type}" if action_type else alert_type
        payload = state.get(bucket_key, {})
        if not isinstance(payload, dict):
            return {"accepted": 0, "dismissed": 0, "executed_later": 0, "ignored": 0}
        return {
            "accepted": int(payload.get("accepted", 0) or 0),
            "dismissed": int(payload.get("dismissed", 0) or 0),
            "executed_later": int(payload.get("executed_later", 0) or 0),
            "ignored": int(payload.get("ignored", 0) or 0),
        }

    def record_proactive_feedback(self, alert_type: str, outcome: str, action_type: str | None = None) -> None:
        if not alert_type or outcome not in {"accepted", "dismissed", "executed_later", "ignored"}:
            return
        state = self._get_json_preference("proactive_feedback", {})
        bucket_key = f"{alert_type}:{action_type}" if action_type else alert_type
        payload = state.get(bucket_key, {})
        if not isinstance(payload, dict):
            payload = {}
        payload[outcome] = int(payload.get(outcome, 0) or 0) + 1
        state[bucket_key] = payload
        self._set_json_preference("proactive_feedback", state)

    def preferred_title(self) -> str:
        return self.get_preference("preferred_title", self.default_title) or self.default_title

    def memory_scope(self) -> str:
        return self.get_preference("memory_scope", "profile") or "profile"

    def set_memory_scope(self, scope: str) -> None:
        self.set_preference("memory_scope", scope)

    def muted(self) -> bool:
        return (self.get_preference("muted", "false") or "false").lower() == "true"

    def set_muted(self, muted: bool) -> None:
        self.set_preference("muted", "true" if muted else "false")

    def quiet_hours_active(self, now: datetime | None = None) -> bool:
        start = self.get_preference("quiet_hours_start")
        end = self.get_preference("quiet_hours_end")
        if not start or not end:
            return False
        if start == end:
            return False
        now = now or datetime.now()
        current = now.strftime("%H:%M")
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    def event_alert_key(self, event: CalendarEventSummary) -> str:
        return f"event_soft_alert:{event.title}:{event.starts_at.isoformat()}"

    def event_alerted(self, event: CalendarEventSummary) -> bool:
        return self.memory.get_value("daily_state", self.event_alert_key(event)) is not None

    def mark_event_alerted(self, event: CalendarEventSummary) -> None:
        self.memory.set_value("daily_state", self.event_alert_key(event), datetime.now(timezone.utc).isoformat())

    def mark_today_greeting(self) -> None:
        self.memory.mark_morning_greeting(datetime.now().strftime("%Y-%m-%d"))

    def device_profile_key(self, input_device: int | None) -> str:
        return f"device:{input_device if input_device is not None else 'default'}"

    def get_routine(self, name: str) -> list[str]:
        raw = self.memory.get_value("routines", name, "") or ""
        return [item.strip() for item in raw.split(",") if item.strip()]

    def list_runtime_logs(self, limit: int = 200) -> list[dict[str, str]]:
        return self.memory.list_runtime_logs(limit=limit)

    def remember_fact(
        self,
        key: str,
        value: str,
        source: str = "user",
        confidence: float = 1.0,
        memory_kind: str | None = None,
        entity_key: str | None = None,
        provenance: dict[str, object] | None = None,
    ) -> None:
        self.memory.remember_memory_item(
            key,
            value,
            source=source,
            confidence=confidence,
            memory_kind=memory_kind,
            entity_key=entity_key,
            provenance=provenance,
        )

    def recall_facts(self, query: str, limit: int = 5, scope: str | None = None) -> list[ContextFact]:
        active_scope = scope or self.memory_scope()
        if active_scope in {"off", "session"}:
            return []
        return self.memory.search_facts(query, limit=limit) if query.strip() else self.memory.list_facts(limit=limit)

    def list_facts(self, limit: int = 10) -> list[ContextFact]:
        return self.memory.list_facts(limit=limit)

    def list_open_loops(self, limit: int = 10) -> list[OpenLoop]:
        return self.memory.list_open_loops(limit=limit)

    def create_open_loop(
        self,
        title: str,
        details: str | None = None,
        due_at: datetime | None = None,
        source: str = "assistant",
    ) -> int:
        return self.memory.create_open_loop(title, details=details, due_at=due_at, source=source)

    def assistant_mode(self) -> str:
        return self.memory.get_active_context("runtime", "assistant_mode", "manual") or "manual"

    def set_assistant_mode(self, mode: str) -> None:
        self.memory.set_active_context("runtime", "assistant_mode", mode)

    def set_focus_until(self, due_at: datetime | None) -> None:
        value = due_at.isoformat() if due_at else ""
        self.memory.set_active_context("runtime", "focus_until", value)

    def focus_until(self) -> datetime | None:
        raw = self.memory.get_active_context("runtime", "focus_until", "")
        return datetime.fromisoformat(raw) if raw else None

    def build_morning_brief(self) -> MorningBrief:
        events = [event.model_dump(mode="json") for event in self.list_today_events()]
        tasks = [task.model_dump(mode="json") for task in self.list_pending_tasks()]
        reminders = [reminder.model_dump(mode="json") for reminder in self.list_pending_reminders(limit=3)]
        next_event = self.next_upcoming_event()
        preferred_music = self.get_preference("morning_playlist", "morning jazz")
        focus = tasks[0]["title"] if tasks else "protect the first hour for your highest-priority work"
        return MorningBrief(
            date=datetime.now(),
            events=events,
            tasks=tasks,
            reminders=reminders,
            focus_suggestion=f"Your best focus is {focus}.",
            music_suggestion=preferred_music,
            next_event=next_event.model_dump(mode="json") if next_event else None,
        )

    def status_summary(self) -> dict[str, object]:
        tasks = self.list_pending_tasks()
        events = self.list_today_events()
        reminders = self.list_pending_reminders(limit=5)
        facts = self.list_facts(limit=3)
        loops = self.list_open_loops(limit=5)
        return {
            "pending_task_count": len(tasks),
            "today_event_count": len(events),
            "pending_reminder_count": len(reminders),
            "preferred_title": self.preferred_title(),
            "morning_playlist": self.get_preference("morning_playlist", "morning jazz"),
            "muted": self.muted(),
            "quiet_hours_active": self.quiet_hours_active(),
            "assistant_mode": self.assistant_mode(),
            "remembered_fact_count": len(facts),
            "open_loop_count": len(loops),
        }
