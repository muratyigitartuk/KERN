from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.config import settings
from app.metrics import metrics

if TYPE_CHECKING:
    import sqlite3

logger = logging.getLogger(__name__)
_CRON_LIMIT_MINUTES = 366 * 24 * 60
_ALLOWED_ACTION_TYPES = frozenset({"custom_prompt", "generate_report"})
_CRON_FIELD_RANGES = (
    (0, 59),
    (0, 23),
    (1, 31),
    (1, 12),
    (0, 7),
)


def _normalize_dow(value: int) -> int:
    return 0 if value == 7 else value


def _expand_cron_field(field: str, minimum: int, maximum: int, *, normalize_dow: bool = False) -> tuple[set[int], bool]:
    token = field.strip()
    if not token:
        raise ValueError("Cron field cannot be empty.")
    is_wildcard = token == "*"
    values: set[int] = set()
    for part in token.split(","):
        item = part.strip()
        if not item:
            raise ValueError(f"Invalid cron field segment: {field!r}")
        if "/" in item:
            base, step_raw = item.split("/", 1)
            step = int(step_raw)
            if step <= 0:
                raise ValueError(f"Cron step must be positive: {item!r}")
        else:
            base, step = item, 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_raw, end_raw = base.split("-", 1)
            start, end = int(start_raw), int(end_raw)
        else:
            start = end = int(base)
        if normalize_dow:
            start = _normalize_dow(start)
            end = _normalize_dow(end)
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"Cron field out of range: {item!r}")
        values.update(range(start, end + 1, step))
    return values, is_wildcard


def _parse_cron_expression(cron_expression: str) -> tuple[tuple[set[int], bool], ...]:
    fields = cron_expression.split()
    if len(fields) != 5:
        raise ValueError("Cron expression must contain exactly 5 fields.")
    parsed: list[tuple[set[int], bool]] = []
    for index, field in enumerate(fields):
        minimum, maximum = _CRON_FIELD_RANGES[index]
        parsed.append(_expand_cron_field(field, minimum, maximum, normalize_dow=index == 4))
    return tuple(parsed)


def _cron_matches(moment: datetime, parsed: tuple[tuple[set[int], bool], ...]) -> bool:
    (minutes, _), (hours, _), (days, day_wildcard), (months, _), (dows, dow_wildcard) = parsed
    minute_match = moment.minute in minutes
    hour_match = moment.hour in hours
    month_match = moment.month in months
    day_match = moment.day in days
    dow_match = (moment.isoweekday() % 7) in dows
    if not minute_match or not hour_match or not month_match:
        return False
    if day_wildcard and dow_wildcard:
        return True
    if day_wildcard:
        return dow_match
    if dow_wildcard:
        return day_match
    return day_match or dow_match


def _next_run_from_cron(cron_expression: str, after: datetime) -> datetime:
    try:
        from croniter import croniter  # type: ignore

        return croniter(cron_expression, after).get_next(datetime)
    except ImportError:
        parsed = _parse_cron_expression(cron_expression)
        candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(_CRON_LIMIT_MINUTES):
            if _cron_matches(candidate, parsed):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError(f"Cron expression does not produce a run within {_CRON_LIMIT_MINUTES} minutes.")


