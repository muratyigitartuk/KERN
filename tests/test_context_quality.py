from __future__ import annotations

import asyncio
from pathlib import Path

from app.context import ContextAssembler
from app.current_context import CurrentContextService
from app.database import connect
from app.dialogue import DialogueStateStore
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.model_router import ModelRouter
from app.tools.system_state import ReadCurrentContextTool
from app.types import (
    ClipboardSnapshot,
    CurrentContextSnapshot,
    ForegroundWindowSnapshot,
    MediaContextSnapshot,
    ProfileSummary,
    RuntimeSnapshot,
    ToolRequest,
)


class _FakeWindowClient:
    def snapshot(self):
        return ForegroundWindowSnapshot(title="Project Plan - Visual Studio Code", process_id=42, process_name="Code.exe")


class _FakeClipboardClient:
    def snapshot(self):
        return ClipboardSnapshot(has_text=True, excerpt="Finish the enterprise rollout checklist.", char_count=39)


class _FakeMediaClient:
    @property
    def available(self):
        return True

    def spotify_session(self):
        class _Session:
            source_app = "Spotify.exe"
            status = "Playing"
            title = "Morning Jazz"
            artist = "KERN Trio"
            album = "Daily Focus"

        return _Session()


class _FakePlatform:
    def assert_profile_unlocked(self, *args, **kwargs):
        return None

    def record_audit(self, *args, **kwargs):
        return None


def test_context_assembler_includes_current_context_summary(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    dialogue = DialogueStateStore(memory)
    service = CurrentContextService(
        window_client=_FakeWindowClient(),
        clipboard_client=_FakeClipboardClient(),
        media_client=_FakeMediaClient(),
        window_enabled=True,
        clipboard_enabled=True,
        media_enabled=True,
    )
    assembler = ContextAssembler(memory, local_data, dialogue, current_context=service)

    summary = assembler.build()

    assert summary.current_context is not None
    assert summary.current_context.window is not None
    assert "Foreground window" in " ".join(summary.summary_lines)
    assert summary.system_signals["active_window_process"] == "Code.exe"
    assert summary.system_signals["clipboard_present"] is True


def test_read_current_context_tool_reports_window_clipboard_and_media():
    snapshot = RuntimeSnapshot(
        current_context=CurrentContextSnapshot(
            window=ForegroundWindowSnapshot(title="Inbox - Outlook", process_id=77, process_name="OUTLOOK.EXE"),
            clipboard=ClipboardSnapshot(has_text=True, excerpt="Call ACME back tomorrow.", char_count=24),
            media=MediaContextSnapshot(title="Morning Jazz", artist="KERN Trio", status="Playing", source_app="Spotify.exe"),
            sources={"window": True, "clipboard": True, "media": True},
        )
    )
    profile = ProfileSummary(
        slug="default",
        title="Primary profile",
        profile_root=".",
        db_path="kern.db",
        documents_root="documents",
        attachments_root="attachments",
        archives_root="archives",
        meetings_root="meetings",
        backups_root="backups",
    )
    tool = ReadCurrentContextTool(lambda: snapshot, _FakePlatform(), profile)

    result = asyncio.run(
        tool.run(
            ToolRequest(
                tool_name="read_current_context",
                arguments={},
                user_utterance="show current context",
                reason="test",
                trigger_source="manual_ui",
            )
        )
    )

    assert "Foreground window" in result.display_text
    assert "Clipboard" in result.display_text
    assert "Morning Jazz" in result.display_text


def test_model_router_prefers_deep_for_complex_rag_queries():
    router = ModelRouter(mode="auto", fast_model="fast.gguf", deep_model="deep.gguf")
    decision = router.choose(
        "Compare these documents and explain the most important differences in the contract terms.",
        rag_candidate=True,
        llm_available=True,
    )

    assert decision.selected_mode == "deep"
    assert decision.requested_model == "deep"


def test_model_router_prefers_fast_for_short_conversational_requests():
    router = ModelRouter(mode="auto", fast_model="fast.gguf", deep_model="deep.gguf")
    decision = router.choose("Hi KERN", llm_available=True)

    assert decision.selected_mode == "fast"
    assert decision.requested_model == "fast"
