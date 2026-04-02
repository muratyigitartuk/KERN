from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import Connection
from uuid import uuid4

import sounddevice as sd

logger = logging.getLogger(__name__)

from app.artifacts import ArtifactStore
from app.memory import MemoryRepository
from app.platform import PlatformStore
from app.local_data import LocalDataService
from app.types import ExtractedActionItem, MeetingRecord, MeetingReviewSnapshot, ProfileSummary, TranscriptArtifact, TranscriptSummary


MEETING_SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_records (
    id TEXT PRIMARY KEY,
    profile_slug TEXT NOT NULL DEFAULT 'default',
    title TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    transcript_path TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'recorded',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meeting_records_created_at ON meeting_records(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS transcript_action_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    profile_slug TEXT NOT NULL DEFAULT 'default',
    title TEXT NOT NULL,
    details TEXT,
    due_hint TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcript_action_items_meeting_id ON transcript_action_items(meeting_id, id ASC);
"""


class MeetingService:
    def __init__(
        self,
        connection: Connection,
        platform: PlatformStore,
        profile: ProfileSummary,
        local_data: LocalDataService | None = None,
    ) -> None:
        self.connection = connection
        self.platform = platform
        self.profile = profile
        self.local_data = local_data
        self.memory = MemoryRepository(connection, profile_slug=profile.slug)
        self.artifacts = ArtifactStore(platform, profile)
        self.connection.executescript(MEETING_SCHEMA)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(meeting_records)").fetchall()}
        if "summary_json" not in columns:
            self.connection.execute("ALTER TABLE meeting_records ADD COLUMN summary_json TEXT NOT NULL DEFAULT '{}'")
        if "profile_slug" not in columns:
            self.connection.execute("ALTER TABLE meeting_records ADD COLUMN profile_slug TEXT NOT NULL DEFAULT 'default'")
            self.connection.execute("UPDATE meeting_records SET profile_slug = ? WHERE profile_slug IS NULL OR profile_slug = ''", (self.profile.slug,))
        action_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(transcript_action_items)").fetchall()}
        if "review_state" not in action_columns:
            self.connection.execute("ALTER TABLE transcript_action_items ADD COLUMN review_state TEXT NOT NULL DEFAULT 'pending'")
        if "related_task_id" not in action_columns:
            self.connection.execute("ALTER TABLE transcript_action_items ADD COLUMN related_task_id INTEGER")
        if "related_reminder_id" not in action_columns:
            self.connection.execute("ALTER TABLE transcript_action_items ADD COLUMN related_reminder_id INTEGER")
        if "profile_slug" not in action_columns:
            self.connection.execute("ALTER TABLE transcript_action_items ADD COLUMN profile_slug TEXT NOT NULL DEFAULT 'default'")
            self.connection.execute("UPDATE transcript_action_items SET profile_slug = ? WHERE profile_slug IS NULL OR profile_slug = ''", (self.profile.slug,))
        artifact_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(transcript_artifacts)").fetchall()}
        if "profile_slug" not in artifact_columns:
            self.connection.execute("ALTER TABLE transcript_artifacts ADD COLUMN profile_slug TEXT NOT NULL DEFAULT 'default'")
            self.connection.execute("UPDATE transcript_artifacts SET profile_slug = ? WHERE profile_slug IS NULL OR profile_slug = ''", (self.profile.slug,))
        self.connection.commit()
        self._recording_stream = None
        self._recording_wave = None
        self._recording_path: Path | None = None
        self._recording_id: str | None = None
        self._recording_title: str | None = None

    @property
    def recording_in_progress(self) -> bool:
        return self._recording_stream is not None and self._recording_id is not None and self._recording_path is not None

    def availability(self) -> tuple[bool, str | None]:
        if self.platform.is_profile_locked(self.profile.slug):
            return False, "Unlock the active profile to access meetings."
        if not hasattr(sd, "RawInputStream"):
            return False, "Audio recording backend unavailable."
        return True, "Recording available."

    def start_recording(self, title: str) -> MeetingRecord:
        self._ensure_unlocked("start_recording")
        if self._recording_stream is not None:
            raise RuntimeError("A meeting recording is already in progress.")
        meeting_id = str(uuid4())
        handle, temp_path = tempfile.mkstemp(prefix=f"meeting-{meeting_id}-", suffix=".wav")
        os.close(handle)
        Path(temp_path).unlink(missing_ok=True)
        audio_path = Path(temp_path)
        wave_file = wave.open(str(audio_path), "wb")
        wave_file.setnchannels(1)
        wave_file.setsampwidth(2)
        wave_file.setframerate(16000)

        def callback(indata, frames, time_info, status):  # pragma: no cover
            if status:
                return
            wave_file.writeframes(indata)

        stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", callback=callback)
        stream.start()
        self._recording_stream = stream
        self._recording_wave = wave_file
        self._recording_path = audio_path
        self._recording_id = meeting_id
        self._recording_title = title
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """
            INSERT INTO meeting_records (id, profile_slug, title, audio_path, transcript_path, summary_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, '{}', 'recording', ?, ?)
            """,
            (meeting_id, self.profile.slug, title, str(audio_path), now, now),
        )
        self.connection.commit()
        self.platform.record_audit("meetings", "start_recording", "success", f"Started meeting recording {title}.", profile_slug=self.profile.slug)
        return MeetingRecord(id=meeting_id, profile_slug=self.profile.slug, title=title, audio_path=str(audio_path), created_at=datetime.fromisoformat(now))

    def stop_recording(self) -> MeetingRecord:
        self._ensure_unlocked("stop_recording")
        if self._recording_stream is None or self._recording_wave is None or self._recording_id is None or self._recording_path is None:
            raise RuntimeError("No meeting recording is in progress.")
        with contextlib.suppress(Exception):  # cleanup — best-effort
            self._recording_stream.stop()
        with contextlib.suppress(Exception):  # cleanup — best-effort
            self._recording_stream.close()
        self._recording_wave.close()
        final_audio_path = self.artifacts.import_file(
            self._recording_path,
            Path(self.profile.meetings_root) / f"{self._recording_id}.wav",
        )
        if final_audio_path != self._recording_path:
            with contextlib.suppress(FileNotFoundError):
                self._recording_path.unlink()
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "UPDATE meeting_records SET audio_path = ?, status = 'recorded', updated_at = ? WHERE id = ? AND profile_slug = ?",
            (str(final_audio_path), now, self._recording_id, self.profile.slug),
        )
        self.connection.commit()
        record = MeetingRecord(
            id=self._recording_id,
            profile_slug=self.profile.slug,
            title=self._recording_title or "Meeting",
            audio_path=str(final_audio_path),
            created_at=datetime.fromisoformat(now),
        )
        self.platform.record_audit("meetings", "stop_recording", "success", f"Stopped meeting recording {record.title}.", profile_slug=self.profile.slug)
        self._recording_stream = None
        self._recording_wave = None
        self._recording_path = None
        self._recording_id = None
        self._recording_title = None
        return record

    def abort_recording(self, reason: str = "Meeting recording aborted.") -> None:
        if self._recording_stream is None or self._recording_wave is None or self._recording_id is None or self._recording_path is None:
            return
        meeting_id = self._recording_id
        recording_path = self._recording_path
        with contextlib.suppress(Exception):  # cleanup — best-effort
            self._recording_stream.stop()
        with contextlib.suppress(Exception):  # cleanup — best-effort
            self._recording_stream.close()
        with contextlib.suppress(Exception):  # cleanup — best-effort
            self._recording_wave.close()
        with contextlib.suppress(Exception):  # cleanup — best-effort
            recording_path.unlink(missing_ok=True)
        self.connection.execute(
            "UPDATE meeting_records SET status = 'failed', updated_at = ? WHERE id = ? AND profile_slug = ?",
            (datetime.now(timezone.utc).isoformat(), meeting_id, self.profile.slug),
        )
        self.connection.commit()
        self.platform.record_audit(
            "meetings",
            "abort_recording",
            "warning",
            reason,
            profile_slug=self.profile.slug,
            details={"meeting_id": meeting_id},
        )
        self._recording_stream = None
        self._recording_wave = None
        self._recording_path = None
        self._recording_id = None
        self._recording_title = None

    def list_meetings(self, limit: int = 10, *, audit: bool = True) -> list[MeetingRecord]:
        self._ensure_unlocked("list_meetings")
        rows = self.connection.execute(
            "SELECT id, title, audio_path, transcript_path, status, created_at FROM meeting_records WHERE profile_slug = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (self.profile.slug, limit),
        ).fetchall()
        meetings = [
            MeetingRecord(
                id=row["id"],
                profile_slug=self.profile.slug,
                title=row["title"],
                audio_path=row["audio_path"],
                transcript_path=row["transcript_path"],
                status=row["status"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]
        if audit:
            self.platform.record_audit(
                "meetings",
                "list_meetings",
                "success",
                "Listed meeting records.",
                profile_slug=self.profile.slug,
                details={"count": len(meetings)},
            )
        return meetings

    def list_recent_reviews(self, limit: int = 6, *, audit: bool = True) -> list[MeetingReviewSnapshot]:
        self._ensure_unlocked("list_meeting_reviews")
        reviews: list[MeetingReviewSnapshot] = []
        for meeting in self.list_meetings(limit=limit, audit=False):
            artifacts = self.memory.list_transcript_artifacts(meeting.id)
            hydrated_artifacts = [self._hydrate_artifact(artifact) for artifact in artifacts]
            action_items = [
                ExtractedActionItem(
                    id=item.get("id"),
                    meeting_id=meeting.id,
                    title=str(item.get("title", "")),
                    details=item.get("details"),
                    due_hint=item.get("due_hint"),
                    review_state=str(item.get("review_state", "pending")),
                    accepted=str(item.get("review_state", "pending")) == "accepted",
                    related_task_id=item.get("related_task_id"),
                    related_reminder_id=item.get("related_reminder_id"),
                )
                for item in self.memory.list_transcript_action_items(meeting.id)
            ]
            summary_artifact = next((artifact for artifact in hydrated_artifacts if artifact.artifact_type == "summary"), None)
            transcript_artifact = next((artifact for artifact in hydrated_artifacts if artifact.artifact_type == "transcript"), None)
            reviews.append(
                MeetingReviewSnapshot(
                    meeting=meeting,
                    summary=summary_artifact,
                    transcript=transcript_artifact,
                    action_items=action_items,
                )
            )
        if audit:
            self.platform.record_audit(
                "meetings",
                "list_meeting_reviews",
                "success",
                "Listed meeting review snapshots.",
                profile_slug=self.profile.slug,
                details={"count": len(reviews)},
            )
        return reviews

    def review_action_item(self, item_id: int, accepted: bool) -> ExtractedActionItem:
        self._ensure_unlocked("review_meeting_action_item")
        row = self.connection.execute(
            """
            SELECT tai.id, tai.meeting_id, tai.title, tai.details, tai.due_hint, mr.profile_slug
            FROM transcript_action_items tai
            JOIN meeting_records mr ON mr.id = tai.meeting_id
            WHERE tai.id = ? AND mr.profile_slug = ?
            """,
            (item_id, self.profile.slug),
        ).fetchone()
        if not row:
            raise RuntimeError("Meeting action item not found.")
        review_state = "accepted" if accepted else "rejected"
        related_task_id: int | None = None
        related_reminder_id: int | None = None
        if accepted and self.local_data:
            due_at = self._parse_due_hint(row["due_hint"])
            follow_up_title = f"Follow-up: {row['title']}"
            if due_at is not None:
                related_reminder_id = self.local_data.create_reminder(follow_up_title, due_at)
            else:
                related_task_id = self.local_data.create_task(follow_up_title)
        self.memory.update_transcript_action_item_review(
            item_id,
            review_state,
            related_task_id=related_task_id,
            related_reminder_id=related_reminder_id,
        )
        self.platform.record_audit(
            "meetings",
            "review_meeting_action_item",
            "success",
            f"{'Accepted' if accepted else 'Rejected'} extracted action item {row['title']}.",
            profile_slug=self.profile.slug,
            details={
                "item_id": item_id,
                "meeting_id": row["meeting_id"],
                "review_state": review_state,
                "related_task_id": related_task_id,
                "related_reminder_id": related_reminder_id,
            },
        )
        return ExtractedActionItem(
            id=row["id"],
            meeting_id=row["meeting_id"],
            title=row["title"],
            details=row["details"],
            due_hint=row["due_hint"],
            review_state=review_state,
            accepted=accepted,
            related_task_id=related_task_id,
            related_reminder_id=related_reminder_id,
        )

    def _build_summary(self, meeting_id: str, transcript: str) -> TranscriptSummary:
        sentences = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", transcript) if segment.strip()]
        decisions = [
            line.strip()
            for line in sentences
            if any(
                token in line.lower()
                for token in ["decided", "agreed", "decision", "beschlossen", "vereinbart", "wir machen", "we will"]
            )
        ]
        reminders = [
            line.strip()
            for line in sentences
            if any(
                token in line.lower()
                for token in ["remind", "deadline", "follow up", "follow-up", "next week", "tomorrow", "morgen", "bis ", "due "]
            )
        ]
        summary_text = " ".join(sentences[:3]) if sentences else "No transcript content captured."
        return TranscriptSummary(meeting_id=meeting_id, summary=summary_text, decisions=decisions[:5], reminders=reminders[:5])

    def _extract_action_items(self, meeting_id: str, transcript: str) -> list[ExtractedActionItem]:
        items: list[ExtractedActionItem] = []
        seen_titles: set[str] = set()
        for line in re.split(r"[\n\.]", transcript):
            cleaned = re.sub(r"\s+", " ", line).strip(" -:\t")
            lowered = cleaned.lower()
            if not cleaned:
                continue
            if not self._looks_like_action_item(lowered):
                continue
            title = self._normalize_action_title(cleaned)
            if not title:
                continue
            dedupe_key = title.lower()
            if dedupe_key in seen_titles:
                continue
            seen_titles.add(dedupe_key)
            items.append(
                ExtractedActionItem(
                    meeting_id=meeting_id,
                    title=title[:120],
                    details=cleaned,
                    due_hint=self._extract_due_hint_from_text(cleaned),
                )
            )
        return items[:10]

    def _looks_like_action_item(self, lowered: str) -> bool:
        action_prefixes = (
            "todo",
            "action",
            "follow up",
            "follow-up",
            "need to",
            "needs to",
            "please",
            "send ",
            "review ",
            "prepare ",
            "finalize ",
            "schedule ",
            "share ",
            "update ",
            "create ",
        )
        return any(token in lowered for token in action_prefixes)

    def _normalize_action_title(self, text: str) -> str:
        normalized = re.sub(r"^(todo|action item|action|follow up|follow-up|please)\s*[:\-]?\s*", "", text, flags=re.IGNORECASE).strip()
        normalized = re.sub(r"\s+(by|before|tomorrow|morgen|next week|nächste woche|on\s+\d{4}-\d{2}-\d{2}).*$", "", normalized, flags=re.IGNORECASE)
        return normalized[:160].strip(" -:")

    def _extract_due_hint_from_text(self, text: str) -> str | None:
        lowered = text.lower()
        if "tomorrow" in lowered or "morgen" in lowered:
            return "tomorrow"
        if "next week" in lowered or "nächste woche" in lowered:
            return "next week"
        iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
        if iso_match:
            return iso_match.group(1)
        german_match = re.search(r"\b(\d{1,2}\.\d{1,2}\.\d{2,4})\b", text)
        if german_match:
            return german_match.group(1)
        weekday_match = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|montag|dienstag|mittwoch|donnerstag|freitag)\b", lowered)
        if weekday_match:
            return weekday_match.group(1)
        return None

    def recover_jobs(self) -> None:
        return None

    def _hydrate_artifact(self, artifact: TranscriptArtifact) -> TranscriptArtifact:
        if artifact.content or not artifact.transcript_path:
            return artifact
        path = Path(artifact.transcript_path)
        if not path.exists():
            return artifact
        try:
            content = self.artifacts.read_text(path)
        except Exception as exc:
            logger.warning("Failed to read artifact file %s: %s", path, exc)
            return artifact
        if artifact.artifact_type == "summary":
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(content)
                return artifact.model_copy(
                    update={
                        "content": str(payload.get("summary", "")),
                        "metadata": {
                            **artifact.metadata,
                            "decisions": payload.get("decisions", artifact.metadata.get("decisions", [])),
                            "reminders": payload.get("reminders", artifact.metadata.get("reminders", [])),
                        },
                    }
                )
        return artifact.model_copy(update={"content": content})

    def _parse_due_hint(self, due_hint: str | None) -> datetime | None:
        if not due_hint:
            return None
        text = due_hint.strip().lower()
        if not text:
            return None
        if "tomorrow" in text or "morgen" in text:
            return datetime.now() + timedelta(days=1)
        try:
            return datetime.fromisoformat(due_hint)
        except ValueError:
            pass
        match = re.search(r"(\d{4}-\d{2}-\d{2})", due_hint)
        if match:
            try:
                return datetime.fromisoformat(match.group(1))
            except ValueError:
                return None
        german = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", due_hint)
        if german:
            day, month, year = german.groups()
            year = f"20{year}" if len(year) == 2 else year
            try:
                return datetime(int(year), int(month), int(day))
            except ValueError:
                return None
        return None

    def _ensure_unlocked(self, action: str) -> None:
        self.platform.assert_profile_unlocked(self.profile.slug, "meetings", action)