class SchedulerService:
    """Persistent cron-like task scheduler backed by the profile SQLite database."""

    def __init__(
        self,
        connection: "sqlite3.Connection",
        profile_slug: str,
        *,
        retry_delay_minutes: int | None = None,
        max_retries: int | None = None,
        stale_run_minutes: int | None = None,
    ) -> None:
        self._db = connection
        self._profile_slug = profile_slug
        self._retry_delay_minutes = retry_delay_minutes if retry_delay_minutes is not None else settings.scheduler_retry_delay_minutes
        self._max_retries = max_retries if max_retries is not None else settings.scheduler_max_retries
        self._stale_run_minutes = stale_run_minutes if stale_run_minutes is not None else settings.scheduler_stale_run_minutes

    def validate_cron_expression(self, cron_expression: str, *, after: datetime | None = None) -> dict:
        anchor = after or datetime.now(timezone.utc)
        next_run = _next_run_from_cron(cron_expression, anchor)
        return {"valid": True, "next_run_at": next_run.isoformat()}

    def create_task(
        self,
        title: str,
        cron_expression: str,
        action_type: str = "custom_prompt",
        action_payload: dict | None = None,
        max_retries: int | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        task_id = str(uuid.uuid4())
        action_type = str(action_type or "custom_prompt").strip()
        if action_type not in _ALLOWED_ACTION_TYPES:
            raise ValueError(f"Unsupported scheduled action type: {action_type}")
        if action_payload is not None and not isinstance(action_payload, dict):
            raise ValueError("Scheduled action payload must be an object.")
        preview = self.validate_cron_expression(cron_expression, after=now)
        retry_budget = self._max_retries if max_retries is None else max(0, int(max_retries))
        self._db.execute(
            """
            INSERT INTO scheduled_tasks
                (id, profile_slug, title, cron_expression, action_type,
                 action_payload_json, enabled, next_run_at, created_at, updated_at,
                 run_status, failure_count, retry_attempts, max_retries, last_result_json, run_started_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'idle', 0, 0, ?, '{}', NULL)
            """,
            (
                task_id,
                self._profile_slug,
                title,
                cron_expression,
                action_type,
                json.dumps(action_payload or {}),
                preview["next_run_at"],
                now.isoformat(),
                now.isoformat(),
                retry_budget,
            ),
        )
        self._db.commit()
        return self._row_to_dict(self._db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone())

    def list_tasks(self) -> list[dict]:
        rows = self._db.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE profile_slug = ?
            ORDER BY enabled DESC, next_run_at, updated_at DESC
            """,
            (self._profile_slug,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def toggle_task(self, task_id: str, enabled: bool) -> bool:
        self._db.execute(
            "UPDATE scheduled_tasks SET enabled = ?, updated_at = ? WHERE id = ? AND profile_slug = ?",
            (1 if enabled else 0, datetime.now(timezone.utc).isoformat(), task_id, self._profile_slug),
        )
        self._db.commit()
        return self._db.execute(
            "SELECT COUNT(*) FROM scheduled_tasks WHERE id = ? AND profile_slug = ?",
            (task_id, self._profile_slug),
        ).fetchone()[0] > 0

    def delete_task(self, task_id: str) -> bool:
        cur = self._db.execute(
            "DELETE FROM scheduled_tasks WHERE id = ? AND profile_slug = ?",
            (task_id, self._profile_slug),
        )
        self._db.commit()
        return cur.rowcount > 0

    def tick(self, now: datetime | None = None) -> list[dict]:
        """Return tasks due right now and mark them as running."""
        now = now or datetime.now(timezone.utc)
        now_str = now.isoformat()
        rows = self._db.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE profile_slug = ?
              AND enabled = 1
              AND next_run_at <= ?
              AND run_status != 'running'
            ORDER BY next_run_at
            """,
            (self._profile_slug, now_str),
        ).fetchall()
        due: list[dict] = []
        for row in rows:
            task = self._row_to_dict(row)
            claimed = self._db.execute(
                """
                UPDATE scheduled_tasks
                SET run_status = 'running', updated_at = ?, run_started_at = ?, last_error = NULL
                WHERE id = ? AND run_status != 'running'
                """,
                (now_str, now_str, task["id"]),
            )
            if claimed.rowcount == 0:
                continue
            task["run_status"] = "running"
            task["run_started_at"] = now_str
            due.append(task)
        if rows:
            self._db.commit()
        return due

    def mark_task_completed(self, task_id: str, *, completed_at: datetime | None = None, result: dict | None = None) -> dict:
        completed_at = completed_at or datetime.now(timezone.utc)
        row = self._db.execute(
            "SELECT cron_expression FROM scheduled_tasks WHERE id = ? AND profile_slug = ?",
            (task_id, self._profile_slug),
        ).fetchone()
        if row is None:
            return {}
        next_run = _next_run_from_cron(str(row["cron_expression"]), completed_at)
        self._db.execute(
            """
            UPDATE scheduled_tasks
            SET last_run_at = ?, next_run_at = ?, updated_at = ?, run_status = 'completed',
                failure_count = 0, retry_attempts = 0, last_error = NULL, last_result_json = ?, run_started_at = NULL
            WHERE id = ? AND profile_slug = ?
            """,
            (
                completed_at.isoformat(),
                next_run.isoformat(),
                completed_at.isoformat(),
                json.dumps(result or {}),
                task_id,
                self._profile_slug,
            ),
        )
        self._db.commit()
        metrics.inc("kern_scheduler_task_executions_total", labels={"result": "success"})
        return self._row_to_dict(self._db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone())

    def mark_task_failed(self, task_id: str, error: str, *, failed_at: datetime | None = None) -> dict:
        failed_at = failed_at or datetime.now(timezone.utc)
        row = self._db.execute(
            """
            SELECT retry_attempts, max_retries, failure_count
            FROM scheduled_tasks
            WHERE id = ? AND profile_slug = ?
            """,
            (task_id, self._profile_slug),
        ).fetchone()
        if row is None:
            return {}
        attempt = int(row["retry_attempts"] or 0) + 1
        failure_count = int(row["failure_count"] or 0) + 1
        max_retries = int(row["max_retries"] or self._max_retries)
        if attempt > max_retries:
            enabled = 0
            run_status = "failed"
            next_run = None
        else:
            enabled = 1
            run_status = "retry_pending"
            delay = timedelta(minutes=max(1, self._retry_delay_minutes * attempt))
            next_run = (failed_at + delay).isoformat()
        self._db.execute(
            """
            UPDATE scheduled_tasks
            SET updated_at = ?, run_status = ?, failure_count = ?, retry_attempts = ?, last_error = ?, enabled = ?,
                next_run_at = COALESCE(?, next_run_at), run_started_at = NULL
            WHERE id = ? AND profile_slug = ?
            """,
            (
                failed_at.isoformat(),
                run_status,
                failure_count,
                attempt,
                error,
                enabled,
                next_run,
                task_id,
                self._profile_slug,
            ),
        )
        self._db.commit()
        metrics.inc("kern_scheduler_task_executions_total", labels={"result": "failure"})
        return self._row_to_dict(self._db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone())

    def record_success(self, task_id: str, result: dict | None = None) -> dict:
        return self.mark_task_completed(task_id, result=result)

    def record_failure(self, task_id: str, error: str) -> dict:
        return self.mark_task_failed(task_id, error)

    def create_schedule(
        self,
        title: str,
        cron_expression: str,
        action_type: str = "custom_prompt",
        action_payload: dict | None = None,
        profile_slug: str | None = None,
        max_retries: int | None = None,
    ) -> dict:
        return self.create_task(title, cron_expression, action_type, action_payload, max_retries=max_retries)

    def list_schedules(self) -> list[dict]:
        return self.list_tasks()

    def delete_schedule(self, schedule_id: str) -> bool:
        return self.delete_task(schedule_id)

    def toggle_schedule(self, schedule_id: str, enabled: bool) -> bool:
        return self.toggle_task(schedule_id, enabled)

    def recover_stale_runs(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        stale_before = (now - timedelta(minutes=max(1, self._stale_run_minutes))).isoformat()
        rows = self._db.execute(
            """
            SELECT *
            FROM scheduled_tasks
            WHERE profile_slug = ?
              AND run_status = 'running'
              AND (run_started_at IS NULL OR run_started_at <= ?)
            ORDER BY updated_at ASC, id ASC
            """,
            (self._profile_slug, stale_before),
        ).fetchall()
        recovered: list[dict] = []
        for row in rows:
            task = self.mark_task_failed(
                str(row["id"]),
                "Recovered stale scheduled task after interrupted runtime.",
                failed_at=now,
            )
            if task:
                recovered.append(task)
        return recovered

    def retry_failed_task(self, task_id: str) -> dict:
        """Re-enable a failed task by resetting its retry state and scheduling the next run."""
        row = self._db.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ? AND profile_slug = ?",
            (task_id, self._profile_slug),
        ).fetchone()
        if row is None:
            return {}
        now = datetime.now(timezone.utc)
        next_run = _next_run_from_cron(str(row["cron_expression"]), now)
        self._db.execute(
            """
            UPDATE scheduled_tasks
            SET enabled = 1, run_status = 'idle', retry_attempts = 0, failure_count = 0,
                last_error = NULL, next_run_at = ?, updated_at = ?, run_started_at = NULL
            WHERE id = ? AND profile_slug = ?
            """,
            (next_run.isoformat(), now.isoformat(), task_id, self._profile_slug),
        )
        self._db.commit()
        return self._row_to_dict(self._db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone())

    @staticmethod
    def _row_to_dict(row) -> dict:
        if row is None:
            return {}
        d = dict(row)
        try:
            d["action_payload"] = json.loads(d.pop("action_payload_json", "{}") or "{}")
        except Exception as exc:
            logger.debug("Failed to parse action_payload JSON: %s", exc)
            d["action_payload"] = {}
        try:
            d["last_result"] = json.loads(d.pop("last_result_json", "{}") or "{}")
        except Exception as exc:
            logger.debug("Failed to parse last_result JSON: %s", exc)
            d["last_result"] = {}
        d["enabled"] = bool(d.get("enabled", 1))
        status = str(d.get("run_status", "idle") or "idle")
        d["status"] = "retrying" if status == "retry_pending" else status
        d["run_started_at"] = d.get("run_started_at")
        return d
