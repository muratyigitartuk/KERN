from __future__ import annotations

import contextlib
import logging
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from app.action_planner import ActionPlanner
from app.config import settings
from app.local_data import LocalDataService
from app.types import ActiveContextSummary, ProactivePrompt

_action_planner = ActionPlanner()

if TYPE_CHECKING:
    from app.documents import DocumentService

logger = logging.getLogger(__name__)

_WATCHER_STATE_LIMIT = 2048
_WATCHER_ALERT_LIMIT = 256


class AttentionManager:
    def __init__(self, local_data: LocalDataService) -> None:
        self.local_data = local_data

    def next_prompt(self, context: ActiveContextSummary, now: datetime | None = None) -> ProactivePrompt | None:
        now = now or datetime.now()
        if self.local_data.quiet_hours_active(now):
            return None

        focus_until = self.local_data.focus_until()
        if focus_until and focus_until <= now:
            self.local_data.set_focus_until(None)
            self.local_data.set_assistant_mode("manual")
            return ProactivePrompt(
                reason="Focus window elapsed",
                message="Your focus window just ended. Do you want a quick review of what changed?",
                source="focus_mode",
            )

        if context.open_loops:
            loop = context.open_loops[0]
            loop_due_at = loop.due_at
            if loop_due_at and loop_due_at <= self._align_datetime(now, loop_due_at):
                return ProactivePrompt(
                    reason="Open loop overdue",
                    message=f"You still have an open commitment: {loop.title}.",
                    source="open_loop",
                )

        next_event = self.local_data.next_upcoming_event(now=now)
        if next_event and 0 <= (next_event.starts_at - now).total_seconds() <= 900:
            return ProactivePrompt(
                reason="Upcoming event",
                message=f"{next_event.title} starts at {next_event.starts_at.strftime('%H:%M')}.",
                source="calendar",
            )

        return None

    def _align_datetime(self, reference: datetime, candidate: datetime) -> datetime:
        if candidate.tzinfo is None or candidate.tzinfo.utcoffset(candidate) is None:
            return reference.replace(tzinfo=None)
        if reference.tzinfo is None or reference.tzinfo.utcoffset(reference) is None:
            return reference.replace(tzinfo=timezone.utc)
        return reference.astimezone(candidate.tzinfo)


# ---------------------------------------------------------------------------
# Watcher helpers
# ---------------------------------------------------------------------------

