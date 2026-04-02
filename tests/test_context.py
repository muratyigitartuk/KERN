from datetime import datetime, timedelta
from pathlib import Path

from app.context import ContextAssembler
from app.database import connect
from app.dialogue import DialogueStateStore
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.types import ClipboardSnapshot, CurrentContextSnapshot, ForegroundWindowSnapshot


class _FakeCurrentContext:
    def collect(self) -> CurrentContextSnapshot:
        return CurrentContextSnapshot(
            window=ForegroundWindowSnapshot(title="Inbox - Outlook", process_name="outlook.exe"),
            clipboard=ClipboardSnapshot(has_text=True, excerpt="Quarterly numbers", char_count=17),
            sources={"window": True, "clipboard": True, "media": False},
        )


def test_context_assembler_includes_current_context_sources(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(repo, "sir")
    dialogue = DialogueStateStore(repo)
    local_data.create_task("Review contract")
    local_data.create_reminder("Reply to ACME", datetime.utcnow() + timedelta(hours=2))

    assembler = ContextAssembler(repo, local_data, dialogue, current_context=_FakeCurrentContext())
    summary = assembler.build()

    assert any("Foreground window" in line for line in summary.summary_lines)
    assert any("Clipboard has text" in line for line in summary.summary_lines)
    assert summary.system_signals["clipboard_present"] is True
    assert "window" in summary.system_signals["context_sources"]
