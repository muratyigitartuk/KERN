import asyncio
from datetime import datetime
from pathlib import Path

from app.backup import BackupService
from app.database import connect
from app.documents import DocumentService
from app.events import EventHub
from app.email_service import EmailService
from app.llm import Brain
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.orchestrator import KernOrchestrator
from app.platform import PlatformStore, connect_platform_db
from app.policy import PolicyEngine
from app.tools.calendar import CalendarService
from app.tools.tasks import TaskService
from app.tts import TTSService
from app.types import AuthContext, DocumentChunk, DocumentRecord, EmailDraft, EmailMessage, ExecutionPlan, PlanStep, ToolResult


class FakeTTSService(TTSService):
    def __init__(self) -> None:
        self.enabled = True
        self.spoken: list[tuple[str, bool]] = []

    def speak(self, text: str) -> None:
        self.spoken.append((text, False))

    def speak_and_wait(self, text: str) -> None:
        self.spoken.append((text, True))

    def stop(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class _FakeLLMClient:
    available = True

    async def chat(self, messages, **kwargs):
        return {"choices": [{"message": {"content": "LLM fallback."}}]}

    async def chat_stream(self, messages, **kwargs):
        yield "LLM "
        yield "fallback."


class _FailingRAGPipeline:
    async def answer_stream(self, *args, **kwargs):
        raise AssertionError("RAG should not have been called for this route.")
        yield  # pragma: no cover


def _create_profile(tmp_path: Path):
    platform = PlatformStore(connect_platform_db(tmp_path / "kern-system.db"))
    profile = platform.ensure_default_profile(
        profile_root=tmp_path / "profiles",
        backup_root=tmp_path / "backups",
        legacy_db_path=tmp_path / "legacy.db",
    )
    memory = MemoryRepository(connect(Path(profile.db_path)))
    local_data = LocalDataService(memory, "sir")
    return platform, profile, memory, local_data


def _create_profile_orchestrator(tmp_path: Path) -> tuple[KernOrchestrator, PlatformStore, object]:
    platform, profile, memory, local_data = _create_profile(tmp_path)
    calendar_service = CalendarService(local_data)
    documents = DocumentService(memory.connection, platform, profile)
    email_service = EmailService(memory.connection, platform, profile, local_data, calendar_service, documents)
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=calendar_service,
        document_service=documents,
        email_service=email_service,
        default_title="sir",
        platform_store=platform,
        active_profile=profile,
        backup_service=BackupService(),
    )
    return orchestrator, platform, profile


def _create_auth_context(platform: PlatformStore, profile) -> AuthContext:
    organization = platform.ensure_default_organization()
    user = platform.create_user(
        email="owner@example.com",
        display_name="Owner",
        organization_id=organization.id,
        auth_source="oidc",
        status="active",
    )
    platform.upsert_workspace_membership(user_id=user.id, workspace_slug=profile.slug, role="org_owner")
    session = platform.create_session(
        organization_id=organization.id,
        user_id=user.id,
        workspace_slug=profile.slug,
        auth_method="oidc",
    )
    context = platform.build_auth_context(session.id)
    assert context is not None
    return context