class FileWatcher:
    """Watch a directory for new or changed files and trigger ingestion alerts."""

    def __init__(
        self,
        watch_dirs: list[Path],
        document_service: "DocumentService",
        profile_slug: str,
        platform=None,
        connection=None,
    ) -> None:
        self._connection = connection
        self._document_service = document_service
        self._profile_slug = profile_slug
        self._platform = platform
        self._dirs = self._load_dirs(watch_dirs)
        self._known: dict[str, float] = {}  # path -> mtime
        self._alerts: list[dict] = []
        self._observer = None
        self._lock = threading.Lock()
        self._supported = {".pdf", ".docx", ".txt", ".md", ".csv", ".xlsx", ".xls", ".json"}
        self._last_reconcile_at: datetime | None = None

    def start(self) -> None:
        """Start watchdog observer if available, otherwise fall back to polling."""
        if not self._dirs:
            return
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            watcher = self

            class _Handler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory:
                        watcher._on_file_changed(Path(event.src_path))

                def on_modified(self, event):
                    if not event.is_directory:
                        watcher._on_file_changed(Path(event.src_path))

            observer = Observer()
            for watch_dir in self._dirs:
                observer.schedule(_Handler(), str(watch_dir), recursive=True)
            observer.daemon = True
            observer.start()
            self._observer = observer
        except ImportError:
            logger.info("watchdog not installed â€” FileWatcher using poll mode")

    def stop(self) -> None:
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:  # cleanup â€” best-effort
                pass
            self._observer = None

    def add_directory(self, path: Path) -> bool:
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_dir():
            return False
        if candidate not in self._dirs:
            self._dirs.append(candidate)
            self._persist_watch_rule(candidate)
        return True

    def list_directories(self) -> list[Path]:
        return list(self._dirs)

    def poll(self) -> list[dict]:
        """Scan watched dirs for new/changed files (polling fallback). Returns new alerts."""
        if self._observer is not None:
            now = datetime.now(timezone.utc)
            if (
                self._last_reconcile_at is not None
                and (now - self._last_reconcile_at).total_seconds() < settings.file_watch_reconcile_minutes * 60
            ):
                return []
            self._last_reconcile_at = now
        new_alerts: list[dict] = []
        for watch_dir in self._dirs:
            try:
                for path in watch_dir.rglob("*"):
                    if path.is_file() and path.suffix.lower() in self._supported:
                        mtime = path.stat().st_mtime
                        key = str(path)
                        if key not in self._known:
                            self._known[key] = mtime
                            alert = self._ingest_and_alert(path, "new_file")
                            if alert:
                                new_alerts.append(alert)
                        elif self._known[key] != mtime:
                            self._known[key] = mtime
                            alert = self._ingest_and_alert(path, "modified_file")
                            if alert:
                                new_alerts.append(alert)
            except Exception as exc:
                logger.warning("FileWatcher poll error in %s: %s", watch_dir, exc)
        current_paths = set()
        for watch_dir in self._dirs:
            with contextlib.suppress(Exception):
                current_paths.update(str(path) for path in watch_dir.rglob("*") if path.is_file())
        for key in list(self._known):
            if key not in current_paths:
                self._known.pop(key, None)
        if len(self._known) > _WATCHER_STATE_LIMIT:
            for key in list(sorted(self._known, key=self._known.get))[:-_WATCHER_STATE_LIMIT]:
                self._known.pop(key, None)
        with self._lock:
            self._alerts.extend(new_alerts)
            if len(self._alerts) > _WATCHER_ALERT_LIMIT:
                self._alerts = self._alerts[-_WATCHER_ALERT_LIMIT:]
        return new_alerts

    def drain_alerts(self) -> list[dict]:
        with self._lock:
            alerts, self._alerts = self._alerts, []
        return alerts

    def _on_file_changed(self, path: Path) -> None:
        if path.suffix.lower() not in self._supported:
            return
        alert = self._ingest_and_alert(path, "file_changed")
        if alert:
            with self._lock:
                self._alerts.append(alert)
                if len(self._alerts) > _WATCHER_ALERT_LIMIT:
                    self._alerts = self._alerts[-_WATCHER_ALERT_LIMIT:]

    def _ingest_and_alert(self, path: Path, event_type: str) -> dict | None:
        try:
            record = self._document_service.ingest_file(path, source="file_watcher")
            msg = f"New file indexed: {record.title}"
            if self._platform:
                self._platform.record_audit(
                    "file_watcher", event_type, "success", msg,
                    profile_slug=self._profile_slug,
                    details={"path": str(path)},
                )
            alert = {
                "type": "file_watch",
                "title": "New file indexed",
                "message": msg,
                "path": str(path),
                "document_id": record.id,
                "document_title": record.title,
                "category": record.category,
                "evidence": [f"{record.title} ({record.category or 'document'})", str(path)],
            }
            alert["suggested_actions"] = _action_planner.suggest_actions(alert)
            return alert
        except Exception as exc:
            logger.debug("FileWatcher ingest skipped for %s: %s", path.name, exc)
            return None

    def _load_dirs(self, watch_dirs: list[Path]) -> list[Path]:
        dirs = [Path(d).expanduser().resolve() for d in watch_dirs if Path(d).is_dir()]
        if self._connection is None:
            return dirs
        try:
            rows = self._connection.execute(
                """
                SELECT config_json
                FROM watch_rules
                WHERE profile_slug = ? AND rule_type = 'folder_watch' AND enabled = 1
                ORDER BY created_at ASC
                """,
                (self._profile_slug,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["config_json"] or "{}")
                folder = str(payload.get("folder_path", "") or "").strip()
                if not folder:
                    continue
                candidate = Path(folder).expanduser().resolve()
                if candidate.is_dir() and candidate not in dirs:
                    dirs.append(candidate)
        except Exception as exc:
            logger.warning("Failed to load watch rules from DB: %s", exc)
        return dirs

    def _persist_watch_rule(self, path: Path) -> None:
        if self._connection is None:
            return
        self._connection.execute(
            """
            INSERT INTO watch_rules (id, profile_slug, rule_type, config_json, enabled, created_at, updated_at)
            VALUES (?, ?, 'folder_watch', ?, 1, ?, ?)
            """,
            (
                f"watch:{self._profile_slug}:{path.as_posix()}",
                self._profile_slug,
                json.dumps({"folder_path": str(path)}),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._connection.commit()



class CalendarWatcher:
    """Alert on upcoming deadlines found in ingested documents or calendar events."""

    def __init__(self, local_data: LocalDataService, interval_seconds: int = 600) -> None:
        self._local_data = local_data
        self._interval = interval_seconds
        self._last_check: datetime | None = None
        self._alerted_keys: set[str] = set()

    def check(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now()
        if self._last_check and (now - self._last_check).total_seconds() < self._interval:
            return []
        self._last_check = now
        alerts: list[dict] = []
        try:
            events = self._local_data.list_upcoming_events(limit=10, now=now)
            for event in events:
                delta = event.starts_at - now
                hours = delta.total_seconds() / 3600
                if 0 < hours <= 24:
                    key = f"cal:{event.title}:{event.starts_at.date()}"
                    if key not in self._alerted_keys:
                        self._alerted_keys.add(key)
                        if len(self._alerted_keys) > _WATCHER_STATE_LIMIT:
                            self._alerted_keys = set(list(self._alerted_keys)[-_WATCHER_STATE_LIMIT:])
                        label = "today" if delta.total_seconds() < 3600 * 6 else "tomorrow"
                        cal_alert = {
                            "type": "calendar",
                            "title": "Upcoming event",
                            "message": f"{event.title} is scheduled {label} at {event.starts_at.strftime('%H:%M')}.",
                            "event_title": event.title,
                            "starts_at": event.starts_at.isoformat(),
                            "importance": event.importance,
                            "evidence": [f"{event.title} at {event.starts_at.strftime('%Y-%m-%d %H:%M')}"],
                        }
                        cal_alert["suggested_actions"] = _action_planner.suggest_actions(cal_alert)
                        alerts.append(cal_alert)
        except Exception as exc:
            logger.debug("CalendarWatcher check error: %s", exc)
        return alerts


class DocumentWatcher:
    """Scan indexed invoices and contracts for approaching due dates."""

    def __init__(
        self,
        document_service: "DocumentService",
        profile_slug: str,
        interval_seconds: int = 600,
        lookahead_days: int = 7,
    ) -> None:
        self._document_service = document_service
        self._profile_slug = profile_slug
        self._interval = interval_seconds
        self._lookahead = lookahead_days
        self._last_check: datetime | None = None
        self._alerted_ids: set[str] = set()

    def check(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        if self._last_check and (now - self._last_check).total_seconds() < self._interval:
            return []
        self._last_check = now
        alerts: list[dict] = []
        try:
            rows = self._document_service.memory.connection.execute(
                """
                SELECT id, title, category, metadata_json
                FROM document_records
                WHERE profile_slug = ? AND category IN ('invoice', 'contract')
                ORDER BY imported_at DESC, id DESC
                LIMIT 50
                """,
                (self._profile_slug,),
            ).fetchall()
            deadline = now + timedelta(days=self._lookahead)
            due_soon = [
                row for row in rows
                if row["id"] not in self._alerted_ids
                and self._metadata_due_within_window(row["metadata_json"], now, deadline)
            ]
            if due_soon:
                count = len(due_soon)
                documents: list[dict[str, str]] = []
                for row in due_soon:
                    self._alerted_ids.add(row["id"])
                    if len(self._alerted_ids) > _WATCHER_STATE_LIMIT:
                        self._alerted_ids = set(list(self._alerted_ids)[-_WATCHER_STATE_LIMIT:])
                    metadata = json.loads(row["metadata_json"] or "{}")
                    documents.append(
                        {
                            "id": row["id"],
                            "title": row["title"],
                            "category": row["category"],
                            "due_date": str(metadata.get("due_date", "") or ""),
                        }
                    )
                doc_alert = {
                    "type": "document",
                    "title": "Documents due soon",
                    "message": f"{count} document{'s' if count > 1 else ''} in your archive require attention within {self._lookahead} days.",
                    "count": count,
                    "documents": documents,
                    "evidence": [
                        f"{item['title']} ({item['due_date'] or 'date missing'})"
                        for item in documents[:5]
                    ],
                }
                doc_alert["suggested_actions"] = _action_planner.suggest_actions(doc_alert)
                alerts.append(doc_alert)
        except Exception as exc:
            logger.debug("DocumentWatcher check error: %s", exc)
        return alerts

    def _metadata_due_within_window(self, raw_metadata: str, now: datetime, deadline: datetime) -> bool:
        try:
            metadata = json.loads(raw_metadata or "{}")
            due_raw = str(metadata.get("due_date", "") or "").strip()
            if not due_raw:
                return False
            due_date = datetime.fromisoformat(due_raw)
            return now <= due_date <= deadline
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid due date in document metadata: %r â€” %s", raw_metadata, exc)
            return False
        except Exception as exc:
            logger.warning("Unexpected error checking document deadline: %s", exc)
            return False
