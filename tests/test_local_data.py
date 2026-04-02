from datetime import datetime, timedelta
from pathlib import Path

from app.database import connect
from app.local_data import LocalDataService
from app.memory import MemoryRepository


def test_announced_reminder_stays_actionable_but_not_due(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    data = LocalDataService(repo, "sir")
    reminder_id = data.create_reminder("Stretch", datetime.now() - timedelta(minutes=1))

    data.mark_reminder_announced(reminder_id)

    pending = data.list_pending_reminders(limit=5)
    due = data.list_due_reminders()

    assert any(reminder.id == reminder_id and reminder.status == "announced" for reminder in pending)
    assert all(reminder.id != reminder_id for reminder in due)


def test_next_upcoming_event_skips_past_events(tmp_path: Path, monkeypatch):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    current = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current if tz is None else current.astimezone(tz)

    monkeypatch.setattr("app.local_data.datetime", FixedDateTime)
    now = current
    repo.connection.execute("DELETE FROM local_calendar_events")
    repo.connection.executemany(
        "INSERT INTO local_calendar_events (title, starts_at, ends_at, importance) VALUES (?, ?, ?, ?)",
        [
            ("Past review", (now - timedelta(hours=2)).isoformat(), None, 2),
            ("Upcoming sync", (now + timedelta(hours=1)).isoformat(), None, 3),
        ],
    )
    repo.connection.commit()
    data = LocalDataService(repo, "sir")

    next_event = data.next_upcoming_event(now=now)
    brief = data.build_morning_brief()

    assert next_event is not None
    assert next_event.title == "Upcoming sync"
    assert brief.next_event is not None
    assert brief.next_event["title"] == "Upcoming sync"


def test_cleanup_rollout_legacy_assistant_state_dismisses_calibration_prompts(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    data = LocalDataService(repo, "sir")
    task_id = data.create_task("Review JARVIS architecture", priority=3)
    reminder_id = data.create_reminder("Check JARVIS calibration profile", datetime.now() - timedelta(minutes=5))
    repo.create_open_loop(
        title="Check JARVIS calibration profile",
        details="Reminder pending",
        due_at=datetime.now() - timedelta(minutes=5),
        source="reminder",
        related_type="reminder",
        related_id=reminder_id,
    )
    repo.append_conversation_entry("You still have an open commitment: Check JARVIS calibration profile.")

    result = data.cleanup_rollout_legacy_assistant_state()

    task = repo.connection.execute("SELECT title FROM local_tasks WHERE id = ?", (task_id,)).fetchone()
    reminder = repo.connection.execute("SELECT status FROM local_reminders WHERE id = ?", (reminder_id,)).fetchone()
    loop = repo.connection.execute(
        "SELECT status FROM open_loops WHERE related_type = 'reminder' AND related_id = ?",
        (reminder_id,),
    ).fetchone()
    renamed_loop = repo.connection.execute(
        "SELECT title FROM open_loops WHERE title = 'Review KERN architecture' LIMIT 1"
    ).fetchone()
    remaining_log = repo.list_recent_conversation_entries(limit=10)

    assert result["tasks_renamed"] == 1
    assert task["title"] == "Review KERN architecture"
    assert result["reminders_completed"] == 1
    assert reminder["status"] == "completed"
    assert loop["status"] == "dismissed"
    assert renamed_loop is not None
    assert not any("jarvis calibration" in entry.lower() for entry in remaining_log)