def test_orchestrator_updates_preferred_title(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    asyncio.run(orchestrator.initialize())
    asyncio.run(orchestrator.process_transcript("Call me Murat", trigger="manual_ui"))
    assert memory.get_value("preferences", "preferred_title") == "Murat"
    assert orchestrator.snapshot.conversation_turns[0].role == "user"
    assert orchestrator.snapshot.conversation_turns[-1].role == "assistant"


def test_orchestrator_speaks_before_spotify_action(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    fake_tts = FakeTTSService()
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=fake_tts,
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    async def fake_run(_request):
        from app.types import ToolResult

        return ToolResult(success=True, display_text="Spotify resumed.", spoken_text="", data={})

    asyncio.run(orchestrator.initialize())
    orchestrator.capabilities._product_posture = "personal"
    orchestrator.snapshot.capability_status = orchestrator.capabilities.available_descriptors()
    orchestrator.tools["play_spotify"].run = fake_run
    asyncio.run(orchestrator.process_transcript("Play some morning jazz", trigger="manual_ui"))

    assert fake_tts.spoken
    assert fake_tts.spoken[0] == ("Of course, sir.", True)


def test_orchestrator_blocks_overlapping_commands(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    fake_tts = FakeTTSService()
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=fake_tts,
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    asyncio.run(orchestrator.initialize())
    orchestrator.snapshot.action_in_progress = True
    turn = asyncio.run(orchestrator.process_transcript("Open Spotify", trigger="manual_ui"))
    assert "One moment" in turn.response_text


def test_orchestrator_respects_runtime_mute_for_tts(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    local_data.set_muted(True)
    fake_tts = FakeTTSService()
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=fake_tts,
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    asyncio.run(orchestrator.initialize())
    asyncio.run(orchestrator.process_transcript("What can you do?", trigger="manual_ui"))

    assert fake_tts.spoken == []


def test_orchestrator_uses_local_reasoning_before_llm_for_guidance(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    calendar_service = CalendarService(local_data)
    documents = DocumentService(memory.connection, platform, profile)
    email_service = EmailService(memory.connection, platform, profile, local_data, calendar_service, documents)
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None, llm_client=_FakeLLMClient()),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=calendar_service,
        document_service=documents,
        email_service=email_service,
        default_title="sir",
        platform_store=platform,
        active_profile=profile,
        backup_service=BackupService(),
    )
    context = _create_auth_context(platform, profile)
    memory.save_email_draft(
        EmailDraft(to=["client@example.com"], subject="Quarterly follow-up", body="Draft body", status="draft"),
    )

    async def _fail_llm_analysis(*args, **kwargs):
        raise AssertionError("LLM planner should not run before deterministic guidance.")

    orchestrator.brain._cognition.analyze_async = _fail_llm_analysis

    asyncio.run(orchestrator.initialize())
    turn = asyncio.run(
        orchestrator.process_transcript(
            "What should I follow up on next?",
            trigger="manual_ui",
            auth_context=context,
        )
    )

    assert turn.reasoning_source == "system_decision"
    assert turn.recommendation_id
    assert turn.workflow_type == "correspondence_follow_up"
    assert "I prepared this for you" in turn.response_text
    assert "Prepared evidence" in turn.response_text
    assert "Draft scaffold" in turn.response_text


def test_orchestrator_uses_packet_backed_llm_wording_when_requested(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    calendar_service = CalendarService(local_data)
    documents = DocumentService(memory.connection, platform, profile)
    email_service = EmailService(memory.connection, platform, profile, local_data, calendar_service, documents)
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None, llm_client=_FakeLLMClient()),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=calendar_service,
        document_service=documents,
        email_service=email_service,
        default_title="sir",
        platform_store=platform,
        active_profile=profile,
        backup_service=BackupService(),
    )
    context = _create_auth_context(platform, profile)
    memory.save_email_draft(
        EmailDraft(to=["client@example.com"], subject="Quarterly follow-up", body="Draft body", status="draft"),
    )

    async def _fail_llm_analysis(*args, **kwargs):
        raise AssertionError("LLM planner should not run before packet-backed wording.")

    orchestrator.brain._cognition.analyze_async = _fail_llm_analysis

    asyncio.run(orchestrator.initialize())
    turn = asyncio.run(
        orchestrator.process_transcript(
            "Draft the follow-up email for me",
            trigger="manual_ui",
            auth_context=context,
        )
    )

    assert turn.reasoning_source == "llm_generated_wording"
    assert turn.recommendation_id
    assert "rewrote the wording" in turn.response_text
    assert "LLM fallback." in turn.response_text


def test_orchestrator_routes_document_requests_through_grounded_packet_first(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    calendar_service = CalendarService(local_data)
    documents = DocumentService(memory.connection, platform, profile)
    email_service = EmailService(memory.connection, platform, profile, local_data, calendar_service, documents)
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=calendar_service,
        document_service=documents,
        email_service=email_service,
        default_title="sir",
        platform_store=platform,
        active_profile=profile,
        backup_service=BackupService(),
    )
    context = _create_auth_context(platform, profile)
    record = DocumentRecord(
        id="doc-1",
        profile_slug=profile.slug,
        organization_id=context.organization_id,
        workspace_id=profile.workspace_id,
        actor_user_id=context.user_id,
        title="deep-research-report",
        source="upload",
        file_type="md",
        file_path=str(tmp_path / "deep-research-report.md"),
        category="research",
        classification="internal",
        data_class="operational",
        tags=["research"],
    )
    memory.upsert_document_record(
        record,
        chunks=[
            DocumentChunk(document_id=record.id, chunk_index=0, text="# Important sections\nKey section one with operational risk."),
            DocumentChunk(document_id=record.id, chunk_index=1, text="# Evidence\nThe report cites a deadline and a payment penalty."),
        ],
        metadata={"classification": "internal", "data_class": "operational"},
    )

    async def _fail_llm_analysis(*args, **kwargs):
        raise AssertionError("LLM planner should not run before deterministic document guidance.")

    orchestrator.brain._cognition.analyze_async = _fail_llm_analysis

    asyncio.run(orchestrator.initialize())
    turn = asyncio.run(
        orchestrator.process_transcript(
            "cite this PDF's important sections",
            trigger="manual_ui",
            auth_context=context,
        )
    )

    assert turn.reasoning_source == "system_decision"
    assert "grounded document packet" in turn.response_text
    assert "Citations:" in turn.response_text


def test_orchestrator_can_use_llm_to_rewrite_grounded_document_packets(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    calendar_service = CalendarService(local_data)
    documents = DocumentService(memory.connection, platform, profile)
    email_service = EmailService(memory.connection, platform, profile, local_data, calendar_service, documents)
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None, llm_client=_FakeLLMClient()),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=calendar_service,
        document_service=documents,
        email_service=email_service,
        default_title="sir",
        platform_store=platform,
        active_profile=profile,
        backup_service=BackupService(),
    )
    context = _create_auth_context(platform, profile)
    record = DocumentRecord(
        id="doc-2",
        profile_slug=profile.slug,
        organization_id=context.organization_id,
        workspace_id=profile.workspace_id,
        actor_user_id=context.user_id,
        title="project-summary",
        source="upload",
        file_type="md",
        file_path=str(tmp_path / "project-summary.md"),
        category="research",
        classification="internal",
        data_class="operational",
        tags=["research"],
    )
    memory.upsert_document_record(
        record,
        chunks=[
            DocumentChunk(document_id=record.id, chunk_index=0, text="# Summary\nThis file explains the main implementation risks and next actions."),
            DocumentChunk(document_id=record.id, chunk_index=1, text="# Next actions\nFollow up with the supplier and resolve the payment deadline."),
        ],
        metadata={"classification": "internal", "data_class": "operational"},
    )

    async def _fail_llm_analysis(*args, **kwargs):
        raise AssertionError("LLM planner should not run before grounded document wording.")

    orchestrator.brain._cognition.analyze_async = _fail_llm_analysis

    asyncio.run(orchestrator.initialize())
    turn = asyncio.run(
        orchestrator.process_transcript(
            "summarize this PDF for me",
            trigger="manual_ui",
            auth_context=context,
        )
    )

    assert turn.reasoning_source == "llm_generated_wording"
    assert "grounded document packet" in turn.response_text
    assert "LLM fallback." in turn.response_text
    assert "Grounded citations:" in turn.response_text


def test_orchestrator_routes_thread_questions_through_freeform_packet_first(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    calendar_service = CalendarService(local_data)
    documents = DocumentService(memory.connection, platform, profile)
    email_service = EmailService(memory.connection, platform, profile, local_data, calendar_service, documents)
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=calendar_service,
        document_service=documents,
        email_service=email_service,
        default_title="sir",
        platform_store=platform,
        active_profile=profile,
        backup_service=BackupService(),
    )
    context = _create_auth_context(platform, profile)
    memory.append_mailbox_message(
        EmailMessage(
            id="thread-1",
            subject="Quarterly follow-up",
            sender="client@example.com",
            recipients=["owner@example.com"],
            received_at=datetime.utcnow(),
            folder="INBOX",
        ),
        body_text="Can you send the latest timeline?",
    )
    memory.save_email_draft(
        EmailDraft(to=["client@example.com"], subject="Quarterly follow-up", body="Draft reply", status="draft"),
    )

    async def _fail_llm_analysis(*args, **kwargs):
        raise AssertionError("LLM planner should not run before deterministic freeform routing.")

    orchestrator.brain._cognition.analyze_async = _fail_llm_analysis

    asyncio.run(orchestrator.initialize())
    turn = asyncio.run(
        orchestrator.process_transcript(
            "what did we tell this client last time",
            trigger="manual_ui",
            auth_context=context,
        )
    )

    assert turn.reasoning_source == "system_decision"
    assert "grounded thread context" in turn.response_text.lower()
    outcomes = memory.list_interaction_outcomes(workspace_slug=profile.slug, actor_user_id=context.user_id, limit=10)
    assert outcomes
    assert outcomes[0].packet_type == "thread_context"
    assert outcomes[0].metadata.get("linked_entity_refs")


def test_orchestrator_asks_for_clarification_before_guessing_person_context(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    calendar_service = CalendarService(local_data)
    documents = DocumentService(memory.connection, platform, profile)
    email_service = EmailService(memory.connection, platform, profile, local_data, calendar_service, documents)
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=calendar_service,
        document_service=documents,
        email_service=email_service,
        default_title="sir",
        platform_store=platform,
        active_profile=profile,
        backup_service=BackupService(),
    )
    context = _create_auth_context(platform, profile)
    for idx, address in enumerate(("alice@example.com", "alice@vendor.example"), start=1):
        memory.append_mailbox_message(
            EmailMessage(
                id=f"alice-{idx}",
                subject="Account review",
                sender=address,
                recipients=["owner@example.com"],
                received_at=datetime.utcnow(),
                folder="INBOX",
            ),
            body_text="Review request",
        )

    async def _fail_llm_analysis(*args, **kwargs):
        raise AssertionError("LLM planner should not run before clarification.")

    orchestrator.brain._cognition.analyze_async = _fail_llm_analysis

    asyncio.run(orchestrator.initialize())
    turn = asyncio.run(
        orchestrator.process_transcript(
            "what matters for alice right now",
            trigger="manual_ui",
            auth_context=context,
        )
    )

    assert turn.reasoning_source == "system_decision"
    assert "clearer" in turn.response_text.lower() or "multiple plausible" in turn.response_text.lower()
    assert orchestrator._pending_interaction is not None


def test_orchestrator_can_disable_llm_fallback_for_scheduler_prompts(tmp_path: Path):
    orchestrator, platform, profile = _create_profile_orchestrator(tmp_path)
    orchestrator.brain = Brain(None, llm_client=_FakeLLMClient())
    context = _create_auth_context(platform, profile)

    async def _fail_llm_analysis(*args, **kwargs):
        raise AssertionError("LLM planner should stay disabled when local-only mode is requested.")

    orchestrator.brain._cognition.analyze_async = _fail_llm_analysis

    asyncio.run(orchestrator.initialize())
    turn = asyncio.run(
        orchestrator.process_transcript(
            "Unstructured scheduled prompt",
            trigger="scheduler",
            auth_context=context,
            allow_llm_fallback=False,
        )
    )

    assert turn.reasoning_source == "system_decision"
    assert "deterministic local reasoning alone" in turn.response_text


def test_orchestrator_runs_focus_routine(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    fake_tts = FakeTTSService()
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=fake_tts,
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    asyncio.run(orchestrator.initialize())
    turn = asyncio.run(orchestrator.process_transcript("Run the focus routine", trigger="manual_ui"))

    assert "focus routine" in turn.response_text.lower() or "focus routine" in orchestrator.snapshot.last_action.lower()
    assert local_data.list_pending_reminders(limit=10)


def test_orchestrator_can_reset_conversation(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    asyncio.run(orchestrator.initialize())
    asyncio.run(orchestrator.process_transcript("What can you do?", trigger="manual_ui"))

    assert orchestrator.snapshot.conversation_turns

    orchestrator.reset_conversation()

    assert orchestrator.snapshot.conversation_turns == []
    assert orchestrator.snapshot.pending_confirmation is None
    assert orchestrator.snapshot.last_action == "Started a new conversation."


def test_orchestrator_uses_follow_up_clarification_context(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    asyncio.run(orchestrator.initialize())
    first = asyncio.run(orchestrator.process_transcript("Remind me to stretch", trigger="manual_ui"))
    second = asyncio.run(orchestrator.process_transcript("in 20 minutes", trigger="manual_ui"))

    assert "clarify when" in first.response_text.lower()
    assert "reminder" in second.response_text.lower()
    assert orchestrator.snapshot.pending_confirmation is None


def test_orchestrator_confirmation_preserves_original_utterance_and_trigger(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    captured_requests = []

    async def fake_open(request):
        captured_requests.append(request)
        return ToolResult(status="attempted", display_text="Sent a launch request for vscode.")

    asyncio.run(orchestrator.initialize())
    orchestrator.tools["open_app"].run = fake_open
    asyncio.run(orchestrator.process_transcript("Open VS Code", trigger="manual_ui"))
    asyncio.run(orchestrator.confirm_pending(True))

    assert captured_requests
    assert captured_requests[0].user_utterance == "Open VS Code"
    assert captured_requests[0].trigger_source == "manual_ui"
    receipts = memory.list_execution_receipts(limit=1)
    assert receipts[0].original_utterance == "Open VS Code"
    assert receipts[0].trigger_source == "manual_ui"


def test_orchestrator_handles_unknown_capability_without_crashing(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    asyncio.run(orchestrator.initialize())
    plan = ExecutionPlan(
        source="planner",
        summary="Do something unsupported.",
        steps=[PlanStep(capability_name="unknown_capability", arguments={}, reason="test")],
        confidence=0.5,
    )
    receipts, reply = asyncio.run(orchestrator._execute_plan(plan, "Do something unsupported", "manual_ui"))

    assert receipts
    assert receipts[0].status == "failed"
    assert "unknown capability" in reply.lower()


def test_orchestrator_blocks_unavailable_capability_with_note(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    asyncio.run(orchestrator.initialize())
    orchestrator.capabilities.is_available = lambda _name: (False, "Desktop launching disabled for this test.")
    plan = ExecutionPlan(
        source="planner",
        summary="Attempt something unavailable.",
        steps=[PlanStep(capability_name="open_app", arguments={"app": "code"}, reason="test")],
        confidence=0.5,
    )

    receipts, reply = asyncio.run(orchestrator._execute_plan(plan, "Open VS Code", "manual_ui"))

    assert receipts
    assert receipts[0].status == "failed"
    assert "disabled for this test" in receipts[0].suggested_follow_up.lower()
    assert "disabled for this test" in reply.lower()


def test_orchestrator_refresh_context_snapshot_uses_dirty_cache(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    build_calls = {"context": 0, "capabilities": 0, "receipts": 0}
    original_context_build = orchestrator.context_assembler.build
    original_available_descriptors = orchestrator.capabilities.available_descriptors
    original_list_receipts = memory.list_execution_receipts

    def count_context():
        build_calls["context"] += 1
        return original_context_build()

    def count_capabilities():
        build_calls["capabilities"] += 1
        return original_available_descriptors()

    def count_receipts(limit: int = 20):
        build_calls["receipts"] += 1
        return original_list_receipts(limit=limit)

    orchestrator.context_assembler.build = count_context
    orchestrator.capabilities.available_descriptors = count_capabilities
    memory.list_execution_receipts = count_receipts

    asyncio.run(orchestrator.initialize())
    first_counts = dict(build_calls)

    orchestrator.refresh_context_snapshot()
    second_counts = dict(build_calls)

    assert second_counts == first_counts

    orchestrator.mark_dirty("context", "capabilities", "receipts")
    orchestrator.refresh_context_snapshot()

    assert build_calls["context"] == first_counts["context"] + 1
    assert build_calls["capabilities"] == first_counts["capabilities"] + 1
    assert build_calls["receipts"] == first_counts["receipts"] + 1


def test_orchestrator_handles_runtime_snapshot_request_without_chat_fallback(tmp_path: Path):
    orchestrator, _, _ = _create_profile_orchestrator(tmp_path)

    asyncio.run(orchestrator.initialize())
    turn = asyncio.run(orchestrator.process_transcript("Give me a quick runtime snapshot summary", trigger="manual_ui"))

    assert turn.plan is not None
    assert turn.plan.steps
    assert turn.plan.steps[0].capability_name == "read_runtime_snapshot"
    assert "active profile" in turn.response_text.lower()


def test_orchestrator_creates_backup_via_chat_tool(tmp_path: Path):
    orchestrator, _, profile = _create_profile_orchestrator(tmp_path)

    asyncio.run(orchestrator.initialize())
    first = asyncio.run(
        orchestrator.process_transcript(
            "Create an encrypted backup named before-weekend-checkpoint with password secret-passphrase",
            trigger="manual_ui",
        )
    )
    asyncio.run(orchestrator.confirm_pending(True))

    assert first.plan is not None
    assert first.plan.steps[0].capability_name == "create_backup"
    backups = sorted(Path(profile.backups_root).glob("*.kernbak"))
    assert backups
    assert "encrypted backup" in orchestrator.snapshot.response_text.lower()


def test_orchestrator_syncs_mailbox_from_natural_prompt(tmp_path: Path):
    orchestrator, _, _ = _create_profile_orchestrator(tmp_path)

    asyncio.run(orchestrator.initialize())

    sample_message = EmailMessage(
        id="msg-1",
        account_id="acct-1",
        message_id="<msg-1@example.com>",
        subject="Urgent: review the backup status today",
        sender="ops@example.com",
        recipients=["kern@example.com"],
        received_at=datetime.utcnow(),
        has_attachments=False,
        folder="INBOX",
        body_preview="Please review this today.",
    )
    orchestrator.tools["sync_mailbox"].availability = lambda: (True, None)

    async def fake_sync(request):
        return ToolResult(
            status="observed",
            display_text="Mailbox synchronized. ops@example.com: Urgent: review the backup status today",
            spoken_text="I synchronized the mailbox.",
            data={"messages": [sample_message.model_dump(mode="json")]},
        )

    orchestrator.tools["sync_mailbox"].run = fake_sync
    first = asyncio.run(orchestrator.process_transcript("Sync my mailbox and tell me if anything urgent arrived today", trigger="manual_ui"))
    asyncio.run(orchestrator.confirm_pending(True))

    assert first.plan is not None
    assert first.plan.steps
    assert first.plan.steps[0].capability_name == "sync_mailbox"
    assert "mailbox synchronized" in orchestrator.snapshot.response_text.lower()


def test_orchestrator_returns_controlled_reply_for_unmapped_operational_request(tmp_path: Path):
    memory = MemoryRepository(connect(tmp_path / "kern.db"))
    local_data = LocalDataService(memory, "sir")
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=Brain(None),
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=CalendarService(local_data),
        default_title="sir",
    )

    asyncio.run(orchestrator.initialize())
    turn = asyncio.run(orchestrator.process_transcript("Export my audit timeline to CSV", trigger="manual_ui"))

    assert "couldn't map" in turn.response_text.lower()
    assert "Good to hear from you" not in turn.response_text


def test_orchestrator_skips_rag_when_router_disables_it(tmp_path: Path):
    platform, profile, memory, local_data = _create_profile(tmp_path)
    calendar_service = CalendarService(local_data)
    documents = DocumentService(memory.connection, platform, profile)
    email_service = EmailService(memory.connection, platform, profile, local_data, calendar_service, documents)
    brain = Brain(
        None,
        llm_client=_FakeLLMClient(),
        rag_pipeline=_FailingRAGPipeline(),
    )
    orchestrator = KernOrchestrator(
        event_hub=EventHub(),
        memory=memory,
        brain=brain,
        local_data=local_data,
        policy=PolicyEngine(),
        tts=TTSService(enabled=False),
        task_service=TaskService(local_data),
        calendar_service=calendar_service,
        document_service=documents,
        email_service=email_service,
        default_title="sir",
        platform_store=platform,
        active_profile=profile,
        backup_service=BackupService(),
    )

    asyncio.run(orchestrator.initialize())
    reply = asyncio.run(
        orchestrator._try_llm_chat(
            "What can you actually help me with in this workspace?",
            "sir",
            context_summary=orchestrator.snapshot.active_context_summary,
        )
    )

    assert reply == "LLM fallback."
    assert orchestrator.snapshot.model_route.used_rag is False
