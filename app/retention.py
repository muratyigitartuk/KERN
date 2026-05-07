from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.memory import MemoryRepository
from app.metrics import metrics
from app.platform import PlatformStore
from app.types import ProfileSummary

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetentionRunResult:
    applied: bool
    ran_at: str
    counts: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    mode: str = "apply"
    reason: str = "scheduled"


class RetentionService:
    RUN_BUCKET = "retention"
    LAST_RUN_KEY = "last_run_at"
    LAST_RESULT_KEY = "last_result_json"

    def __init__(
        self,
        memory: MemoryRepository,
        platform: PlatformStore,
        profile: ProfileSummary,
    ) -> None:
        self.memory = memory
        self.platform = platform
        self.profile = profile

    def status(self) -> dict[str, Any]:
        last_run_at = self.memory.get_value(self.RUN_BUCKET, self.LAST_RUN_KEY)
        last_result_raw = self.memory.get_value(self.RUN_BUCKET, self.LAST_RESULT_KEY, "{}") or "{}"
        try:
            last_result = json.loads(last_result_raw)
        except json.JSONDecodeError:
            last_result = {}
        return {
            "enabled": bool(settings.retention_enforcement_enabled),
            "interval_hours": settings.retention_run_interval_hours,
            "last_run_at": last_run_at,
            "last_result": last_result,
        }

    def run_if_due(self, *, force: bool = False, reason: str = "scheduled") -> RetentionRunResult | None:
        if not settings.retention_enforcement_enabled:
            return None
        last_run_at = self.memory.get_value(self.RUN_BUCKET, self.LAST_RUN_KEY)
        if not force and last_run_at:
            try:
                last_run = datetime.fromisoformat(last_run_at)
            except ValueError:
                last_run = None
            if last_run is not None and datetime.now(timezone.utc) - last_run < timedelta(hours=settings.retention_run_interval_hours):
                return None
        return self.apply(reason=reason)

    def apply(self, *, reason: str = "manual") -> RetentionRunResult:
        ran_at = datetime.now(timezone.utc).isoformat()
        self._current_failures: int = 0
        counts = {
            "documents": self._prune_documents(),
            "deprecated_legacy_email_data": self._prune_legacy_mailbox_messages(),
            "transcripts": self._prune_meetings_and_transcripts(),
            "audit": self._prune_audit_events(),
            "backups": self._prune_backups(),
        }
        failures = {"delete_errors": self._current_failures}
        self._record_failures(failures)
        result = RetentionRunResult(
            applied=True,
            ran_at=ran_at,
            counts=counts,
            failures=failures,
            mode="apply",
            reason=reason,
        )
        self.memory.set_value(self.RUN_BUCKET, self.LAST_RUN_KEY, ran_at)
        self.memory.set_value(
            self.RUN_BUCKET,
            self.LAST_RESULT_KEY,
            json.dumps(
                {
                    "applied": True,
                    "ran_at": ran_at,
                    "counts": counts,
                    "failures": failures,
                    "mode": "apply",
                    "reason": reason,
                },
                sort_keys=True,
            ),
        )
        self.platform.record_audit(
            "governance",
            "retention_enforced",
            "success",
            "Applied profile retention policies.",
            profile_slug=self.profile.slug,
            details={"counts": counts, "reason": reason},
        )
        metrics.inc("kern_retention_deletions_total", labels={"result": "success"})
        return result

    def _prune_documents(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.retention_documents_days)).isoformat()
        rows = self.memory.connection.execute(
            """
            SELECT id, file_path
            FROM document_records
            WHERE profile_slug = ? AND imported_at < ?
            """,
            (self.profile.slug, cutoff),
        ).fetchall()
        removed = 0
        for row in rows:
            self._delete_path(row["file_path"])
            self.memory.connection.execute(
                "DELETE FROM document_records WHERE id = ? AND profile_slug = ?",
                (row["id"], self.profile.slug),
            )
            removed += 1
        self.memory.connection.commit()
        return removed

    def _legacy_table_exists(self, table_name: str) -> bool:
        row = self.memory.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return bool(row)

    def _prune_legacy_mailbox_messages(self) -> int:
        if not self._legacy_table_exists("mailbox_messages"):
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.legacy_email_retention_days)).isoformat()
        rows = self.memory.connection.execute(
            """
            SELECT id, attachment_paths_json, metadata_json
            FROM mailbox_messages
            WHERE profile_slug = ? AND received_at < ?
            """,
            (self.profile.slug, cutoff),
        ).fetchall()
        removed = 0
        for row in rows:
            attachment_paths = json.loads(row["attachment_paths_json"] or "[]")
            metadata = json.loads(row["metadata_json"] or "{}")
            for path in attachment_paths:
                self._delete_path(path)
            raw_path = metadata.get("raw_path")
            if isinstance(raw_path, str):
                self._delete_path(raw_path)
            self.memory.connection.execute(
                "DELETE FROM mailbox_messages WHERE id = ? AND profile_slug = ?",
                (row["id"], self.profile.slug),
            )
            removed += 1
        self.memory.connection.commit()
        return removed

    def _prune_meetings_and_transcripts(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.retention_transcripts_days)).isoformat()
        rows = self.memory.connection.execute(
            """
            SELECT id, audio_path, transcript_path
            FROM meeting_records
            WHERE profile_slug = ? AND created_at < ?
            """,
            (self.profile.slug, cutoff),
        ).fetchall()
        removed = 0
        for row in rows:
            artifact_rows = self.memory.connection.execute(
                """
                SELECT file_path
                FROM transcript_artifacts
                WHERE profile_slug = ? AND meeting_id = ?
                """,
                (self.profile.slug, row["id"]),
            ).fetchall()
            for artifact in artifact_rows:
                self._delete_path(artifact["file_path"])
            self._delete_path(row["audio_path"])
            self._delete_path(row["transcript_path"])
            self.memory.connection.execute(
                "DELETE FROM transcript_action_items WHERE profile_slug = ? AND meeting_id = ?",
                (self.profile.slug, row["id"]),
            )
            self.memory.connection.execute(
                "DELETE FROM meeting_records WHERE profile_slug = ? AND id = ?",
                (self.profile.slug, row["id"]),
            )
            removed += 1
        self.memory.connection.commit()
        return removed

    def _prune_audit_events(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.retention_audit_days)).isoformat()
        return self.platform.prune_audit_events(self.profile.slug, cutoff)

    def _prune_backups(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.retention_backups_days)
        removed = 0
        backup_roots: set[Path] = {Path(self.profile.backups_root).resolve()}
        for target in self.platform.list_backup_targets(self.profile.slug):
            try:
                backup_roots.add(Path(target.path).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        backup_roots.add((settings.root_path / "upgrade-backups").resolve())
        for backup_root in backup_roots:
            if not backup_root.exists():
                continue
            for path in backup_root.glob(f"{self.profile.slug}-*.kernbak"):
                created_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if created_at >= cutoff:
                    continue
                self._delete_path(path)
                removed += 1
        return removed

    def _record_failures(self, failures: dict[str, int]) -> None:
        if not failures.get("delete_errors"):
            return
        self._ensure_retention_failures_table()
        ran_at = datetime.now(timezone.utc).isoformat()
        self.memory.connection.execute(
            "INSERT INTO retention_failures (profile_slug, ran_at, failure_count) VALUES (?, ?, ?)",
            (self.profile.slug, ran_at, failures["delete_errors"]),
        )
        self.memory.connection.commit()

    def _ensure_retention_failures_table(self) -> None:
        self.memory.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS retention_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_slug TEXT NOT NULL,
                ran_at TEXT NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )

    def _delete_path(self, raw_path: str | Path | None) -> None:
        if not raw_path:
            return
        path = Path(raw_path)
        if not path.exists():
            logger.info("Retention: %s already deleted", path)
            return
        try:
            path.unlink()
        except PermissionError:
            logger.error("Retention: cannot delete %s â€” file locked", path)
            self._current_failures = getattr(self, "_current_failures", 0) + 1
            metrics.inc("kern_retention_deletions_total", labels={"result": "failure"})
        except OSError as exc:
            logger.error("Retention: failed to delete %s â€” %s", path, exc)
            self._current_failures = getattr(self, "_current_failures", 0) + 1
            metrics.inc("kern_retention_deletions_total", labels={"result": "failure"})
