import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from app.database import connect
from app.llm import Brain
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.tools.memory_tools import RecallMemoryTool
from app.types import ActiveContextSummary, CalendarEventSummary, ExecutionReceipt, ToolRequest


def test_brain_builds_multistep_plan_for_compound_request():
    brain = Brain(None, local_mode_enabled=True, cognition_backend="hybrid")
    context = ActiveContextSummary(preferred_title="sir")

    analysis = brain.analyze_intent(
        "Open VS Code and start focus mode",
        context_summary=context,
        available_capabilities=["open_app", "focus_mode"],
    )

    assert len(analysis.execution_plan.steps) == 2
    assert analysis.execution_plan.steps[0].capability_name == "open_app"
    assert analysis.execution_plan.steps[1].capability_name == "focus_mode"


def test_brain_resolves_after_meeting_reminder_with_context():
    brain = Brain(None, local_mode_enabled=True, cognition_backend="hybrid")
    event = CalendarEventSummary(
        title="Team sync",
        starts_at=datetime.now() + timedelta(hours=1),
        ends_at=datetime.now() + timedelta(hours=2),
    )
    context = ActiveContextSummary(preferred_title="sir", events=[event])

    analysis = brain.analyze_intent(
        "Remind me to send the deck after the meeting",
        context_summary=context,
        available_capabilities=["create_reminder"],
    )

    assert analysis.execution_plan.steps
    step = analysis.execution_plan.steps[0]
    assert step.capability_name == "create_reminder"
    assert "due_at" in step.arguments


def test_memory_tracks_facts_open_loops_and_receipts(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    repo.upsert_fact("preferred editor", "VS Code")
    loop_id = repo.create_open_loop("Send deck", source="assistant")
    repo.append_execution_receipt(
        ExecutionReceipt(
            capability_name="remember_fact",
            status="observed",
            message="Stored memory.",
            evidence=["Fact written."],
        )
    )

    facts = repo.search_facts("editor")
    loops = repo.list_open_loops()
    receipts = repo.list_execution_receipts(limit=5)

    assert facts
    assert facts[0].value == "VS Code"
    assert any(loop.id == loop_id for loop in loops)
    assert receipts
    assert receipts[0].capability_name == "remember_fact"


def test_brain_preserves_multi_clause_memory_request():
    brain = Brain(None, local_mode_enabled=True, cognition_backend="hybrid")
    context = ActiveContextSummary(preferred_title="sir")

    analysis = brain.analyze_intent(
        "Remember that my preferred editor is VS Code and I prefer concise answers.",
        context_summary=context,
        available_capabilities=["remember_fact"],
    )

    assert len(analysis.execution_plan.steps) == 1
    step = analysis.execution_plan.steps[0]
    assert step.capability_name == "remember_fact"
    assert len(step.arguments["facts"]) == 2


def test_memory_recall_normalizes_working_preferences_query(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    data = LocalDataService(repo, "sir")
    data.remember_fact("preferred editor", "VS Code")
    data.remember_fact("response style", "concise answers")
    tool = RecallMemoryTool(data)

    result = asyncio.run(
        tool.run(
            ToolRequest(
                tool_name="recall_memory",
                arguments={"query": "my working preferences"},
                user_utterance="What do you remember about my working preferences?",
                reason="test",
            )
        )
    )

    assert "preferred editor" in result.display_text.lower()
    assert "response style" in result.display_text.lower()


def test_brain_preserves_full_mailbox_sync_request_when_second_clause_is_contextual():
    brain = Brain(None, local_mode_enabled=True, cognition_backend="hybrid")
    context = ActiveContextSummary(preferred_title="sir")

    analysis = brain.analyze_intent(
        "Sync my mailbox and tell me if anything urgent arrived today.",
        context_summary=context,
        available_capabilities=["sync_mailbox"],
    )

    assert len(analysis.execution_plan.steps) == 1
    assert analysis.execution_plan.steps[0].arguments["urgent_only"] is True
    assert analysis.execution_plan.steps[0].arguments["today_only"] is True


def test_brain_preserves_full_backup_listing_request_when_second_clause_is_contextual():
    brain = Brain(None, local_mode_enabled=True, cognition_backend="hybrid")
    context = ActiveContextSummary(preferred_title="sir")

    analysis = brain.analyze_intent(
        "List available backups and tell me which one is newest.",
        context_summary=context,
        available_capabilities=["list_backups"],
    )

    assert len(analysis.execution_plan.steps) == 1
    assert analysis.execution_plan.steps[0].arguments["newest_only"] is True
