from __future__ import annotations

import json
import logging
import re
import sqlite3
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

logger = logging.getLogger(__name__)


def _placeholders(count: int) -> str:
    if count <= 0:
        raise ValueError("At least one placeholder is required.")
    return ",".join("?" for _ in range(count))

from app.types import (
    ContextLinkRecord,
    DocumentAnswerPacket,
    CalendarEventSummary,
    ComplianceReminderRule,
    ContextFact,
    DecisionRecord,
    FeedbackSignal,
    InteractionOutcomeRecord,
    ConversationArchiveRecord,
    DocumentChunk,
    DocumentRecord,
    ExecutionReceipt,
    GermanBusinessDocument,
    MeetingRecord,
    RegulatedDocumentRecord,
    RegulatedDocumentVersion,
    ObligationRecord,
    OpenLoop,
    RecoveryCheckpoint,
    ReminderSummary,
    RetrievalHit,
    ShadowRankingRecord,
    SyncTarget,
    TaskSummary,
    ThreadContextPacket,
    TrainingExampleRecord,
    TranscriptArtifact,
    WorkflowDomainEvent,
    WorkflowEvent,
    WorkflowRecord,
    PersonContextPacket,
)


class MemoryRepository:
    CONVERSATION_RETENTION = 200
    RUNTIME_LOG_RETENTION = 1000
    RECEIPT_RETENTION = 200
    ARTIFACT_RETENTION = 500

    def __init__(self, connection: sqlite3.Connection, profile_slug: str = "default", *, llm_client=None) -> None:
        self.connection = connection
        self.profile_slug = profile_slug
        self._llm_client = llm_client

    def _has_column(self, table: str, column: str) -> bool:
        rows = self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    def _trim_table(self, table: str, limit: int, order_column: str = "created_at") -> None:
        self.connection.execute(
            f"""
            DELETE FROM {table}
            WHERE id NOT IN (
                SELECT id FROM {table}
                ORDER BY {order_column} DESC, id DESC
                LIMIT ?
            )
            """,
            (limit,),
        )

    def set_value(self, bucket: str, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO key_value_store (bucket, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(bucket, key) DO UPDATE SET value = excluded.value
            """,
            (bucket, key, value),
        )
        self.connection.commit()

    def get_value(self, bucket: str, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM key_value_store WHERE bucket = ? AND key = ?",
            (bucket, key),
        ).fetchone()
        return row["value"] if row else default

    def list_values(self, bucket: str, *, prefix: str | None = None, limit: int = 100) -> list[tuple[str, str]]:
        query = "SELECT key, value FROM key_value_store WHERE bucket = ?"
        params: list[object] = [bucket]
        if prefix:
            query += " AND key LIKE ?"
            params.append(f"{prefix}%")
        query += " ORDER BY key DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, tuple(params)).fetchall()
        return [(str(row["key"]), str(row["value"])) for row in rows]

    def store_document_answer_packet(self, packet: DocumentAnswerPacket) -> DocumentAnswerPacket:
        self.set_value("document_answer_packets", packet.id, packet.model_dump_json())
        return packet

    def get_document_answer_packet(self, packet_id: str) -> DocumentAnswerPacket | None:
        raw = self.get_value("document_answer_packets", packet_id)
        if not raw:
            return None
        try:
            return DocumentAnswerPacket.model_validate_json(raw)
        except Exception:
            logger.debug("document answer packet decode failed for %s", packet_id, exc_info=True)
            return None

    def store_thread_context_packet(self, packet: ThreadContextPacket) -> ThreadContextPacket:
        self.set_value("thread_context_packets", packet.id, packet.model_dump_json())
        return packet

    def get_thread_context_packet(self, packet_id: str) -> ThreadContextPacket | None:
        raw = self.get_value("thread_context_packets", packet_id)
        if not raw:
            return None
        try:
            return ThreadContextPacket.model_validate_json(raw)
        except Exception:
            logger.debug("thread context packet decode failed for %s", packet_id, exc_info=True)
            return None

    def store_person_context_packet(self, packet: PersonContextPacket) -> PersonContextPacket:
        self.set_value("person_context_packets", packet.id, packet.model_dump_json())
        return packet

    def get_person_context_packet(self, packet_id: str) -> PersonContextPacket | None:
        raw = self.get_value("person_context_packets", packet_id)
        if not raw:
            return None
        try:
            return PersonContextPacket.model_validate_json(raw)
        except Exception:
            logger.debug("person context packet decode failed for %s", packet_id, exc_info=True)
            return None

    def record_context_link(self, record: ContextLinkRecord) -> ContextLinkRecord:
        self.set_value("context_link_records", record.id, record.model_dump_json())
        return record

    def list_context_link_records(
        self,
        *,
        workspace_slug: str | None = None,
        actor_user_id: str | None = None,
        source_ref: str | None = None,
        target_ref: str | None = None,
        limit: int = 100,
    ) -> list[ContextLinkRecord]:
        records: list[ContextLinkRecord] = []
        for _, raw in self.list_values("context_link_records", limit=limit * 3):
            try:
                record = ContextLinkRecord.model_validate_json(raw)
            except Exception:
                logger.debug("context link decode failed", exc_info=True)
                continue
            if workspace_slug is not None and record.workspace_slug != workspace_slug:
                continue
            if actor_user_id is not None and record.actor_user_id not in {None, actor_user_id}:
                continue
            if source_ref is not None and record.source_ref != source_ref:
                continue
            if target_ref is not None and record.target_ref != target_ref:
                continue
            records.append(record)
        records.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return records[:limit]

    def record_interaction_outcome(self, record: InteractionOutcomeRecord) -> InteractionOutcomeRecord:
        self.set_value("interaction_outcome_records", record.id, record.model_dump_json())
        return record

    def list_interaction_outcomes(
        self,
        *,
        workspace_slug: str | None = None,
        actor_user_id: str | None = None,
        packet_id: str | None = None,
        limit: int = 100,
    ) -> list[InteractionOutcomeRecord]:
        records: list[InteractionOutcomeRecord] = []
        for _, raw in self.list_values("interaction_outcome_records", limit=limit * 3):
            try:
                record = InteractionOutcomeRecord.model_validate_json(raw)
            except Exception:
                logger.debug("interaction outcome decode failed", exc_info=True)
                continue
            if workspace_slug is not None and record.workspace_slug != workspace_slug:
                continue
            if actor_user_id is not None and record.actor_user_id not in {None, actor_user_id}:
                continue
            if packet_id is not None and record.packet_id != packet_id:
                continue
            records.append(record)
        records.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return records[:limit]

    def _structured_memory_supported(self) -> bool:
        return self._has_column("structured_memory_items", "memory_kind")

    def _memory_kind_for_key(self, key: str, source: str = "user") -> str:
        lowered = key.strip().lower()
        if lowered in {"response style", "preferred editor", "preferred title"}:
            return "preference"
        if any(token in lowered for token in ("prefer", "preference", "style")):
            return "preference"
        if source == "memory_consolidation" and lowered.startswith("decision_"):
            return "decision"
        if source == "memory_consolidation" and lowered.startswith("commitment_"):
            return "commitment"
        if source == "memory_consolidation" and lowered.startswith("episode_"):
            return "episodic_summary"
        return "fact"

    def _memory_item_key(self, prefix: str, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        return f"{prefix}_{normalized[:48] or 'item'}"

    def _row_to_context_fact(self, row: sqlite3.Row) -> ContextFact:
        provenance: dict[str, object] = {}
        if "provenance_json" in row.keys() and row["provenance_json"]:
            try:
                loaded = json.loads(row["provenance_json"])
                if isinstance(loaded, dict):
                    provenance = loaded
            except json.JSONDecodeError:
                provenance = {}
        updated_at_raw = row["updated_at"] if "updated_at" in row.keys() else None
        return ContextFact(
            key=row["key"],
            value=row["value"],
            source=row["source"] if "source" in row.keys() else "user",
            confidence=float(row["confidence"] if "confidence" in row.keys() else 1.0),
            memory_kind=row["memory_kind"] if "memory_kind" in row.keys() else self._memory_kind_for_key(row["key"], row["source"] if "source" in row.keys() else "user"),
            entity_key=row["entity_key"] if "entity_key" in row.keys() else None,
            status=row["status"] if "status" in row.keys() else "active",
            provenance=provenance,
            updated_at=datetime.fromisoformat(updated_at_raw) if updated_at_raw else None,
        )

    def remember_memory_item(
        self,
        key: str,
        value: str,
        *,
        memory_kind: str | None = None,
        source: str = "user",
        confidence: float = 1.0,
        entity_key: str | None = None,
        provenance: dict[str, object] | None = None,
    ) -> str:
        memory_kind = memory_kind or self._memory_kind_for_key(key, source)
        provenance = provenance or {}
        now = datetime.now(timezone.utc).isoformat()
        if self._structured_memory_supported():
            existing = self.connection.execute(
                """
                SELECT id, value
                FROM structured_memory_items
                WHERE profile_slug = ?
                  AND key = ?
                  AND memory_kind = ?
                  AND COALESCE(entity_key, '') = COALESCE(?, '')
                  AND status = 'active'
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (self.profile_slug, key, memory_kind, entity_key),
            ).fetchone()
            if existing and str(existing["value"]).strip().lower() == value.strip().lower():
                self.connection.execute(
                    """
                    UPDATE structured_memory_items
                    SET source = ?, confidence = ?, provenance_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (source, confidence, json.dumps(provenance), now, existing["id"]),
                )
                item_id = str(existing["id"])
            else:
                item_id = f"mem-{uuid4().hex}"
                if existing:
                    self.connection.execute(
                        """
                        UPDATE structured_memory_items
                        SET status = 'superseded', superseded_by_id = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (item_id, now, existing["id"]),
                    )
                self.connection.execute(
                    """
                    INSERT INTO structured_memory_items (
                        id,
                        profile_slug,
                        memory_kind,
                        key,
                        value,
                        entity_key,
                        source,
                        confidence,
                        status,
                        superseded_by_id,
                        provenance_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?)
                    """,
                    (
                        item_id,
                        self.profile_slug,
                        memory_kind,
                        key,
                        value,
                        entity_key,
                        source,
                        confidence,
                        json.dumps(provenance),
                        now,
                        now,
                    ),
                )
        else:
            item_id = f"legacy-{key}"
        self.connection.execute(
            """
            INSERT INTO memory_entries (key, value, source, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key, value, source, confidence, now, now),
        )
        self.connection.execute(
            """
            INSERT INTO assistant_facts (key, value, source, confidence, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key)
            DO UPDATE SET
                value = excluded.value,
                source = excluded.source,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (key, value, source, confidence, now),
        )
        self.connection.commit()
        return item_id

    def append_conversation_entry(self, content: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "INSERT INTO conversation_log (created_at, content) VALUES (?, ?)",
            (now, content),
        )
        if self._structured_memory_supported() and content.strip():
            self.connection.execute(
                """
                INSERT INTO structured_memory_items (
                    id,
                    profile_slug,
                    memory_kind,
                    key,
                    value,
                    entity_key,
                    source,
                    confidence,
                    status,
                    superseded_by_id,
                    provenance_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, 'episodic_turn', ?, ?, NULL, 'conversation', ?, 'active', NULL, ?, ?, ?)
                """,
                (
                    f"turn-{uuid4().hex}",
                    self.profile_slug,
                    self._memory_item_key("turn", content),
                    content,
                    0.46,
                    json.dumps({"origin": "conversation_log"}),
                    now,
                    now,
                ),
            )
        self._trim_table("conversation_log", self.CONVERSATION_RETENTION)
        self.connection.commit()

    def list_recent_conversation_entries(self, limit: int = 20) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT content
            FROM conversation_log
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row["content"] for row in rows]

    def append_conversation_summary(self, summary: str) -> None:
        self.append_conversation_entry(summary)

    def list_recent_conversation_summaries(self, limit: int = 20) -> list[str]:
        return self.list_recent_conversation_entries(limit)

    def append_runtime_log(self, category: str, message: str) -> None:
        self.connection.execute(
            "INSERT INTO runtime_logs (created_at, category, message) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), category, message),
        )
        self._trim_table("runtime_logs", self.RUNTIME_LOG_RETENTION)
        self.connection.commit()

    def list_runtime_logs(self, limit: int = 200) -> list[dict[str, str]]:
        rows = self.connection.execute(
            """
            SELECT created_at, category, message
            FROM runtime_logs
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "created_at": row["created_at"],
                "category": row["category"],
                "message": row["message"],
            }
            for row in rows
        ]

    def mark_morning_greeting(self, date_key: str) -> None:
        self.set_value("daily_state", f"morning_greeting:{date_key}", datetime.now(timezone.utc).isoformat())

    def has_morning_greeting(self, date_key: str) -> bool:
        return self.get_value("daily_state", f"morning_greeting:{date_key}") is not None

    def create_note(self, content: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO notes (created_at, content) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(), content),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_notes(self, limit: int = 20) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT content
            FROM notes
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row["content"] for row in rows]

    def seed_defaults(self, include_demo_data: bool = False) -> None:
        if not self.get_value("user_profile", "name"):
            self.set_value("user_profile", "name", "Murat")
        if not self.get_value("preferences", "preferred_title"):
            self.set_value("preferences", "preferred_title", "")
        if not self.get_value("preferences", "muted"):
            self.set_value("preferences", "muted", "false")
        if not self.get_value("preferences", "quiet_hours_start"):
            self.set_value("preferences", "quiet_hours_start", "22:30")
        if not self.get_value("preferences", "quiet_hours_end"):
            self.set_value("preferences", "quiet_hours_end", "07:00")

        if not self.get_value("routines", "morning"):
            self.set_value("routines", "morning", "brief,events,tasks,reminders,music")
        if not self.get_value("routines", "focus"):
            self.set_value("routines", "focus", "status,top_task,focus_timer")
        if not self.get_value("routines", "shutdown"):
            self.set_value("routines", "shutdown", "status,remaining_tasks,tomorrow_prompt")

        if include_demo_data:
            task_count = self.connection.execute("SELECT COUNT(*) AS count FROM local_tasks").fetchone()["count"]
            if task_count == 0:
                self.connection.executemany(
                    "INSERT INTO local_tasks (title, priority, due_at) VALUES (?, ?, ?)",
                    [
                        ("Review KERN architecture", 4, None),
                        ("Prepare focus list for today", 3, None),
                    ],
                )

            event_count = self.connection.execute("SELECT COUNT(*) AS count FROM local_calendar_events").fetchone()["count"]
            if event_count == 0:
                now = datetime.now().replace(minute=0, second=0, microsecond=0)
                self.connection.executemany(
                    "INSERT INTO local_calendar_events (title, starts_at, ends_at, importance) VALUES (?, ?, ?, ?)",
                    [
                        ("Architecture review", now.isoformat(), None, 4),
                        ("Product sync", now.replace(hour=min(now.hour + 2, 23)).isoformat(), None, 3),
                    ],
                )
            reminder_count = self.connection.execute("SELECT COUNT(*) AS count FROM local_reminders").fetchone()["count"]
            if reminder_count == 0:
                later = datetime.now() + timedelta(minutes=45)
                self.connection.execute(
                    """
                    INSERT INTO local_reminders (title, due_at, status, kind, created_at)
                    VALUES (?, ?, 'pending', 'reminder', ?)
                    """,
                    ("Review next deadline", later.isoformat(), datetime.now(timezone.utc).isoformat()),
                )

            open_loop_count = self.connection.execute("SELECT COUNT(*) AS count FROM open_loops").fetchone()["count"]
            if open_loop_count == 0:
                task_rows = self.connection.execute(
                    "SELECT id, title FROM local_tasks WHERE completed = 0 ORDER BY id ASC LIMIT 5"
                ).fetchall()
                reminder_rows = self.connection.execute(
                    "SELECT id, title, due_at FROM local_reminders WHERE status IN ('pending', 'announced', 'snoozed') ORDER BY id ASC LIMIT 5"
                ).fetchall()
                for row in task_rows:
                    self.create_open_loop(
                        title=row["title"],
                        details="Task pending",
                        source="task",
                        related_type="task",
                        related_id=row["id"],
                    )
                for row in reminder_rows:
                    self.create_open_loop(
                        title=row["title"],
                        details="Reminder pending",
                        due_at=datetime.fromisoformat(row["due_at"]) if row["due_at"] else None,
                        source="reminder",
                        related_type="reminder",
                        related_id=row["id"],
                    )

            if not self.list_facts(limit=1):
                self.upsert_fact("preferred_workspace", "Desktop setup", source="seed", confidence=0.6)
        self.connection.commit()

    def list_local_tasks(self) -> list[TaskSummary]:
        rows = self.connection.execute(
            """
            SELECT id, title, priority, due_at
            FROM local_tasks
            WHERE completed = 0
            ORDER BY priority DESC, COALESCE(due_at, '9999-12-31T00:00:00')
            LIMIT 10
            """
            ,
        ).fetchall()
        tasks: list[TaskSummary] = []
        for row in rows:
            due_at = datetime.fromisoformat(row["due_at"]) if row["due_at"] else None
            tasks.append(TaskSummary(id=row["id"], title=row["title"], priority=row["priority"], due_at=due_at))
        return tasks

    def list_local_events(self) -> list[CalendarEventSummary]:
        today = datetime.now().date().isoformat()
        rows = self.connection.execute(
            """
            SELECT title, starts_at, ends_at, importance
            FROM local_calendar_events
            WHERE date(starts_at) = date(?)
            ORDER BY starts_at ASC
            LIMIT 10
            """,
            (today,),
        ).fetchall()
        events: list[CalendarEventSummary] = []
        for row in rows:
            ends_at = datetime.fromisoformat(row["ends_at"]) if row["ends_at"] else None
            events.append(
                CalendarEventSummary(
                    title=row["title"],
                    starts_at=datetime.fromisoformat(row["starts_at"]),
                    ends_at=ends_at,
                    importance=row["importance"],
                )
            )
        return events

    def delete_local_event(self, event_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM local_calendar_events WHERE id = ?",
            (event_id,),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def create_local_task(self, title: str, priority: int = 1) -> int:
        cursor = self.connection.execute(
            "INSERT INTO local_tasks (title, priority) VALUES (?, ?)",
            (title, priority),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def complete_local_task_by_title(self, title: str) -> int | None:
        row = self.connection.execute(
            """
            SELECT id
            FROM local_tasks
            WHERE completed = 0 AND lower(title) = lower(?)
            ORDER BY priority DESC, id ASC
            LIMIT 1
            """,
            (title,),
        ).fetchone()
        if not row:
            return None
        self.connection.execute("UPDATE local_tasks SET completed = 1 WHERE id = ?", (row["id"],))
        self.connection.commit()
        return int(row["id"])

    def complete_local_task(self, title: str) -> bool:
        return self.complete_local_task_by_title(title) is not None

    def create_reminder(self, title: str, due_at: datetime, kind: str = "reminder") -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO local_reminders (title, due_at, status, kind, created_at)
            VALUES (?, ?, 'pending', ?, ?)
            """,
            (title, due_at.isoformat(), kind, datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_due_reminders(self, now: datetime | None = None) -> list[ReminderSummary]:
        now = now or datetime.now()
        rows = self.connection.execute(
            """
            SELECT id, title, due_at, status, kind
            FROM local_reminders
            WHERE status IN ('pending', 'snoozed') AND due_at <= ?
            ORDER BY due_at ASC, id ASC
            LIMIT 10
            """,
            (now.isoformat(),),
        ).fetchall()
        return [
            ReminderSummary(
                id=row["id"],
                title=row["title"],
                due_at=datetime.fromisoformat(row["due_at"]),
                status=row["status"],
                kind=row["kind"],
            )
            for row in rows
        ]

    def list_pending_reminders(self, limit: int = 10) -> list[ReminderSummary]:
        rows = self.connection.execute(
            """
            SELECT id, title, due_at, status, kind
            FROM local_reminders
            WHERE status IN ('pending', 'announced', 'snoozed')
            ORDER BY due_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            ReminderSummary(
                id=row["id"],
                title=row["title"],
                due_at=datetime.fromisoformat(row["due_at"]),
                status=row["status"],
                kind=row["kind"],
            )
            for row in rows
        ]

    def update_reminder_status(self, reminder_id: int, status: str, due_at: datetime | None = None) -> None:
        if due_at is None:
            self.connection.execute(
                "UPDATE local_reminders SET status = ? WHERE id = ?",
                (status, reminder_id),
            )
        else:
            self.connection.execute(
                "UPDATE local_reminders SET status = ?, due_at = ? WHERE id = ?",
                (status, due_at.isoformat(), reminder_id),
            )
        self.connection.commit()

    def upsert_fact(self, key: str, value: str, source: str = "user", confidence: float = 1.0) -> None:
        self.remember_memory_item(
            key,
            value,
            memory_kind=self._memory_kind_for_key(key, source),
            source=source,
            confidence=confidence,
        )

    def list_facts(self, limit: int = 20) -> list[ContextFact]:
        if self._structured_memory_supported():
            rows = self.connection.execute(
                """
                SELECT key, value, source, confidence, memory_kind, entity_key, status, provenance_json, updated_at
                FROM structured_memory_items
                WHERE profile_slug = ? AND status = 'active'
                ORDER BY
                    CASE memory_kind
                        WHEN 'preference' THEN 0
                        WHEN 'commitment' THEN 1
                        WHEN 'decision' THEN 2
                        WHEN 'fact' THEN 3
                        WHEN 'episodic_summary' THEN 4
                        ELSE 5
                    END,
                    updated_at DESC,
                    id DESC
                LIMIT ?
                """,
                (self.profile_slug, limit),
            ).fetchall()
            return [self._row_to_context_fact(row) for row in rows]
        rows = self.connection.execute(
            """
            SELECT key, value, source, confidence, updated_at
            FROM memory_entries
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            ContextFact(
                key=row["key"],
                value=row["value"],
                source=row["source"],
                confidence=row["confidence"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def search_facts(self, query: str, limit: int = 5) -> list[ContextFact]:
        if self._structured_memory_supported():
            rows = self.connection.execute(
                """
                SELECT key, value, source, confidence, memory_kind, entity_key, status, provenance_json, updated_at
                FROM structured_memory_items
                WHERE profile_slug = ? AND status = 'active'
                ORDER BY updated_at DESC, id DESC
                LIMIT 300
                """,
                (self.profile_slug,),
            ).fetchall()
            query_text = query.strip().lower()
            query_terms = [token for token in re.findall(r"[a-z0-9_]{2,}", query_text)]
            wants_preferences = any(token in query_text for token in ("preference", "preferences", "editor", "style", "about me"))
            wants_commitments = any(token in query_text for token in ("commitment", "todo", "follow up", "need to", "next step"))
            wants_decisions = any(token in query_text for token in ("decision", "decided", "agreed"))
            scored: list[tuple[float, ContextFact]] = []
            for row in rows:
                fact = self._row_to_context_fact(row)
                haystack = " ".join(
                    [
                        fact.key,
                        fact.value,
                        fact.memory_kind,
                        fact.entity_key or "",
                        json.dumps(fact.provenance, sort_keys=True),
                    ]
                ).lower()
                score = 0.0
                if query_text and query_text in haystack:
                    score += 3.0
                score += sum(1.2 for term in query_terms if term in haystack)
                if wants_preferences and fact.memory_kind == "preference":
                    score += 2.2
                if wants_commitments and fact.memory_kind == "commitment":
                    score += 1.8
                if wants_decisions and fact.memory_kind == "decision":
                    score += 1.8
                if not query_terms and fact.memory_kind == "preference":
                    score += 1.0
                score += min(max(fact.confidence, 0.0), 1.0) * 0.4
                if score > 0:
                    scored.append((score, fact))
            scored.sort(
                key=lambda item: (
                    -item[0],
                    -(item[1].updated_at.timestamp() if item[1].updated_at else 0.0),
                    item[1].key,
                )
            )
            return [fact for _, fact in scored[:limit]]
        like = f"%{query.lower()}%"
        rows = self.connection.execute(
            """
            SELECT key, value, source, confidence, updated_at
            FROM memory_entries
            WHERE lower(key) LIKE ? OR lower(value) LIKE ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (like, like, limit),
        ).fetchall()
        return [
            ContextFact(
                key=row["key"],
                value=row["value"],
                source=row["source"],
                confidence=row["confidence"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def list_all_facts(self) -> list[ContextFact]:
        if self._structured_memory_supported():
            rows = self.connection.execute(
                """
                SELECT key, value, source, confidence, memory_kind, entity_key, status, provenance_json, updated_at
                FROM structured_memory_items
                WHERE profile_slug = ? AND status = 'active'
                ORDER BY updated_at DESC, id DESC
                """,
                (self.profile_slug,),
            ).fetchall()
            return [self._row_to_context_fact(row) for row in rows]
        rows = self.connection.execute(
            """
            SELECT key, value, source, confidence, updated_at
            FROM memory_entries
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
        return [
            ContextFact(
                key=row["key"],
                value=row["value"],
                source=row["source"],
                confidence=row["confidence"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def create_open_loop(
        self,
        title: str,
        details: str | None = None,
        due_at: datetime | None = None,
        source: str = "assistant",
        related_type: str | None = None,
        related_id: int | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO open_loops (title, details, status, due_at, source, related_type, related_id, created_at, updated_at)
            VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                details,
                due_at.isoformat() if due_at else None,
                source,
                related_type,
                related_id,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def update_open_loop_status(self, loop_id: int, status: str) -> None:
        self.connection.execute(
            "UPDATE open_loops SET status = ?, updated_at = ? WHERE id = ?",
            (status, datetime.now(timezone.utc).isoformat(), loop_id),
        )
        self.connection.commit()

    def resolve_open_loop_by_relation(self, related_type: str, related_id: int, status: str = "resolved") -> None:
        self.connection.execute(
            """
            UPDATE open_loops
            SET status = ?, updated_at = ?
            WHERE related_type = ? AND related_id = ? AND status = 'open'
            """,
            (status, datetime.now(timezone.utc).isoformat(), related_type, related_id),
        )
        self.connection.commit()

    def list_open_loops(self, status: str = "open", limit: int = 10) -> list[OpenLoop]:
        rows = self.connection.execute(
            """
            SELECT id, title, details, status, due_at, source, related_type, related_id, updated_at
            FROM open_loops
            WHERE status = ?
            ORDER BY COALESCE(due_at, '9999-12-31T00:00:00'), id ASC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
        loops: list[OpenLoop] = []
        for row in rows:
            loops.append(
                OpenLoop(
                    id=row["id"],
                    title=row["title"],
                    details=row["details"],
                    status=row["status"],
                    due_at=datetime.fromisoformat(row["due_at"]) if row["due_at"] else None,
                    source=row["source"],
                    related_type=row["related_type"],
                    related_id=row["related_id"],
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
            )
        return loops

    def append_execution_receipt(self, receipt: ExecutionReceipt) -> None:
        payload = dict(receipt.data)
        payload["_receipt_meta"] = {
            "original_utterance": receipt.original_utterance,
            "trigger_source": receipt.trigger_source,
            "verification_source": receipt.verification_source,
        }
        self.connection.execute(
            """
            INSERT INTO execution_receipts (
                created_at,
                capability_name,
                status,
                message,
                evidence_json,
                side_effects_json,
                suggested_follow_up,
                data_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.timestamp.isoformat(),
                receipt.capability_name,
                receipt.status,
                receipt.message,
                json.dumps(receipt.evidence),
                json.dumps(receipt.side_effects),
                receipt.suggested_follow_up,
                json.dumps(payload),
            ),
        )
        self._trim_table("execution_receipts", self.RECEIPT_RETENTION)
        self.connection.commit()

    def list_execution_receipts(self, limit: int = 20) -> list[ExecutionReceipt]:
        rows = self.connection.execute(
            """
            SELECT created_at, capability_name, status, message, evidence_json, side_effects_json, suggested_follow_up, data_json
            FROM execution_receipts
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        receipts: list[ExecutionReceipt] = []
        for row in rows:
            payload = json.loads(row["data_json"])
            meta = payload.pop("_receipt_meta", {})
            receipts.append(
                ExecutionReceipt(
                    timestamp=datetime.fromisoformat(row["created_at"]),
                    capability_name=row["capability_name"],
                    status=row["status"],
                    message=row["message"],
                    original_utterance=str(meta.get("original_utterance", "")),
                    trigger_source=str(meta.get("trigger_source", "manual_ui")),
                    verification_source=str(meta.get("verification_source", "none")),
                    evidence=json.loads(row["evidence_json"]),
                    side_effects=json.loads(row["side_effects_json"]),
                    suggested_follow_up=row["suggested_follow_up"],
                    data=payload,
                )
            )
        return receipts

    def set_active_context(self, bucket: str, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO active_context (bucket, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(bucket, key)
            DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (bucket, key, value, datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()

    def get_active_context(self, bucket: str, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM active_context WHERE bucket = ? AND key = ?",
            (bucket, key),
        ).fetchone()
        return row["value"] if row else default

    def list_active_context_bucket(self, bucket: str) -> dict[str, str]:
        rows = self.connection.execute(
            "SELECT key, value FROM active_context WHERE bucket = ?",
            (bucket,),
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def create_local_event(
        self,
        title: str,
        starts_at: datetime,
        ends_at: datetime | None = None,
        importance: int = 0,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO local_calendar_events (title, starts_at, ends_at, importance)
            VALUES (?, ?, ?, ?)
            """,
            (title, starts_at.isoformat(), ends_at.isoformat() if ends_at else None, importance),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def upsert_document_record(
        self,
        record: DocumentRecord,
        chunks: list[DocumentChunk] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        payload = dict(metadata or {})
        payload.setdefault("classification", record.classification)
        payload.setdefault("data_class", record.data_class)
        payload.setdefault("retention_state", record.retention_state)
        payload.setdefault("provenance", record.provenance)
        self.connection.execute(
            """
            INSERT INTO document_records (
                id, profile_slug, organization_id, workspace_id, actor_user_id, title, source, file_type, file_path, file_hash, category, tags_json, archived, metadata_json, created_at, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                profile_slug = excluded.profile_slug,
                organization_id = excluded.organization_id,
                workspace_id = excluded.workspace_id,
                actor_user_id = excluded.actor_user_id,
                title = excluded.title,
                source = excluded.source,
                file_type = excluded.file_type,
                file_path = excluded.file_path,
                file_hash = excluded.file_hash,
                category = excluded.category,
                tags_json = excluded.tags_json,
                archived = excluded.archived,
                metadata_json = excluded.metadata_json,
                imported_at = excluded.imported_at
            """,
            (
                record.id,
                record.profile_slug,
                record.organization_id,
                record.workspace_id,
                record.actor_user_id,
                record.title,
                record.source,
                record.file_type,
                record.file_path,
                record.file_hash,
                record.category,
                json.dumps(record.tags),
                1 if record.archived else 0,
                json.dumps(payload),
                record.created_at.isoformat(),
                record.imported_at.isoformat(),
            ),
        )
        self.connection.execute("DELETE FROM document_chunks WHERE document_id = ?", (record.id,))
        for chunk in chunks or []:
            self.connection.execute(
                """
                INSERT INTO document_chunks (document_id, chunk_index, text, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    chunk.chunk_index,
                    chunk.text,
                    json.dumps(chunk.metadata),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        self.connection.commit()

    def list_document_records(
        self,
        limit: int = 20,
        category: str | None = None,
        *,
        include_archived: bool = False,
    ) -> list[DocumentRecord]:
        archived_clause = "" if include_archived else "AND archived = 0"
        if category:
            rows = self.connection.execute(
                f"""
                SELECT id, profile_slug, organization_id, workspace_id, actor_user_id, title, source, file_type, file_path, file_hash, category, tags_json, archived, metadata_json, created_at, imported_at
                FROM document_records
                WHERE profile_slug = ? AND category = ? {archived_clause}
                ORDER BY imported_at DESC, id DESC
                LIMIT ?
                """,
                (self.profile_slug, category, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                f"""
                SELECT id, profile_slug, organization_id, workspace_id, actor_user_id, title, source, file_type, file_path, file_hash, category, tags_json, archived, metadata_json, created_at, imported_at
                FROM document_records
                WHERE profile_slug = ? {archived_clause}
                ORDER BY imported_at DESC, id DESC
                LIMIT ?
                """,
                (self.profile_slug, limit),
            ).fetchall()
        records: list[DocumentRecord] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            records.append(DocumentRecord(
                id=row["id"],
                profile_slug=row["profile_slug"],
                organization_id=row["organization_id"] if "organization_id" in row.keys() else None,
                workspace_id=row["workspace_id"] if "workspace_id" in row.keys() else None,
                actor_user_id=row["actor_user_id"] if "actor_user_id" in row.keys() else None,
                title=row["title"],
                source=row["source"],
                file_type=row["file_type"],
                file_path=row["file_path"],
                file_hash=row["file_hash"] if "file_hash" in row.keys() else None,
                category=row["category"],
                classification=str(metadata.get("classification") or "internal"),
                data_class=str(metadata.get("data_class") or "operational"),
                retention_state=metadata.get("retention_state"),
                metadata=metadata,
                provenance=dict(metadata.get("provenance") or {}),
                tags=json.loads(row["tags_json"]),
                archived=bool(row["archived"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                imported_at=datetime.fromisoformat(row["imported_at"]),
            ))
        return records

    def get_document_details(self, document_ids: list[str]) -> dict[str, dict[str, object]]:
        ids = [str(item) for item in document_ids if str(item).strip()]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"""
            SELECT id, title, file_type, file_path, category, metadata_json, imported_at
            FROM document_records
            WHERE profile_slug = ? AND id IN ({placeholders})
            """,
            (self.profile_slug, *ids),
        ).fetchall()
        details: dict[str, dict[str, object]] = {}
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            details[str(row["id"])] = {
                "id": row["id"],
                "title": row["title"],
                "file_type": row["file_type"],
                "file_path": row["file_path"],
                "category": row["category"],
                "imported_at": row["imported_at"],
                "ocr_used": bool(metadata.get("ocr_used", False)),
                "ocr_low_confidence": bool(metadata.get("ocr_low_confidence", False)),
                "ocr_confidence_avg": metadata.get("ocr_confidence_avg"),
            }
        return details

    def search_document_chunks(self, query: str, limit: int = 8, include_archived: bool = False) -> list[RetrievalHit]:
        archived_clause = "" if include_archived else "AND dr.archived = 0"
        rows = self.connection.execute(
            f"""
            SELECT dr.id AS document_id, dr.title, dr.source, dr.archived, dc.text, dc.chunk_index, dr.file_path, dr.metadata_json
            FROM document_chunks dc
            JOIN document_records dr ON dr.id = dc.document_id
            WHERE dr.profile_slug = ?
              {archived_clause}
            ORDER BY dr.imported_at DESC, dc.chunk_index ASC
            """,
            (self.profile_slug,),
        ).fetchall()
        query_terms = self._document_search_terms(query)
        lowered_query = query.lower().strip()
        scored_hits: list[RetrievalHit] = []
        for row in rows:
            title = str(row["title"] or "")
            text = str(row["text"] or "")
            document_metadata = json.loads(row["metadata_json"] or "{}")
            haystack = f"{title} {text}".lower()
            overlap = sum(1 for term in query_terms if term in haystack)
            if overlap <= 0:
                if lowered_query and lowered_query not in haystack:
                    continue
            score = 1.0 + min(overlap, 6) * 0.1
            scored_hits.append(
                RetrievalHit(
                    source_type="archive" if bool(row["archived"]) or str(row["source"]).startswith("archive:") else "document",
                    source_id=row["document_id"],
                    score=score,
                    text=text,
                    metadata={
                        "title": title,
                        "chunk_index": row["chunk_index"],
                        "file_path": row["file_path"],
                        "source": row["source"],
                        "classification": str(document_metadata.get("classification") or "internal"),
                        "ocr_used": bool(document_metadata.get("ocr_used", False)),
                        "ocr_engine": document_metadata.get("ocr_engine"),
                        "ocr_pages": int(document_metadata.get("ocr_pages", 0) or 0),
                        "ocr_page_indices": list(document_metadata.get("ocr_page_indices") or []),
                        "ocr_confidence_avg": document_metadata.get("ocr_confidence_avg"),
                        "ocr_low_confidence": bool(document_metadata.get("ocr_low_confidence", False)),
                        "ocr_mode": document_metadata.get("ocr_mode"),
                    },
                )
            )
        scored_hits.sort(key=lambda item: (-item.score, item.metadata.get("title", ""), item.metadata.get("chunk_index", 0)))
        return scored_hits[:limit]

    def _document_search_terms(self, query: str) -> list[str]:
        tokens = [token.lower() for token in re.findall(r"[0-9A-Za-zÃ„Ã–ÃœÃ¤Ã¶Ã¼ÃŸ_-]+", query)]
        synonyms = {
            "angebot": ("offer",),
            "rechnung": ("invoice",),
            "zielbetrag": ("target", "amount"),
            "betrag": ("amount", "total"),
            "summe": ("amount", "total"),
            "faellig": ("due",),
            "fÃ¤llig": ("due",),
            "faelligkeit": ("due", "date"),
            "fÃ¤lligkeit": ("due", "date"),
            "ust": ("vat",),
            "ustid": ("vat", "id"),
            "ustidnr": ("vat", "id"),
        }
        stopwords = {
            "the",
            "and",
            "for",
            "with",
            "what",
            "when",
            "from",
            "into",
            "that",
            "this",
            "only",
            "your",
            "user",
            "reply",
            "short",
            "using",
            "available",
            "context",
            "does",
            "contain",
            "hochgeladenen",
            "hochgeladen",
            "dokumenten",
            "dokumente",
            "dokument",
            "quellen",
            "quelle",
            "eckigen",
            "klammern",
            "antworte",
            "nenne",
            "kurz",
            "worum",
            "wann",
            "wie",
            "was",
            "aus",
            "den",
            "dem",
            "der",
            "die",
            "das",
            "und",
            "ist",
            "im",
            "in",
        }
        terms: list[str] = []
        for token in tokens:
            normalized = token.strip().lower()
            if len(normalized) < 3 or normalized in stopwords:
                continue
            if normalized not in terms:
                terms.append(normalized)
            for synonym in synonyms.get(normalized, ()):
                if synonym not in terms:
                    terms.append(synonym)
        return terms

    def list_all_document_chunks(self, include_archived: bool = True) -> list[dict[str, object]]:
        archived_clause = "" if include_archived else "AND dr.archived = 0"
        rows = self.connection.execute(
            f"""
            SELECT dr.id AS document_id, dr.title, dr.file_path, dr.archived, dr.metadata_json, dc.chunk_index, dc.text
            FROM document_chunks dc
            JOIN document_records dr ON dr.id = dc.document_id
            WHERE dr.profile_slug = ?
              {archived_clause}
            ORDER BY dr.imported_at DESC, dc.chunk_index ASC
            """,
            (self.profile_slug,),
        ).fetchall()
        return [
            {
                "source_type": "archive" if bool(row["archived"]) else "document",
                "source_id": row["document_id"],
                "title": row["title"],
                "file_path": row["file_path"],
                "classification": str(json.loads(row["metadata_json"] or "{}").get("classification") or "internal"),
                "ocr_used": bool(json.loads(row["metadata_json"] or "{}").get("ocr_used", False)),
                "ocr_low_confidence": bool(json.loads(row["metadata_json"] or "{}").get("ocr_low_confidence", False)),
                "ocr_confidence_avg": json.loads(row["metadata_json"] or "{}").get("ocr_confidence_avg"),
                "chunk_index": row["chunk_index"],
                "text": row["text"],
            }
            for row in rows
        ]

    def list_document_chunks_for_documents(
        self,
        document_ids: list[str],
        *,
        include_archived: bool = True,
    ) -> list[dict[str, object]]:
        if not document_ids:
            return []
        archived_clause = "" if include_archived else "AND dr.archived = 0"
        placeholders = _placeholders(len(document_ids))
        rows = self.connection.execute(
            f"""
            SELECT dr.id AS document_id, dr.title, dr.file_path, dr.archived, dr.metadata_json, dc.chunk_index, dc.text
            FROM document_chunks dc
            JOIN document_records dr ON dr.id = dc.document_id
            WHERE dr.profile_slug = ?
              AND dr.id IN ({placeholders})
              {archived_clause}
            ORDER BY dr.imported_at DESC, dc.chunk_index ASC
            """,
            (self.profile_slug, *document_ids),
        ).fetchall()
        return [
            {
                "source_type": "archive" if bool(row["archived"]) else "document",
                "source_id": row["document_id"],
                "title": row["title"],
                "file_path": row["file_path"],
                "classification": str(json.loads(row["metadata_json"] or "{}").get("classification") or "internal"),
                "ocr_used": bool(json.loads(row["metadata_json"] or "{}").get("ocr_used", False)),
                "ocr_low_confidence": bool(json.loads(row["metadata_json"] or "{}").get("ocr_low_confidence", False)),
                "ocr_confidence_avg": json.loads(row["metadata_json"] or "{}").get("ocr_confidence_avg"),
                "chunk_index": row["chunk_index"],
                "text": row["text"],
            }
            for row in rows
        ]

    def summarize_document_classifications(self) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT metadata_json
            FROM document_records
            WHERE profile_slug = ?
            """,
            (self.profile_slug,),
        ).fetchall()
        summary: dict[str, int] = {}
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            classification = str(metadata.get("classification") or "internal")
            summary[classification] = summary.get(classification, 0) + 1
        return summary

    def count_document_records(self, *, include_archived: bool = False) -> int:
        archived_clause = "" if include_archived else "AND archived = 0"
        row = self.connection.execute(
            f"SELECT COUNT(*) AS count FROM document_records WHERE profile_slug = ? {archived_clause}",
            (self.profile_slug,),
        ).fetchone()
        return int(row["count"] or 0) if row else 0

    def set_documents_archived(self, document_ids: list[str], archived: bool = True) -> int:
        ids = [str(item) for item in document_ids if str(item).strip()]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = self.connection.execute(
            f"""
            UPDATE document_records
            SET archived = ?
            WHERE profile_slug = ?
              AND id IN ({placeholders})
            """,
            (1 if archived else 0, self.profile_slug, *ids),
        )
        self.connection.commit()
        return int(cursor.rowcount or 0)

    def count_meetings(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM meeting_records WHERE profile_slug = ?",
            (self.profile_slug,),
        ).fetchone()
        return int(row["count"] or 0) if row else 0

    def count_business_documents(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM german_business_documents WHERE profile_slug = ?",
            (self.profile_slug,),
        ).fetchone()
        return int(row["count"] or 0) if row else 0

    def count_sync_targets(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM sync_targets WHERE profile_slug = ?",
            (self.profile_slug,),
        ).fetchone()
        return int(row["count"] or 0) if row else 0

    def prompt_cache_revision(self) -> str:
        snapshot = {
            "structured_memory": [
                dict(row)
                for row in self.connection.execute(
                    """
                    SELECT key, value, memory_kind, source, status, updated_at
                    FROM structured_memory_items
                    WHERE profile_slug = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 100
                    """,
                    (self.profile_slug,),
                ).fetchall()
            ]
            if self._structured_memory_supported()
            else [],
            "memory_entries": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT key, value, source, updated_at FROM memory_entries ORDER BY updated_at DESC, id DESC LIMIT 50"
                ).fetchall()
            ],
            "open_loops": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT title, status, due_at, updated_at FROM open_loops ORDER BY updated_at DESC, id DESC LIMIT 50"
                ).fetchall()
            ],
            "reminders": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT title, due_at, status, created_at FROM local_reminders ORDER BY created_at DESC, id DESC LIMIT 50"
                ).fetchall()
            ],
            "tasks": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT title, due_at, completed, priority FROM local_tasks ORDER BY id DESC LIMIT 50"
                ).fetchall()
            ],
            "events": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT title, starts_at, ends_at, importance FROM local_calendar_events ORDER BY id DESC LIMIT 50"
                ).fetchall()
            ],
            "documents": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT id, imported_at FROM document_records ORDER BY imported_at DESC LIMIT 50"
                ).fetchall()
            ],
            "meetings": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT id, created_at FROM meeting_records ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
            ],
            "business_docs": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT id, updated_at FROM german_business_documents ORDER BY updated_at DESC LIMIT 50"
                ).fetchall()
            ],
            "conversation": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT id, created_at FROM conversation_log ORDER BY created_at DESC, id DESC LIMIT 50"
                ).fetchall()
            ],
        }
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def upsert_conversation_archive(
        self,
        record: ConversationArchiveRecord,
        chunks: list[DocumentChunk] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO conversation_archives (id, profile_slug, source, title, file_path, imported_turns, metadata_json, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, '{}', ?)
            ON CONFLICT(id) DO UPDATE SET
                profile_slug = excluded.profile_slug,
                source = excluded.source,
                title = excluded.title,
                file_path = excluded.file_path,
                imported_turns = excluded.imported_turns,
                archived_at = excluded.archived_at
            """,
            (
                record.id,
                record.profile_slug,
                record.source,
                record.title,
                record.file_path,
                record.imported_turns,
                record.archived_at.isoformat(),
            ),
        )
        archive_document = DocumentRecord(
            id=f"archive:{record.id}",
            profile_slug=record.profile_slug,
            title=record.title,
            source=f"archive:{record.source}",
            file_type="json",
            file_path=record.file_path,
            category="conversation_archive",
            tags=["archive", record.source],
            archived=True,
            created_at=record.archived_at,
            imported_at=record.archived_at,
        )
        self.upsert_document_record(archive_document, chunks=chunks, metadata={"archive_id": record.id})
        self.connection.commit()

    def list_conversation_archives(self, limit: int = 20) -> list[ConversationArchiveRecord]:
        rows = self.connection.execute(
            """
            SELECT id, profile_slug, source, title, file_path, imported_turns, archived_at
            FROM conversation_archives
            WHERE profile_slug = ?
            ORDER BY archived_at DESC, id DESC
            LIMIT ?
            """,
            (self.profile_slug, limit),
        ).fetchall()
        return [
            ConversationArchiveRecord(
                id=row["id"],
                profile_slug=row["profile_slug"],
                source=row["source"],
                title=row["title"],
                file_path=row["file_path"],
                imported_turns=row["imported_turns"],
                archived_at=datetime.fromisoformat(row["archived_at"]),
            )
            for row in rows
        ]

    def list_archive_chunks(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT ca.id AS archive_id, ca.title, dc.chunk_index, dc.text
            FROM document_chunks dc
            JOIN conversation_archives ca ON dc.document_id = 'archive:' || ca.id
            WHERE ca.profile_slug = ?
            ORDER BY ca.archived_at DESC, dc.chunk_index ASC
            """,
            (self.profile_slug,),
        ).fetchall()
        return [
            {
                "source_type": "archive",
                "source_id": f"archive:{row['archive_id']}",
                "title": row["title"],
                "chunk_index": row["chunk_index"],
                "text": row["text"],
            }
            for row in rows
        ]

    def upsert_meeting_record(
        self,
        record: MeetingRecord,
        transcript_path: str | None = None,
        status: str = "recorded",
        metadata: dict[str, object] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            INSERT INTO meeting_records (id, profile_slug, title, audio_path, transcript_path, status, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                profile_slug = excluded.profile_slug,
                title = excluded.title,
                audio_path = excluded.audio_path,
                transcript_path = excluded.transcript_path,
                status = excluded.status,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                record.id,
                record.profile_slug,
                record.title,
                record.audio_path,
                transcript_path,
                status,
                json.dumps(metadata or {}),
                record.created_at.isoformat(),
                now,
            ),
        )
        self.connection.commit()

    def list_meeting_records(self, limit: int = 20) -> list[MeetingRecord]:
        rows = self.connection.execute(
            """
            SELECT id, profile_slug, title, audio_path, transcript_path, status, created_at
            FROM meeting_records
            WHERE profile_slug = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (self.profile_slug, limit),
        ).fetchall()
        return [
            MeetingRecord(
                id=row["id"],
                profile_slug=row["profile_slug"],
                title=row["title"],
                audio_path=row["audio_path"],
                transcript_path=row["transcript_path"],
                status=row["status"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def append_transcript_artifact(
        self,
        meeting_id: str,
        artifact_type: str,
        content: str = "",
        file_path: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO transcript_artifacts (meeting_id, profile_slug, artifact_type, file_path, content, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                self.profile_slug,
                artifact_type,
                file_path,
                content,
                json.dumps(metadata or {}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._trim_table("transcript_artifacts", self.ARTIFACT_RETENTION)
        self.connection.commit()

    def list_transcript_artifacts(self, meeting_id: str) -> list[TranscriptArtifact]:
        rows = self.connection.execute(
            """
            SELECT meeting_id, file_path, content, metadata_json, artifact_type, created_at
            FROM transcript_artifacts
            WHERE meeting_id = ? AND profile_slug = ?
            ORDER BY created_at DESC, id DESC
            """,
            (meeting_id, self.profile_slug),
        ).fetchall()
        return [
            TranscriptArtifact(
                meeting_id=row["meeting_id"],
                transcript_path=row["file_path"] or "",
                artifact_type=row["artifact_type"],
                content=row["content"] or "",
                metadata=json.loads(row["metadata_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def list_transcript_action_items(self, meeting_id: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT id, title, details, due_hint, created_at, review_state, related_task_id, related_reminder_id
            FROM transcript_action_items
            WHERE meeting_id = ? AND profile_slug = ?
            ORDER BY id ASC
            """,
            (meeting_id, self.profile_slug),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "details": row["details"],
                "due_hint": row["due_hint"],
                "created_at": row["created_at"],
                "review_state": row["review_state"] if "review_state" in row.keys() else "pending",
                "related_task_id": row["related_task_id"] if "related_task_id" in row.keys() else None,
                "related_reminder_id": row["related_reminder_id"] if "related_reminder_id" in row.keys() else None,
            }
            for row in rows
        ]

    def update_transcript_action_item_review(
        self,
        item_id: int,
        review_state: str,
        *,
        related_task_id: int | None = None,
        related_reminder_id: int | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE transcript_action_items
            SET review_state = ?, related_task_id = ?, related_reminder_id = ?
            WHERE id = ? AND profile_slug = ?
            """,
            (review_state, related_task_id, related_reminder_id, item_id, self.profile_slug),
        )
        self.connection.commit()

    def upsert_business_document(self, document: GermanBusinessDocument, metadata: dict[str, object] | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            INSERT INTO german_business_documents (id, profile_slug, kind, title, status, file_path, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                profile_slug = excluded.profile_slug,
                kind = excluded.kind,
                title = excluded.title,
                status = excluded.status,
                file_path = excluded.file_path,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                document.id,
                document.profile_slug,
                document.kind,
                document.title,
                document.status,
                document.file_path,
                json.dumps(metadata or {}),
                now,
                now,
            ),
        )
        self.connection.commit()

    def list_business_documents(self, limit: int = 20) -> list[GermanBusinessDocument]:
        rows = self.connection.execute(
            """
            SELECT id, profile_slug, kind, title, status, file_path, metadata_json
            FROM german_business_documents
            WHERE profile_slug = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (self.profile_slug, limit),
        ).fetchall()
        return [
            GermanBusinessDocument(
                id=row["id"],
                profile_slug=row["profile_slug"],
                kind=row["kind"],
                title=row["title"],
                status=row["status"],
                file_path=row["file_path"],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    def list_structured_memory_items(
        self,
        *,
        organization_id: str | None = None,
        workspace_slug: str | None = None,
        user_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        if not self._structured_memory_supported():
            return []
        query = """
            SELECT id, profile_slug, organization_id, workspace_slug, user_id, memory_kind, key, value, entity_key,
                   source, confidence, status, provenance_json, data_class, promotion_state, approved_count,
                   rejected_count, last_feedback_at, created_at, updated_at
            FROM structured_memory_items
            WHERE profile_slug = ? AND status = 'active'
        """
        params: list[object] = [self.profile_slug]
        if organization_id is not None and self._has_column("structured_memory_items", "organization_id"):
            query += " AND COALESCE(organization_id, '') = COALESCE(?, '')"
            params.append(organization_id)
        if workspace_slug is not None and self._has_column("structured_memory_items", "workspace_slug"):
            query += " AND COALESCE(workspace_slug, '') IN ('', COALESCE(?, ''))"
            params.append(workspace_slug)
        if user_id is not None and self._has_column("structured_memory_items", "user_id"):
            query += " AND COALESCE(user_id, '') IN ('', COALESCE(?, ''))"
            params.append(user_id)
        query += " ORDER BY approved_count DESC, confidence DESC, updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, tuple(params)).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            items.append(
                {
                    "id": str(row["id"]),
                    "profile_slug": str(row["profile_slug"]),
                    "organization_id": row["organization_id"] if "organization_id" in row.keys() else None,
                    "workspace_slug": row["workspace_slug"] if "workspace_slug" in row.keys() else None,
                    "user_id": row["user_id"] if "user_id" in row.keys() else None,
                    "memory_kind": str(row["memory_kind"]),
                    "key": str(row["key"]),
                    "value": str(row["value"]),
                    "entity_key": row["entity_key"],
                    "source": str(row["source"]),
                    "confidence": float(row["confidence"]),
                    "status": str(row["status"]),
                    "provenance": json.loads(row["provenance_json"] or "{}"),
                    "data_class": str(row["data_class"] or "operational"),
                    "promotion_state": str(row["promotion_state"] or "none"),
                    "approved_count": int(row["approved_count"] or 0),
                    "rejected_count": int(row["rejected_count"] or 0),
                    "last_feedback_at": row["last_feedback_at"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return items

    def get_structured_memory_item(self, memory_item_id: str) -> dict[str, object] | None:
        items = self.connection.execute(
            """
            SELECT id, profile_slug, organization_id, workspace_slug, user_id, memory_kind, key, value, entity_key,
                   source, confidence, status, provenance_json, data_class, promotion_state, approved_count,
                   rejected_count, last_feedback_at, created_at, updated_at
            FROM structured_memory_items
            WHERE id = ? AND profile_slug = ?
            LIMIT 1
            """,
            (memory_item_id, self.profile_slug),
        ).fetchall()
        if not items:
            return None
        row = items[0]
        return {
            "id": str(row["id"]),
            "profile_slug": str(row["profile_slug"]),
            "organization_id": row["organization_id"] if "organization_id" in row.keys() else None,
            "workspace_slug": row["workspace_slug"] if "workspace_slug" in row.keys() else None,
            "user_id": row["user_id"] if "user_id" in row.keys() else None,
            "memory_kind": str(row["memory_kind"]),
            "key": str(row["key"]),
            "value": str(row["value"]),
            "entity_key": row["entity_key"],
            "source": str(row["source"]),
            "confidence": float(row["confidence"]),
            "status": str(row["status"]),
            "provenance": json.loads(row["provenance_json"] or "{}"),
            "data_class": str(row["data_class"] or "operational"),
            "promotion_state": str(row["promotion_state"] or "none"),
            "approved_count": int(row["approved_count"] or 0),
            "rejected_count": int(row["rejected_count"] or 0),
            "last_feedback_at": row["last_feedback_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def update_structured_memory_feedback(
        self,
        memory_item_id: str,
        *,
        approved_delta: int = 0,
        rejected_delta: int = 0,
        promotion_state: str | None = None,
    ) -> None:
        if not self._structured_memory_supported():
            return
        if not self._has_column("structured_memory_items", "approved_count"):
            return
        updates = [
            "approved_count = MAX(0, COALESCE(approved_count, 0) + ?)",
            "rejected_count = MAX(0, COALESCE(rejected_count, 0) + ?)",
            "last_feedback_at = ?",
        ]
        params: list[object] = [approved_delta, rejected_delta, datetime.now(timezone.utc).isoformat()]
        if promotion_state is not None and self._has_column("structured_memory_items", "promotion_state"):
            updates.append("promotion_state = ?")
            params.append(promotion_state)
        params.extend([memory_item_id, self.profile_slug])
        self.connection.execute(
            f"UPDATE structured_memory_items SET {', '.join(updates)} WHERE id = ? AND profile_slug = ?",
            tuple(params),
        )
        self.connection.commit()

    def record_feedback_signal(self, signal: FeedbackSignal) -> FeedbackSignal:
        now = signal.created_at.isoformat()
        self.connection.execute(
            """
            INSERT INTO memory_feedback_signals (
                id, profile_slug, organization_id, workspace_slug, user_id, signal_type, source_type, source_id,
                memory_item_id, approved_for_training, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                metadata_json = excluded.metadata_json,
                approved_for_training = excluded.approved_for_training
            """,
            (
                signal.id,
                signal.profile_slug,
                signal.organization_id,
                signal.workspace_slug,
                signal.user_id,
                signal.signal_type,
                signal.source_type,
                signal.source_id,
                signal.memory_item_id,
                1 if signal.approved_for_training else 0,
                json.dumps(signal.metadata),
                now,
            ),
        )
        self.connection.commit()
        return signal

    def list_feedback_signals(
        self,
        *,
        workspace_slug: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackSignal]:
        query = """
            SELECT id, profile_slug, organization_id, workspace_slug, user_id, signal_type, source_type, source_id,
                   memory_item_id, approved_for_training, metadata_json, created_at
            FROM memory_feedback_signals
            WHERE profile_slug = ?
        """
        params: list[object] = [self.profile_slug]
        if workspace_slug is not None:
            query += " AND COALESCE(workspace_slug, '') = COALESCE(?, '')"
            params.append(workspace_slug)
        if user_id is not None:
            query += " AND COALESCE(user_id, '') = COALESCE(?, '')"
            params.append(user_id)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, tuple(params)).fetchall()
        return [
            FeedbackSignal(
                id=str(row["id"]),
                profile_slug=str(row["profile_slug"]),
                organization_id=row["organization_id"],
                workspace_slug=row["workspace_slug"],
                user_id=row["user_id"],
                signal_type=str(row["signal_type"]),
                source_type=str(row["source_type"]),
                source_id=str(row["source_id"]),
                memory_item_id=row["memory_item_id"],
                approved_for_training=bool(row["approved_for_training"]),
                metadata=json.loads(row["metadata_json"] or "{}"),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def upsert_training_example(self, record: TrainingExampleRecord) -> TrainingExampleRecord:
        now = record.updated_at.isoformat()
        self.connection.execute(
            """
            INSERT INTO training_examples (
                id, profile_slug, organization_id, workspace_slug, user_id, source_type, source_id, input_text,
                output_text, status, approved_for_training, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                input_text = excluded.input_text,
                output_text = excluded.output_text,
                status = excluded.status,
                approved_for_training = excluded.approved_for_training,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                record.id,
                record.profile_slug,
                record.organization_id,
                record.workspace_slug,
                record.user_id,
                record.source_type,
                record.source_id,
                record.input_text,
                record.output_text,
                record.status,
                1 if record.approved_for_training else 0,
                json.dumps(record.metadata),
                record.created_at.isoformat(),
                now,
            ),
        )
        self.connection.commit()
        return record

    def list_training_examples(
        self,
        *,
        workspace_slug: str | None = None,
        user_id: str | None = None,
        approved_only: bool = False,
        limit: int = 200,
    ) -> list[TrainingExampleRecord]:
        query = """
            SELECT id, profile_slug, organization_id, workspace_slug, user_id, source_type, source_id, input_text,
                   output_text, status, approved_for_training, metadata_json, created_at, updated_at
            FROM training_examples
            WHERE profile_slug = ?
        """
        params: list[object] = [self.profile_slug]
        if workspace_slug is not None:
            query += " AND COALESCE(workspace_slug, '') = COALESCE(?, '')"
            params.append(workspace_slug)
        if user_id is not None:
            query += " AND COALESCE(user_id, '') = COALESCE(?, '')"
            params.append(user_id)
        if approved_only:
            query += " AND approved_for_training = 1 AND status = 'approved'"
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, tuple(params)).fetchall()
        return [
            TrainingExampleRecord(
                id=str(row["id"]),
                profile_slug=str(row["profile_slug"]),
                organization_id=row["organization_id"],
                workspace_slug=row["workspace_slug"],
                user_id=row["user_id"],
                source_type=str(row["source_type"]),
                source_id=str(row["source_id"]),
                input_text=str(row["input_text"]),
                output_text=str(row["output_text"]),
                status=str(row["status"]),
                approved_for_training=bool(row["approved_for_training"]),
                metadata=json.loads(row["metadata_json"] or "{}"),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def get_training_example(self, example_id: str) -> TrainingExampleRecord | None:
        row = self.connection.execute(
            """
            SELECT id, profile_slug, organization_id, workspace_slug, user_id, source_type, source_id, input_text,
                   output_text, status, approved_for_training, metadata_json, created_at, updated_at
            FROM training_examples
            WHERE id = ? AND profile_slug = ?
            LIMIT 1
            """,
            (example_id, self.profile_slug),
        ).fetchone()
        if not row:
            return None
        return TrainingExampleRecord(
            id=str(row["id"]),
            profile_slug=str(row["profile_slug"]),
            organization_id=row["organization_id"],
            workspace_slug=row["workspace_slug"],
            user_id=row["user_id"],
            source_type=str(row["source_type"]),
            source_id=str(row["source_id"]),
            input_text=str(row["input_text"]),
            output_text=str(row["output_text"]),
            status=str(row["status"]),
            approved_for_training=bool(row["approved_for_training"]),
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def update_training_example_status(
        self,
        example_id: str,
        *,
        status: str,
        approved_for_training: bool | None = None,
        metadata: dict[str, object] | None = None,
    ) -> TrainingExampleRecord | None:
        row = self.connection.execute(
            """
            SELECT metadata_json, approved_for_training
            FROM training_examples
            WHERE id = ? AND profile_slug = ?
            LIMIT 1
            """,
            (example_id, self.profile_slug),
        ).fetchone()
        if not row:
            return None
        merged_metadata = json.loads(row["metadata_json"] or "{}")
        if metadata:
            merged_metadata.update(metadata)
        self.connection.execute(
            """
            UPDATE training_examples
            SET status = ?,
                approved_for_training = ?,
                metadata_json = ?,
                updated_at = ?
            WHERE id = ? AND profile_slug = ?
            """,
            (
                status,
                int(approved_for_training if approved_for_training is not None else bool(row["approved_for_training"])),
                json.dumps(merged_metadata),
                datetime.now(timezone.utc).isoformat(),
                example_id,
                self.profile_slug,
            ),
        )
        self.connection.commit()
        return self.get_training_example(example_id)

    def record_workflow_domain_event(self, event: WorkflowDomainEvent) -> WorkflowDomainEvent:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO workflow_domain_events (
                id, profile_slug, organization_id, workspace_slug, actor_user_id, workflow_id, workflow_type,
                event_type, detail, fingerprint, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.profile_slug,
                event.organization_id,
                event.workspace_slug,
                event.actor_user_id,
                event.workflow_id,
                event.workflow_type,
                event.event_type,
                event.detail,
                event.fingerprint,
                json.dumps(event.metadata),
                event.created_at.isoformat(),
            ),
        )
        self.connection.commit()
        return event

    def list_workflow_domain_events(
        self,
        *,
        workflow_id: str | None = None,
        workspace_slug: str | None = None,
        workflow_type: str | None = None,
        limit: int = 100,
    ) -> list[WorkflowDomainEvent]:
        query = """
            SELECT *
            FROM workflow_domain_events
            WHERE profile_slug = ?
        """
        params: list[object] = [self.profile_slug]
        if workflow_id is not None:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        if workspace_slug is not None:
            query += " AND COALESCE(workspace_slug, '') = COALESCE(?, '')"
            params.append(workspace_slug)
        if workflow_type is not None:
            query += " AND workflow_type = ?"
            params.append(workflow_type)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, tuple(params)).fetchall()
        return [
            WorkflowDomainEvent(
                id=str(row["id"]),
                profile_slug=str(row["profile_slug"]),
                organization_id=row["organization_id"],
                workspace_slug=row["workspace_slug"],
                actor_user_id=row["actor_user_id"],
                workflow_id=str(row["workflow_id"]),
                workflow_type=str(row["workflow_type"]),
                event_type=str(row["event_type"]),
                detail=str(row["detail"] or ""),
                fingerprint=str(row["fingerprint"] or ""),
                metadata=json.loads(row["metadata_json"] or "{}"),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def record_shadow_ranking(self, record: ShadowRankingRecord) -> ShadowRankingRecord:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO shadow_ranking_records (
                id, profile_slug, organization_id, workspace_slug, actor_user_id, workflow_id, recommendation_id,
                policy_name, score, features_json, outcome_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.profile_slug,
                record.organization_id,
                record.workspace_slug,
                record.actor_user_id,
                record.workflow_id,
                record.recommendation_id,
                record.policy_name,
                record.score,
                json.dumps(record.features),
                json.dumps(record.outcome),
                record.created_at.isoformat(),
            ),
        )
        self.connection.commit()
        return record

    def list_shadow_ranking_records(
        self,
        *,
        workspace_slug: str | None = None,
        recommendation_id: str | None = None,
        limit: int = 100,
    ) -> list[ShadowRankingRecord]:
        query = """
            SELECT *
            FROM shadow_ranking_records
            WHERE profile_slug = ?
        """
        params: list[object] = [self.profile_slug]
        if workspace_slug is not None:
            query += " AND COALESCE(workspace_slug, '') = COALESCE(?, '')"
            params.append(workspace_slug)
        if recommendation_id is not None:
            query += " AND recommendation_id = ?"
            params.append(recommendation_id)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, tuple(params)).fetchall()
        return [
            ShadowRankingRecord(
                id=str(row["id"]),
                profile_slug=str(row["profile_slug"]),
                organization_id=row["organization_id"],
                workspace_slug=row["workspace_slug"],
                actor_user_id=row["actor_user_id"],
                workflow_id=row["workflow_id"],
                recommendation_id=row["recommendation_id"],
                policy_name=str(row["policy_name"]),
                score=float(row["score"] or 0.0),
                features=json.loads(row["features_json"] or "{}"),
                outcome=json.loads(row["outcome_json"] or "{}"),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def replace_reasoning_snapshot(
        self,
        *,
        workspace_slug: str | None,
        workflows: list[WorkflowRecord],
        workflow_events: list[WorkflowEvent],
        obligations: list[ObligationRecord],
        decisions: list[DecisionRecord],
    ) -> None:
        workspace_value = workspace_slug or self.profile_slug
        existing_rows = self.connection.execute(
            """
            SELECT id, workflow_type, status, last_event, next_expected_step, blocking_reasons_json, due_at,
                   evidence_refs_json, subject_refs_json, metadata_json
            FROM workflow_records
            WHERE profile_slug = ? AND COALESCE(workspace_slug, '') = COALESCE(?, '')
            """,
            (self.profile_slug, workspace_value),
        ).fetchall()
        existing_by_id = {str(row["id"]): row for row in existing_rows}
        workflow_type_by_id = {workflow.id: workflow.workflow_type for workflow in workflows}
        self.connection.execute(
            "DELETE FROM obligation_records WHERE profile_slug = ? AND COALESCE(workspace_slug, '') = COALESCE(?, '')",
            (self.profile_slug, workspace_value),
        )
        self.connection.execute(
            "DELETE FROM decision_records WHERE profile_slug = ? AND COALESCE(workspace_slug, '') = COALESCE(?, '')",
            (self.profile_slug, workspace_value),
        )
        for workflow in workflows:
            previous = existing_by_id.get(workflow.id)
            fingerprint_payload = {
                "workflow_type": workflow.workflow_type,
                "status": workflow.status,
                "last_event": workflow.last_event,
                "next_expected_step": workflow.next_expected_step,
                "blocking_reasons": workflow.blocking_reasons,
                "due_at": workflow.due_at.isoformat() if workflow.due_at else None,
                "evidence_refs": workflow.evidence_refs,
                "subject_refs": workflow.subject_refs,
            }
            previous_payload = None
            if previous is not None:
                previous_payload = {
                    "workflow_type": str(previous["workflow_type"]),
                    "status": str(previous["status"] or ""),
                    "last_event": str(previous["last_event"] or ""),
                    "next_expected_step": str(previous["next_expected_step"] or ""),
                    "blocking_reasons": json.loads(previous["blocking_reasons_json"] or "[]"),
                    "due_at": previous["due_at"],
                    "evidence_refs": json.loads(previous["evidence_refs_json"] or "[]"),
                    "subject_refs": json.loads(previous["subject_refs_json"] or "{}"),
                }
            if previous_payload != fingerprint_payload:
                fingerprint = hashlib.sha256(
                    json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
                ).hexdigest()
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO workflow_domain_events (
                        id, profile_slug, organization_id, workspace_slug, actor_user_id, workflow_id, workflow_type,
                        event_type, detail, fingerprint, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"wde-{workflow.id}-{fingerprint[:16]}",
                        workflow.profile_slug,
                        workflow.organization_id,
                        workflow.workspace_slug,
                        workflow.actor_user_id,
                        workflow.id,
                        workflow.workflow_type,
                        workflow.last_event or "workflow_projected",
                        workflow.next_expected_step or workflow.status,
                        fingerprint,
                        json.dumps(
                            {
                                "current": fingerprint_payload,
                                "previous": previous_payload,
                            }
                        ),
                        workflow.updated_at.isoformat(),
                    ),
                )
            self.connection.execute(
                """
                INSERT INTO workflow_records (
                    id, profile_slug, organization_id, workspace_slug, actor_user_id, workflow_type, subject_refs_json,
                    status, last_event, next_expected_step, blocking_reasons_json, due_at, evidence_refs_json,
                    confidence, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    organization_id = excluded.organization_id,
                    workspace_slug = excluded.workspace_slug,
                    actor_user_id = excluded.actor_user_id,
                    workflow_type = excluded.workflow_type,
                    subject_refs_json = excluded.subject_refs_json,
                    status = excluded.status,
                    last_event = excluded.last_event,
                    next_expected_step = excluded.next_expected_step,
                    blocking_reasons_json = excluded.blocking_reasons_json,
                    due_at = excluded.due_at,
                    evidence_refs_json = excluded.evidence_refs_json,
                    confidence = excluded.confidence,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    workflow.id,
                    workflow.profile_slug,
                    workflow.organization_id,
                    workflow.workspace_slug,
                    workflow.actor_user_id,
                    workflow.workflow_type,
                    json.dumps(workflow.subject_refs),
                    workflow.status,
                    workflow.last_event,
                    workflow.next_expected_step,
                    json.dumps(workflow.blocking_reasons),
                    workflow.due_at.isoformat() if workflow.due_at else None,
                    json.dumps(workflow.evidence_refs),
                    workflow.confidence,
                    json.dumps(workflow.metadata),
                    workflow.created_at.isoformat(),
                    workflow.updated_at.isoformat(),
                ),
            )
        for event in workflow_events:
            workflow_type = workflow_type_by_id.get(event.workflow_id)
            if workflow_type:
                fingerprint = hashlib.sha256(
                    json.dumps(
                        {
                            "workflow_id": event.workflow_id,
                            "workflow_type": workflow_type,
                            "event_type": event.event_type,
                            "detail": event.detail,
                            "metadata": event.metadata,
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO workflow_domain_events (
                        id, profile_slug, organization_id, workspace_slug, actor_user_id, workflow_id, workflow_type,
                        event_type, detail, fingerprint, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"wde-{event.workflow_id}-{fingerprint[:16]}",
                        event.profile_slug,
                        event.organization_id,
                        event.workspace_slug,
                        event.actor_user_id,
                        event.workflow_id,
                        workflow_type,
                        event.event_type,
                        event.detail,
                        fingerprint,
                        json.dumps(event.metadata),
                        event.created_at.isoformat(),
                    ),
                )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO workflow_events (
                    id, profile_slug, organization_id, workspace_slug, actor_user_id, workflow_id, event_type,
                    detail, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.profile_slug,
                    event.organization_id,
                    event.workspace_slug,
                    event.actor_user_id,
                    event.workflow_id,
                    event.event_type,
                    event.detail,
                    json.dumps(event.metadata),
                    event.created_at.isoformat(),
                ),
            )
        for obligation in obligations:
            self.connection.execute(
                """
                INSERT INTO obligation_records (
                    id, profile_slug, organization_id, workspace_slug, actor_user_id, workflow_id, title, status,
                    reason, priority, due_at, blocking_reasons_json, evidence_refs_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    obligation.id,
                    obligation.profile_slug,
                    obligation.organization_id,
                    obligation.workspace_slug,
                    obligation.actor_user_id,
                    obligation.workflow_id,
                    obligation.title,
                    obligation.status,
                    obligation.reason,
                    obligation.priority,
                    obligation.due_at.isoformat() if obligation.due_at else None,
                    json.dumps(obligation.blocking_reasons),
                    json.dumps(obligation.evidence_refs),
                    json.dumps(obligation.metadata),
                    obligation.created_at.isoformat(),
                    obligation.updated_at.isoformat(),
                ),
            )
        for decision in decisions:
            self.connection.execute(
                """
                INSERT INTO decision_records (
                    id, profile_slug, organization_id, workspace_slug, actor_user_id, decision_kind, decision_value,
                    source_type, source_id, reasoning_source, rationale, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.id,
                    decision.profile_slug,
                    decision.organization_id,
                    decision.workspace_slug,
                    decision.actor_user_id,
                    decision.decision_kind,
                    decision.decision_value,
                    decision.source_type,
                    decision.source_id,
                    decision.reasoning_source,
                    decision.rationale,
                    json.dumps(decision.metadata),
                    decision.created_at.isoformat(),
                ),
            )
        self.connection.commit()

    def list_workflow_records(
        self,
        *,
        workspace_slug: str | None = None,
        actor_user_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkflowRecord]:
        query = """
            SELECT *
            FROM workflow_records
            WHERE profile_slug = ?
        """
        params: list[object] = [self.profile_slug]
        if workspace_slug is not None:
            query += " AND COALESCE(workspace_slug, '') = COALESCE(?, '')"
            params.append(workspace_slug)
        if actor_user_id is not None:
            query += " AND COALESCE(actor_user_id, '') IN ('', COALESCE(?, ''))"
            params.append(actor_user_id)
        query += " ORDER BY due_at IS NULL, due_at ASC, confidence DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_workflow_record(row) for row in rows]

    def get_workflow_record(self, workflow_id: str) -> WorkflowRecord | None:
        row = self.connection.execute(
            "SELECT * FROM workflow_records WHERE id = ? AND profile_slug = ? LIMIT 1",
            (workflow_id, self.profile_slug),
        ).fetchone()
        return self._row_to_workflow_record(row) if row else None

    def list_workflow_events(self, workflow_id: str, limit: int = 50) -> list[WorkflowEvent]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM workflow_events
            WHERE workflow_id = ? AND profile_slug = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (workflow_id, self.profile_slug, limit),
        ).fetchall()
        return [
            WorkflowEvent(
                id=str(row["id"]),
                profile_slug=str(row["profile_slug"]),
                organization_id=row["organization_id"],
                workspace_slug=row["workspace_slug"],
                actor_user_id=row["actor_user_id"],
                workflow_id=str(row["workflow_id"]),
                event_type=str(row["event_type"]),
                detail=str(row["detail"] or ""),
                metadata=json.loads(row["metadata_json"] or "{}"),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def list_obligation_records(
        self,
        *,
        workspace_slug: str | None = None,
        actor_user_id: str | None = None,
        limit: int = 100,
    ) -> list[ObligationRecord]:
        query = """
            SELECT *
            FROM obligation_records
            WHERE profile_slug = ?
        """
        params: list[object] = [self.profile_slug]
        if workspace_slug is not None:
            query += " AND COALESCE(workspace_slug, '') = COALESCE(?, '')"
            params.append(workspace_slug)
        if actor_user_id is not None:
            query += " AND COALESCE(actor_user_id, '') IN ('', COALESCE(?, ''))"
            params.append(actor_user_id)
        query += " ORDER BY priority DESC, due_at IS NULL, due_at ASC, updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, tuple(params)).fetchall()
        return [
            ObligationRecord(
                id=str(row["id"]),
                profile_slug=str(row["profile_slug"]),
                organization_id=row["organization_id"],
                workspace_slug=row["workspace_slug"],
                actor_user_id=row["actor_user_id"],
                workflow_id=row["workflow_id"],
                title=str(row["title"]),
                status=str(row["status"]),
                reason=str(row["reason"] or ""),
                priority=int(row["priority"] or 1),
                due_at=datetime.fromisoformat(row["due_at"]) if row["due_at"] else None,
                blocking_reasons=json.loads(row["blocking_reasons_json"] or "[]"),
                evidence_refs=json.loads(row["evidence_refs_json"] or "[]"),
                metadata=json.loads(row["metadata_json"] or "{}"),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def list_decision_records(
        self,
        *,
        workspace_slug: str | None = None,
        actor_user_id: str | None = None,
        limit: int = 100,
    ) -> list[DecisionRecord]:
        query = """
            SELECT *
            FROM decision_records
            WHERE profile_slug = ?
        """
        params: list[object] = [self.profile_slug]
        if workspace_slug is not None:
            query += " AND COALESCE(workspace_slug, '') = COALESCE(?, '')"
            params.append(workspace_slug)
        if actor_user_id is not None:
            query += " AND COALESCE(actor_user_id, '') IN ('', COALESCE(?, ''))"
            params.append(actor_user_id)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, tuple(params)).fetchall()
        return [
            DecisionRecord(
                id=str(row["id"]),
                profile_slug=str(row["profile_slug"]),
                organization_id=row["organization_id"],
                workspace_slug=row["workspace_slug"],
                actor_user_id=row["actor_user_id"],
                decision_kind=str(row["decision_kind"]),
                decision_value=str(row["decision_value"]),
                source_type=str(row["source_type"]),
                source_id=str(row["source_id"]),
                reasoning_source=str(row["reasoning_source"] or "system_decision"),
                rationale=str(row["rationale"] or ""),
                metadata=json.loads(row["metadata_json"] or "{}"),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def upsert_regulated_document(self, record: RegulatedDocumentRecord) -> RegulatedDocumentRecord:
        self.connection.execute(
            """
            INSERT INTO regulated_documents (
                id, profile_slug, workspace_slug, organization_id, data_class, title, document_id,
                business_document_id, current_version_id, current_version_number, immutability_state,
                retention_state, finalized_at, finalized_by_user_id, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                document_id = excluded.document_id,
                business_document_id = excluded.business_document_id,
                current_version_id = excluded.current_version_id,
                current_version_number = excluded.current_version_number,
                immutability_state = excluded.immutability_state,
                retention_state = excluded.retention_state,
                finalized_at = excluded.finalized_at,
                finalized_by_user_id = excluded.finalized_by_user_id,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                record.id,
                record.profile_slug,
                record.workspace_slug,
                record.organization_id,
                record.data_class,
                record.title,
                record.document_id,
                record.business_document_id,
                record.current_version_id,
                record.current_version_number,
                record.immutability_state,
                record.retention_state,
                record.finalized_at.isoformat() if record.finalized_at else None,
                record.finalized_by_user_id,
                json.dumps(record.metadata),
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        self.connection.commit()
        return record

    def get_regulated_document(self, regulated_document_id: str) -> RegulatedDocumentRecord | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM regulated_documents
            WHERE id = ? AND profile_slug = ?
            LIMIT 1
            """,
            (regulated_document_id, self.profile_slug),
        ).fetchone()
        if not row:
            return None
        return self._row_to_regulated_document(row)

    def find_regulated_document(
        self,
        *,
        document_id: str | None = None,
        business_document_id: str | None = None,
    ) -> RegulatedDocumentRecord | None:
        if not document_id and not business_document_id:
            return None
        query = "SELECT * FROM regulated_documents WHERE profile_slug = ?"
        params: list[object] = [self.profile_slug]
        if document_id:
            query += " AND document_id = ?"
            params.append(document_id)
        if business_document_id:
            query += " AND business_document_id = ?"
            params.append(business_document_id)
        query += " ORDER BY updated_at DESC, id DESC LIMIT 1"
        row = self.connection.execute(query, tuple(params)).fetchone()
        return self._row_to_regulated_document(row) if row else None

    def list_regulated_documents(self, limit: int = 50) -> list[RegulatedDocumentRecord]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM regulated_documents
            WHERE profile_slug = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (self.profile_slug, limit),
        ).fetchall()
        return [self._row_to_regulated_document(row) for row in rows]

    def append_regulated_document_version(self, version: RegulatedDocumentVersion) -> RegulatedDocumentVersion:
        self.connection.execute(
            """
            INSERT INTO regulated_document_versions (
                id, regulated_document_id, version_number, supersedes_version_id, document_id, business_document_id,
                content_digest, version_chain_digest, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.id,
                version.regulated_document_id,
                version.version_number,
                version.supersedes_version_id,
                version.document_id,
                version.business_document_id,
                version.content_digest,
                version.version_chain_digest,
                json.dumps(version.metadata),
                version.created_at.isoformat(),
            ),
        )
        self.connection.commit()
        return version

    def list_regulated_document_versions(self, regulated_document_id: str) -> list[RegulatedDocumentVersion]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM regulated_document_versions
            WHERE regulated_document_id = ?
            ORDER BY version_number ASC, created_at ASC, id ASC
            """,
            (regulated_document_id,),
        ).fetchall()
        return [
            RegulatedDocumentVersion(
                id=str(row["id"]),
                regulated_document_id=str(row["regulated_document_id"]),
                version_number=int(row["version_number"]),
                supersedes_version_id=row["supersedes_version_id"],
                document_id=row["document_id"],
                business_document_id=row["business_document_id"],
                content_digest=str(row["content_digest"]),
                version_chain_digest=str(row["version_chain_digest"]),
                metadata=json.loads(row["metadata_json"] or "{}"),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def _row_to_regulated_document(self, row: sqlite3.Row) -> RegulatedDocumentRecord:
        return RegulatedDocumentRecord(
            id=str(row["id"]),
            profile_slug=str(row["profile_slug"]),
            workspace_slug=str(row["workspace_slug"]),
            organization_id=row["organization_id"],
            data_class=str(row["data_class"] or "regulated_business"),
            title=str(row["title"]),
            document_id=row["document_id"],
            business_document_id=row["business_document_id"],
            current_version_id=row["current_version_id"],
            current_version_number=int(row["current_version_number"] or 0),
            immutability_state=str(row["immutability_state"] or "draft"),
            retention_state=str(row["retention_state"] or "standard"),
            finalized_at=datetime.fromisoformat(row["finalized_at"]) if row["finalized_at"] else None,
            finalized_by_user_id=row["finalized_by_user_id"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_workflow_record(self, row: sqlite3.Row) -> WorkflowRecord:
        return WorkflowRecord(
            id=str(row["id"]),
            profile_slug=str(row["profile_slug"]),
            organization_id=row["organization_id"],
            workspace_slug=row["workspace_slug"],
            actor_user_id=row["actor_user_id"],
            workflow_type=str(row["workflow_type"]),
            subject_refs=json.loads(row["subject_refs_json"] or "{}"),
            status=str(row["status"] or "open"),
            last_event=str(row["last_event"] or ""),
            next_expected_step=str(row["next_expected_step"] or ""),
            blocking_reasons=json.loads(row["blocking_reasons_json"] or "[]"),
            due_at=datetime.fromisoformat(row["due_at"]) if row["due_at"] else None,
            evidence_refs=json.loads(row["evidence_refs_json"] or "[]"),
            confidence=float(row["confidence"] or 0.0),
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def upsert_compliance_rule(self, rule: ComplianceReminderRule) -> None:
        self.connection.execute(
            """
            INSERT INTO compliance_rules (id, profile_slug, title, cadence, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                profile_slug = excluded.profile_slug,
                title = excluded.title,
                cadence = excluded.cadence,
                details = excluded.details
            """,
            (rule.id, self.profile_slug, rule.title, rule.cadence, rule.details, datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()

    def list_compliance_rules(self, limit: int = 20) -> list[ComplianceReminderRule]:
        rows = self.connection.execute(
            """
            SELECT id, title, cadence, details
            FROM compliance_rules
            WHERE profile_slug = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (self.profile_slug, limit),
        ).fetchall()
        return [
            ComplianceReminderRule(id=row["id"], title=row["title"], cadence=row["cadence"], details=row["details"])
            for row in rows
        ]

    def upsert_sync_target(self, target: SyncTarget, metadata: dict[str, object] | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            INSERT INTO sync_targets (id, profile_slug, kind, label, path_or_url, enabled, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                profile_slug = excluded.profile_slug,
                kind = excluded.kind,
                label = excluded.label,
                path_or_url = excluded.path_or_url,
                enabled = excluded.enabled,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                target.id,
                self.profile_slug,
                target.kind,
                target.label,
                target.path_or_url,
                1 if target.enabled else 0,
                json.dumps(metadata or {}),
                now,
                now,
            ),
        )
        self.connection.commit()

    def find_sync_target(self, kind: str, path_or_url: str) -> SyncTarget | None:
        row = self.connection.execute(
            """
            SELECT id, kind, label, path_or_url, enabled
            FROM sync_targets
            WHERE profile_slug = ? AND kind = ? AND path_or_url = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (self.profile_slug, kind, path_or_url),
        ).fetchone()
        if not row:
            return None
        return SyncTarget(
            id=row["id"],
            kind=row["kind"],
            label=row["label"],
            path_or_url=row["path_or_url"],
            enabled=bool(row["enabled"]),
        )

    def list_sync_targets(self) -> list[SyncTarget]:
        rows = self.connection.execute(
            """
            SELECT id, kind, label, path_or_url, enabled
            FROM sync_targets
            WHERE profile_slug = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (self.profile_slug,),
        ).fetchall()
        return [
            SyncTarget(
                id=row["id"],
                kind=row["kind"],
                label=row["label"],
                path_or_url=row["path_or_url"],
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def get_sync_target_details(self, target_id: str) -> dict[str, object] | None:
        row = self.connection.execute(
            """
            SELECT id, kind, label, path_or_url, enabled, metadata_json
            FROM sync_targets
            WHERE id = ? AND profile_slug = ?
            """,
            (target_id, self.profile_slug),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "label": row["label"],
            "path_or_url": row["path_or_url"],
            "enabled": bool(row["enabled"]),
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }

    def delete_embeddings(self, content_type: str) -> None:
        self.connection.execute(
            "DELETE FROM memory_embeddings WHERE content_type = ?",
            (content_type,),
        )
        self.connection.commit()

    def upsert_embedding(self, content_type: str, content_id: str, embedding: list[float]) -> None:
        self.connection.execute(
            "DELETE FROM memory_embeddings WHERE content_type = ? AND content_id = ?",
            (content_type, content_id),
        )
        self.connection.execute(
            """
            INSERT INTO memory_embeddings (content_type, content_id, embedding_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (content_type, content_id, json.dumps(embedding), datetime.now(timezone.utc).isoformat()),
        )

    def list_embeddings(self, content_type: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT content_id, embedding_json, created_at
            FROM memory_embeddings
            WHERE content_type = ?
            ORDER BY id ASC
            """,
            (content_type,),
        ).fetchall()
        return [
            {
                "content_id": row["content_id"],
                "embedding": json.loads(row["embedding_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def set_index_metadata(self, key: str, value: str) -> None:
        self.set_value("retrieval_index", key, value)

    def get_index_metadata(self, key: str, default: str | None = None) -> str | None:
        return self.get_value("retrieval_index", key, default)

    def save_recovery_checkpoint(self, checkpoint: RecoveryCheckpoint, payload: dict[str, object] | None = None) -> None:
        self.connection.execute(
            """
            INSERT INTO recovery_checkpoints (job_id, profile_slug, stage, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                checkpoint.job_id,
                self.profile_slug,
                checkpoint.stage,
                json.dumps(payload or {}),
                checkpoint.updated_at.isoformat(),
            ),
        )
        self.connection.commit()

    def list_recovery_checkpoints(self, job_id: str, limit: int = 20) -> list[RecoveryCheckpoint]:
        rows = self.connection.execute(
            """
            SELECT job_id, stage, updated_at
            FROM recovery_checkpoints
            WHERE job_id = ? AND profile_slug = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (job_id, self.profile_slug, limit),
        ).fetchall()
        return [
            RecoveryCheckpoint(
                job_id=row["job_id"],
                stage=row["stage"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def search_conversation_history(
        self,
        query: str,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Lexical search over conversation_log with optional date range."""
        sql = "SELECT id, created_at, content FROM conversation_log WHERE 1=1"
        params: list = []
        if query:
            sql += " AND LOWER(content) LIKE ?"
            params.append(f"%{query.lower()}%")
        if date_from:
            sql += " AND created_at >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND created_at <= ?"
            params.append(date_to)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "content": row["content"],
                "date": row["created_at"][:10] if row["created_at"] else "",
            }
            for row in rows
        ]

    def build_topic_timeline(self, topic_query: str, limit: int = 40) -> list[dict]:
        """Return conversation turns grouped by date that match a topic query."""
        hits = self.search_conversation_history(topic_query, limit=limit)
        grouped: dict[str, list[dict]] = {}
        for hit in hits:
            date = hit.get("date", "unknown")
            grouped.setdefault(date, []).append(hit)
        return [
            {"date": date, "entries": entries}
            for date, entries in sorted(grouped.items(), reverse=True)
        ]

    def consolidate_memory(self, older_than_days: int = 30) -> int:
        """Summarise old conversation turns into conversation_summaries and prune them.

        Returns the number of turns consolidated.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        rows = self.connection.execute(
            "SELECT id, created_at, content FROM conversation_log WHERE created_at < ? ORDER BY created_at ASC LIMIT 200",
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0
        contents = [str(row["content"] or "").strip() for row in rows if row["content"]]
        extracted = self._extract_structured_memory(contents)
        summary = self._build_consolidated_summary(contents, extracted=extracted)
        self.remember_memory_item(
            self._memory_item_key("episode", f"{rows[0]['id']}-{rows[-1]['id']}"),
            summary,
            memory_kind="episodic_summary",
            source="memory_consolidation",
            confidence=0.58,
            provenance={"origin": "conversation_consolidation", "turn_count": len(rows)},
        )
        self.connection.execute(
            "INSERT INTO conversation_summaries (created_at, summary) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(), f"[auto-consolidated]\n{summary}"),
        )
        for key, value in extracted["facts"]:
            self.remember_memory_item(
                key,
                value,
                memory_kind=self._memory_kind_for_key(key, "memory_consolidation"),
                source="memory_consolidation",
                confidence=0.72,
                provenance={"origin": "conversation_consolidation"},
            )
        for decision in extracted["decisions"]:
            self.remember_memory_item(
                self._memory_item_key("decision", decision),
                decision.strip(),
                memory_kind="decision",
                source="memory_consolidation",
                confidence=0.66,
                provenance={"origin": "conversation_consolidation"},
            )
        existing_loops = {loop.title.lower() for loop in self.list_open_loops(limit=200)}
        for commitment in extracted["commitments"]:
            title = commitment["title"]
            self.remember_memory_item(
                self._memory_item_key("commitment", title),
                title,
                memory_kind="commitment",
                source="memory_consolidation",
                confidence=0.69,
                provenance={"origin": "conversation_consolidation"},
            )
            if title.lower() in existing_loops:
                continue
            self.create_open_loop(
                title=title,
                details="Recovered from consolidated conversation history.",
                source="memory_consolidation",
            )
        ids = [row["id"] for row in rows]
        self.connection.execute(
            f"DELETE FROM conversation_log WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        self.connection.commit()
        return len(rows)

    async def consolidate_memory_async(self, older_than_days: int = 30) -> int:
        """Async variant of consolidate_memory that tries LLM summarization first."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        rows = self.connection.execute(
            "SELECT id, created_at, content FROM conversation_log WHERE created_at < ? ORDER BY created_at ASC LIMIT 200",
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0
        contents = [str(row["content"] or "").strip() for row in rows if row["content"]]

        # Try LLM summarization first
        llm_summary = await self._llm_summarize(contents)
        if llm_summary:
            summary = llm_summary
            logger.info("Memory consolidation used LLM summarization for %d turns.", len(contents))
        else:
            extracted = self._extract_structured_memory(contents)
            summary = self._build_consolidated_summary(contents, extracted=extracted)
            logger.info("Memory consolidation used template fallback for %d turns (LLM unavailable).", len(contents))

        # Store the summary - reuse existing consolidation logic
        extracted = self._extract_structured_memory(contents)
        self.remember_memory_item(
            self._memory_item_key("episode", f"{rows[0]['id']}-{rows[-1]['id']}"),
            summary,
            memory_kind="episodic_summary",
            source="memory_consolidation",
            confidence=0.72 if llm_summary else 0.58,
            provenance={"origin": "conversation_consolidation", "turn_count": len(rows), "method": "llm" if llm_summary else "template"},
        )
        self.connection.execute(
            "INSERT INTO conversation_summaries (created_at, summary) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(), f"[auto-consolidated]\n{summary}"),
        )
        for key, value in extracted["facts"]:
            self.remember_memory_item(
                key,
                value,
                memory_kind=self._memory_kind_for_key(key, "memory_consolidation"),
                source="memory_consolidation",
                confidence=0.72,
                provenance={"origin": "conversation_consolidation"},
            )
        for decision in extracted["decisions"]:
            self.remember_memory_item(
                self._memory_item_key("decision", decision),
                decision.strip(),
                memory_kind="decision",
                source="memory_consolidation",
                confidence=0.66,
                provenance={"origin": "conversation_consolidation"},
            )
        existing_loops = {loop.title.lower() for loop in self.list_open_loops(limit=200)}
        for commitment in extracted["commitments"]:
            title = commitment["title"]
            self.remember_memory_item(
                self._memory_item_key("commitment", title),
                title,
                memory_kind="commitment",
                source="memory_consolidation",
                confidence=0.69,
                provenance={"origin": "conversation_consolidation"},
            )
            if title.lower() in existing_loops:
                continue
            self.create_open_loop(
                title=title,
                details="Recovered from consolidated conversation history.",
                source="memory_consolidation",
            )
        ids = [row["id"] for row in rows]
        self.connection.execute(
            f"DELETE FROM conversation_log WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        self.connection.commit()
        return len(rows)

    def _extract_structured_memory(self, contents: list[str]) -> dict[str, list]:
        facts: list[tuple[str, str]] = []
        decisions: list[str] = []
        commitments: list[dict[str, str]] = []
        for line in contents:
            lowered = line.lower().strip()
            if not lowered:
                continue
            if "preferred editor" in lowered and " is " in lowered:
                value = line.split(" is ", 1)[-1].strip(" .")
                if value:
                    facts.append(("preferred editor", value))
            elif "prefer concise" in lowered or "concise answers" in lowered:
                facts.append(("response style", "concise answers"))
            elif "prefer " in lowered and "answers" in lowered:
                tail = line.split("prefer", 1)[-1].strip(" .")
                if tail:
                    facts.append(("response style", tail))

            if any(token in lowered for token in ("decision:", "decided", "agreed", "keep ", "ship ", "use ")):
                decisions.append(line.strip())

            if any(token in lowered for token in ("todo:", "follow up", "next step", "need to", "should ", "fix ")):
                cleaned = re.sub(r"^(todo:|next step:)\s*", "", line.strip(), flags=re.IGNORECASE)
                cleaned = re.sub(r"\b(before|by) \d{4}-\d{2}-\d{2}\b", "", cleaned, flags=re.IGNORECASE).strip(" .")
                if cleaned:
                    title = cleaned[:80]
                    commitments.append({"title": title})
        return {
            "facts": facts[:6],
            "decisions": decisions[:6],
            "commitments": commitments[:6],
        }

    async def _llm_summarize(self, contents: list[str]) -> str | None:
        """Attempt to summarize conversation turns using the local LLM.

        Returns None if the LLM is unavailable or the request fails.
        """
        if self._llm_client is None or not getattr(self._llm_client, "available", False):
            return None
        combined = "\n".join(contents[:100])
        if len(combined) > 6000:
            combined = combined[:6000]
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a memory consolidation assistant. Summarize the following conversation turns "
                    "into a structured summary. Extract: key facts, user preferences, decisions made, "
                    "commitments/action items, and main topics discussed. Be concise but preserve "
                    "all important information. Output in plain text with clear section headers."
                ),
            },
            {"role": "user", "content": f"Summarize these {len(contents)} conversation turns:\n\n{combined}"},
        ]
        try:
            response = await self._llm_client.chat(messages, max_tokens=512, temperature=0.2)
            choices = response.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                if content and len(content.strip()) > 20:
                    return content.strip()
            return None
        except Exception as exc:
            logger.info("LLM summarization failed, will use template fallback: %s", exc)
            return None

    def _build_consolidated_summary(self, contents: list[str], *, extracted: dict[str, list] | None = None) -> str:
        extracted = extracted or self._extract_structured_memory(contents)
        preference_lines = [line for line in contents if any(token in line.lower() for token in ("prefer", "preferred", "preference", "concise", "editor"))]
        decision_lines = [line for line in contents if any(token in line.lower() for token in ("decision", "decided", "agreed", "keep ", "ship", "use "))][:6]
        action_lines = [line for line in contents if any(token in line.lower() for token in ("todo", "follow up", "next step", "need to", "should ", "fix "))][:6]
        date_matches: list[str] = []
        for line in contents:
            date_matches.extend(re.findall(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b", line))
        topic_tokens: dict[str, int] = {}
        for line in contents:
            for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", line.lower()):
                if token in {"that", "this", "with", "from", "have", "will", "would", "about", "there", "their", "your"}:
                    continue
                topic_tokens[token] = topic_tokens.get(token, 0) + 1
        topics = [token for token, _ in sorted(topic_tokens.items(), key=lambda item: (-item[1], item[0]))[:8]]

        # Extractive summarization: score each sentence by TF-IDF-like relevance
        key_sentences = self._extractive_summarize(contents, topic_tokens, top_n=10)

        parts = [
            f"Consolidated {len(contents)} older turns.",
            f"Preferences: {' | '.join(preference_lines[:3]) or 'none captured'}.",
            f"Decisions: {' | '.join(decision_lines[:3]) or 'none captured'}.",
            f"Commitments: {' | '.join(action_lines[:3]) or 'none captured'}.",
            f"Dates: {', '.join(date_matches[:6]) or 'none captured'}.",
            f"Topics: {', '.join(topics) or 'none captured'}.",
            f"Recovered facts: {', '.join(f'{key}={value}' for key, value in extracted['facts'][:3]) or 'none captured'}.",
            f"Recovered commitments: {', '.join(item['title'] for item in extracted['commitments'][:3]) or 'none captured'}.",
        ]
        if key_sentences:
            parts.append(f"Key points:\n" + "\n".join(f"  - {s}" for s in key_sentences))
        return "\n".join(parts)

    @staticmethod
    def _extractive_summarize(contents: list[str], token_freq: dict[str, int], *, top_n: int = 10) -> list[str]:
        """Score each content line by cumulative token frequency and return top N.

        This is a simple TF-based extractive summarizer: sentences that
        contain the most frequent meaningful tokens score higher.
        """
        import math

        if not contents or not token_freq:
            return []

        num_docs = len(contents)
        # Document frequency for IDF approximation
        doc_freq: dict[str, int] = {}
        for line in contents:
            seen: set[str] = set()
            for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", line.lower()):
                if token not in seen:
                    doc_freq[token] = doc_freq.get(token, 0) + 1
                    seen.add(token)

        scored: list[tuple[float, int, str]] = []
        for idx, line in enumerate(contents):
            stripped = line.strip()
            if len(stripped) < 15:
                continue
            tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", stripped.lower())
            if not tokens:
                continue
            score = 0.0
            for token in tokens:
                tf = token_freq.get(token, 0)
                df = doc_freq.get(token, 1)
                idf = math.log((num_docs + 1) / (df + 1)) + 1.0
                score += tf * idf
            # Normalize by line length to avoid favoring very long lines
            score /= max(len(tokens), 1)
            scored.append((score, idx, stripped))

        scored.sort(key=lambda item: -item[0])
        # Return top N sentences in original order
        selected = sorted(scored[:top_n], key=lambda item: item[1])
        return [s for _, _, s in selected]

    def run_maintenance(self) -> None:
        self._trim_table("conversation_log", self.CONVERSATION_RETENTION)
        self._trim_table("runtime_logs", self.RUNTIME_LOG_RETENTION)
        self._trim_table("execution_receipts", self.RECEIPT_RETENTION)
        self._trim_table("transcript_artifacts", self.ARTIFACT_RETENTION)
        self.connection.execute("PRAGMA optimize")
        self.connection.commit()
