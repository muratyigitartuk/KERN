from __future__ import annotations

from app.current_context import CurrentContextService
from app.dialogue import DialogueStateStore
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.types import ActiveContextSummary


class ContextAssembler:
    def __init__(
        self,
        memory: MemoryRepository,
        local_data: LocalDataService,
        dialogue_state: DialogueStateStore,
        current_context: CurrentContextService | None = None,
    ) -> None:
        self.memory = memory
        self.local_data = local_data
        self.dialogue_state = dialogue_state
        self.current_context = current_context

    def build(self) -> ActiveContextSummary:
        memory_scope = self.local_data.memory_scope()
        facts = self.local_data.list_facts(limit=5) if memory_scope in {"profile", "profile_plus_archive"} else []
        open_loops = self.local_data.list_open_loops(limit=5)
        reminders = self.local_data.list_pending_reminders(limit=5)
        tasks = self.local_data.list_pending_tasks()[:5]
        events = self.local_data.list_today_events()[:5]
        recent_dialogue = self.memory.list_recent_conversation_entries(limit=6)
        current_context = self.current_context.collect() if self.current_context else None

        summary_lines: list[str] = []
        if tasks:
            summary_lines.append(f"Top task: {tasks[0].title}.")
        if reminders:
            summary_lines.append(f"Pending reminders: {len(reminders)}.")
        if open_loops:
            summary_lines.append(f"Open commitments: {len(open_loops)}. Next: {open_loops[0].title}.")
        if events:
            summary_lines.append(f"Next event: {events[0].title}.")
        if facts:
            fact = facts[0]
            summary_lines.append(f"Remembered {fact.key}: {fact.value}.")
        if current_context and current_context.window and current_context.window.title:
            label = current_context.window.process_name or "foreground app"
            summary_lines.append(f"Foreground window: {label} - {current_context.window.title}.")
        if current_context and current_context.clipboard and current_context.clipboard.has_text:
            summary_lines.append(f"Clipboard has text ({current_context.clipboard.char_count} chars).")
        if current_context and current_context.sources:
            active_sources = [name for name, enabled in current_context.sources.items() if enabled]
            if active_sources:
                summary_lines.append(f"Current context sources: {', '.join(active_sources)}.")

        return ActiveContextSummary(
            preferred_title=self.local_data.preferred_title(),
            facts=facts,
            open_loops=open_loops,
            reminders=reminders,
            tasks=tasks,
            events=events,
            recent_dialogue=recent_dialogue,
            summary_lines=summary_lines,
            system_signals={
                "assistant_mode": self.local_data.assistant_mode(),
                "quiet_hours_active": self.local_data.quiet_hours_active(),
                "active_window_title": current_context.window.title if current_context and current_context.window else "",
                "active_window_process": current_context.window.process_name if current_context and current_context.window else "",
                "clipboard_present": bool(current_context and current_context.clipboard and current_context.clipboard.has_text),
                "context_sources": [name for name, enabled in (current_context.sources.items() if current_context else []) if enabled],
            },
            current_context=current_context,
        )
