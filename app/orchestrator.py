from __future__ import annotations

import asyncio
import json
import logging
import re
import time as perf_time
from datetime import datetime, time as clock_time, timezone
from pathlib import Path

from app.attention import AttentionManager
from app.capabilities import build_capability_registry
from app.planning import LlamaServerPlanner
from app.config import settings
from app.context import ContextAssembler
from app.dialogue import DialogueStateStore
from app.documents import DocumentService
from app.events import EventHub
from app.german_business import GermanBusinessService
from app.llm import Brain
from app.rag import NoRetrievalHitsError, RAGStreamResult
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.model_router import ModelRouter
from app.policy import PolicyEngine
from app.platform import PlatformStore
from app.reasoning import ReasoningService
from app.reminders import ReminderService
from app.retrieval import RetrievalService
from app.routines import RoutineService
from app.tools.calendar import CalendarService, TodayCalendarTool
from app.tools.documents import (
    BulkIngestTool,
    CompareDocumentsTool,
    ImportConversationArchiveTool,
    IngestDocumentTool,
    QuerySpreadsheetTool,
    SearchDocumentsTool,
    SetMemoryScopeTool,
    SummarizeDocumentTool,
)
from app.tools.scheduler_tools import CreateScheduleTool, ListSchedulesTool, ManageScheduleTool, WatchFolderTool
from app.tools.german_business import CreateAngebotTool, CreateDsgvoReminderTool, CreateRechnungTool, DraftBehoerdeLetterTool, TaxSupportTool
from app.tools.local_runtime import GenerateMorningBriefTool, ReadStatusTool, SetPreferenceTool
from app.tools.memory_tools import BuildTopicTimelineTool, RecallMemoryTool, RememberFactTool, SearchConversationHistoryTool
from app.tools.notes import ListNotesTool, NoteTool
from app.tools.reminders import CreateReminderTool, DismissReminderTool, SnoozeReminderTool
from app.tools.routines import RunRoutineTool
from app.tools.runtime_control import BrowserSearchTool, FocusModeTool, SystemStatusTool
from app.tools.system import OpenAppTool, OpenWebsiteTool
from app.tools.system_state import (
    CreateBackupTool,
    ExportAuditTrailTool,
    ListBackupsTool,
    ReadAuditEventsTool,
    ReadCurrentContextTool,
    ReadProfileSecurityTool,
    ReadRuntimeSnapshotTool,
    RestoreBackupTool,
)
from app.tools.sync_tools import SyncToTargetTool
from app.tools.tasks import CompleteTaskTool, CreateTaskTool, PendingTasksTool, TaskService
from app.tools.workspace import ReadFileExcerptTool, SearchFilesTool
from app.types import (
    ActionHistoryEntry,
    AssistantTurn,
    ConversationTurn,
    FreeformIntentRecord,
    ExecutionPlan,
    ExecutionReceipt,
    MorningBrief,
    PendingConfirmation,
    PendingInteraction,
    PersonContextPacket,
    ProfileSummary,
    RuntimeSnapshot,
    SecretRef,
    DocumentAnswerPacket,
    SuggestedDraftRecord,
    ThreadContextPacket,
    ToolRequest,
    ToolResult,
)
from app.verification import VerificationService
from app.syncing import SyncService
from app.backup import BackupService

logger = logging.getLogger(__name__)

class _NullPlatformStore:
    """Stub platform store for personal/test use only. Logs a warning on first use."""

    def __init__(self) -> None:
        logger.warning("Audit logging disabled Ã¢â‚¬â€ running without platform store")

    def record_audit(self, *args, **kwargs) -> None:
        return None

    def create_job(self, *args, **kwargs):
        class _Job:
            id = "noop-job"

        return _Job()

    def update_job(self, *args, **kwargs) -> None:
        return None

    def update_checkpoint(self, *args, **kwargs) -> None:
        return None

    def upsert_backup_target(self, *args, **kwargs) -> None:
        return None

    def is_profile_locked(self, *args, **kwargs) -> bool:
        return False

    def assert_profile_unlocked(self, *args, **kwargs) -> None:
        return None

    def store_secret(self, profile_slug: str, name: str, value: str) -> SecretRef:
        return SecretRef(id=f"env:{name}", profile_slug=profile_slug, name=name)

    def resolve_secret(self, secret_ref: str | None, profile_slug: str | None = None) -> str | None:
        return None

    def list_jobs(self, *args, **kwargs) -> list:
        return []

    def list_audit_events(self, *args, **kwargs) -> list:
        return []

    def list_backup_targets(self, *args, **kwargs) -> list:
        return []

    def get_profile_security_state(self, *args, **kwargs) -> dict[str, object]:
        return {
            "db_encryption_enabled": False,
            "db_encryption_mode": "off",
            "artifact_encryption_migration_state": "not enabled",
        }


class KernOrchestrator:
    MAX_CONVERSATION_TURNS = 40
    MAX_ACTION_HISTORY = 24
    MAX_RECEIPT_CACHE = 6
    PLAIN_CHAT_LLM_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        event_hub: EventHub,
        memory: MemoryRepository,
        brain: Brain,
        local_data: LocalDataService,
        policy: PolicyEngine,
        task_service: TaskService,
        calendar_service: CalendarService,
        default_title: str,
        document_service: DocumentService | None = None,
        german_business_service: GermanBusinessService | None = None,
        sync_service: SyncService | None = None,
        dialogue_state: DialogueStateStore | None = None,
        reminder_service: ReminderService | None = None,
        platform_store: PlatformStore | None = None,
        active_profile: ProfileSummary | None = None,
        backup_service: BackupService | None = None,
        scheduler_service=None,
        file_watcher=None,
        current_context_service=None,
    ) -> None:
        self.event_hub = event_hub
        self.memory = memory
        self.brain = brain
        self.local_data = local_data
        self.policy = policy
        self.default_title = default_title
        self.dialogue_state = dialogue_state or DialogueStateStore(memory)
        self.reminder_service = reminder_service or ReminderService(local_data)
        self.routine_service = RoutineService(local_data)
        self.retrieval = RetrievalService(memory)
        if platform_store is None and settings.product_posture == "production":
            raise RuntimeError(
                "Production posture requires a PlatformStore for audit logging. "
                "Set KERN_PRODUCT_POSTURE=personal or provide a platform_store."
            )
        null_platform = platform_store or _NullPlatformStore()
        default_profile = active_profile or ProfileSummary(
            slug="default",
            title="Default profile",
            profile_root=str((Path.cwd() / ".kern" / "profiles" / "default").resolve()),
            db_path=str((Path.cwd() / ".kern" / "profiles" / "default" / "kern.db").resolve()),
            documents_root=str((Path.cwd() / ".kern" / "profiles" / "default" / "documents").resolve()),
            attachments_root=str((Path.cwd() / ".kern" / "profiles" / "default" / "attachments").resolve()),
            archives_root=str((Path.cwd() / ".kern" / "profiles" / "default" / "archives").resolve()),
            meetings_root=str((Path.cwd() / ".kern" / "profiles" / "default" / "meetings").resolve()),
            backups_root=str((Path.cwd() / ".kern" / "backups" / "default").resolve()),
            has_pin=False,
        )
        document_service = document_service or DocumentService(memory, Path(default_profile.documents_root), Path(default_profile.archives_root))
        german_business_service = german_business_service or GermanBusinessService(
            memory.connection,
            null_platform,  # type: ignore[arg-type]
            default_profile,
            local_data,
            document_service,
        )
        sync_service = sync_service or SyncService(memory, default_profile, platform=null_platform)  # type: ignore[arg-type]
        self.context_assembler = ContextAssembler(memory, local_data, self.dialogue_state, current_context=current_context_service)
        self.attention_manager = AttentionManager(local_data)
        self.verifier = VerificationService()
        self.platform = null_platform
        self.active_profile = default_profile
        self.document_service = document_service
        self.backup_service = backup_service or BackupService()
        self.scheduler_service = scheduler_service
        self.file_watcher = file_watcher
        self.model_router = ModelRouter(
            mode=settings.model_mode,
            fast_model=settings.fast_model_path,
            deep_model=settings.deep_model_path,
        )
        self.snapshot = RuntimeSnapshot(
            product_posture=settings.product_posture,
            cognition_backend=brain.cognition_backend,
            assistant_mode=self.local_data.assistant_mode(),
        )
        self.tools = self._build_tools(
            task_service,
            calendar_service,
            document_service,
            german_business_service,
            sync_service,
            self.backup_service,
        )
        self.capabilities = build_capability_registry(self.tools)
        if self.brain.llm_available:
            from app.tool_calling import ToolCallingBridge
            self._tool_bridge = ToolCallingBridge(self.capabilities)
            self.brain._cognition.server_planner = LlamaServerPlanner(
                self.brain.llm_client, self._tool_bridge
            )
        else:
            self._tool_bridge = None
        self._pending_plan: ExecutionPlan | None = None
        self._pending_interaction: PendingInteraction | None = None
        self._last_proactive_message: str = ""
        self._tool_timeout_seconds = 12.0
        self._dirty_flags: dict[str, bool] = {
            "conversation": True,
            "context": True,
            "capabilities": True,
            "receipts": True,
            "runtime": True,
        }
        self.snapshot.prompt_cache = self.model_router.cache_snapshot()
        self._render_versions: dict[str, int] = {
            "conversation": 0,
            "context": 0,
            "capabilities": 0,
            "receipts": 0,
            "runtime": 0,
        }
        self._last_context_refresh_at = 0.0
        self._last_capability_refresh_at = 0.0
        self._context_cache_ready = False
        self._capability_cache_ready = False
        self._receipt_cache_ready = False
        self._cached_context_summary = None
        self._cached_capabilities = []
        self._cached_receipts: list[ExecutionReceipt] = []
        self._sync_snapshot_metadata()

    def _build_tools(
        self,
        task_service: TaskService,
        calendar_service: CalendarService,
        document_service: DocumentService,
        german_business_service: GermanBusinessService,
        sync_service: SyncService,
        backup_service: BackupService,
    ):
        workspace_root = Path.cwd()
        return {
            "open_app": OpenAppTool(),
            "open_website": OpenWebsiteTool(),
            "browser_search": BrowserSearchTool(),
            "get_today_calendar": TodayCalendarTool(calendar_service),
            "get_pending_tasks": PendingTasksTool(task_service),
            "create_note": NoteTool(self.local_data),
            "list_notes": ListNotesTool(self.local_data),
            "create_task": CreateTaskTool(task_service),
            "complete_task": CompleteTaskTool(task_service),
            "set_preference": SetPreferenceTool(self.local_data),
            "read_status": ReadStatusTool(self.local_data),
            "generate_morning_brief": GenerateMorningBriefTool(self.local_data),
            "create_reminder": CreateReminderTool(self.reminder_service),
            "set_timer": CreateReminderTool(self.reminder_service),
            "snooze_reminder": SnoozeReminderTool(self.reminder_service),
            "dismiss_reminder": DismissReminderTool(self.reminder_service),
            "run_routine": RunRoutineTool(self.routine_service),
            "remember_fact": RememberFactTool(self.local_data),
            "recall_memory": RecallMemoryTool(self.local_data, document_service, self.retrieval),
            "focus_mode": FocusModeTool(self.local_data),
            "search_files": SearchFilesTool(workspace_root),
            "read_file_excerpt": ReadFileExcerptTool(workspace_root),
            "system_status": SystemStatusTool(),
            "ingest_document": IngestDocumentTool(document_service),
            "search_documents": SearchDocumentsTool(document_service),
            "import_conversation_archive": ImportConversationArchiveTool(document_service),
            "set_memory_scope": SetMemoryScopeTool(self.local_data),
            "bulk_ingest": BulkIngestTool(document_service),
            "compare_documents": CompareDocumentsTool(document_service, self.brain._rag),
            "summarize_document": SummarizeDocumentTool(document_service),
            "query_spreadsheet": QuerySpreadsheetTool(document_service),
            "create_schedule": CreateScheduleTool(lambda: self.scheduler_service),
            "list_schedules": ListSchedulesTool(lambda: self.scheduler_service),
            "manage_schedule": ManageScheduleTool(lambda: self.scheduler_service),
            "watch_folder": WatchFolderTool(lambda: self.file_watcher),
            "search_conversation_history": SearchConversationHistoryTool(self.memory),
            "build_topic_timeline": BuildTopicTimelineTool(self.memory),
            "create_angebot": CreateAngebotTool(german_business_service),
            "create_rechnung": CreateRechnungTool(german_business_service),
            "draft_behoerde_letter": DraftBehoerdeLetterTool(german_business_service),
            "create_dsgvo_reminders": CreateDsgvoReminderTool(german_business_service),
            "tax_support_query": TaxSupportTool(german_business_service),
            "sync_profile_data": SyncToTargetTool(sync_service),
            "create_backup": CreateBackupTool(backup_service, self.platform, self.active_profile),
            "list_backups": ListBackupsTool(backup_service, self.platform, self.active_profile),
            "restore_backup": RestoreBackupTool(backup_service, self.platform, self.active_profile),
            "read_audit_events": ReadAuditEventsTool(self.platform, self.active_profile),
            "export_audit_trail": ExportAuditTrailTool(self.platform, self.active_profile),
            "read_runtime_snapshot": ReadRuntimeSnapshotTool(lambda: self.snapshot, self.platform, self.active_profile),
            "read_current_context": ReadCurrentContextTool(lambda: self.snapshot, self.platform, self.active_profile),
            "read_profile_security": ReadProfileSecurityTool(self.platform, self.active_profile, self.local_data.memory_scope),
        }

    async def initialize(self) -> None:
        await asyncio.to_thread(self.memory.seed_defaults, include_demo_data=settings.seed_defaults)
        await asyncio.to_thread(self.memory.run_maintenance)
        self.refresh_context_snapshot(force=True)
        await self.broadcast(reason="initialize", force=True)

    def refresh_context_snapshot(self, force: bool = False) -> None:
        now = perf_time.monotonic()
        self.snapshot.assistant_mode = self.local_data.assistant_mode()
        if force or self._dirty_flags["context"] or (now - self._last_context_refresh_at) >= settings.context_refresh_seconds:
            started = perf_time.perf_counter()
            self._cached_context_summary = self.context_assembler.build()
            self._context_cache_ready = True
            self.snapshot.active_context_summary = self._cached_context_summary
            self.snapshot.current_context = self._cached_context_summary.current_context if self._cached_context_summary else None
            self.snapshot.loop_metrics.last_context_refresh_ms = round((perf_time.perf_counter() - started) * 1000, 2)
            self._last_context_refresh_at = now
            self._dirty_flags["context"] = False
        elif self._context_cache_ready:
            self.snapshot.active_context_summary = self._cached_context_summary
            self.snapshot.current_context = self._cached_context_summary.current_context if self._cached_context_summary else None

        if force or self._dirty_flags["capabilities"] or (now - self._last_capability_refresh_at) >= settings.capability_refresh_seconds:
            started = perf_time.perf_counter()
            self._cached_capabilities = self.capabilities.available_descriptors()
            self._capability_cache_ready = True
            self.snapshot.capability_status = self._cached_capabilities
            self.snapshot.loop_metrics.last_capability_refresh_ms = round((perf_time.perf_counter() - started) * 1000, 2)
            self._last_capability_refresh_at = now
            self._dirty_flags["capabilities"] = False
        elif self._capability_cache_ready:
            self.snapshot.capability_status = self._cached_capabilities

        if force or self._dirty_flags["receipts"] or not self._receipt_cache_ready:
            started = perf_time.perf_counter()
            self._cached_receipts = self.memory.list_execution_receipts(limit=self.MAX_RECEIPT_CACHE)
            self._receipt_cache_ready = True
            self.snapshot.last_receipts = self._cached_receipts
            self.snapshot.loop_metrics.last_receipt_refresh_ms = round((perf_time.perf_counter() - started) * 1000, 2)
            self._dirty_flags["receipts"] = False
        elif self._receipt_cache_ready:
            self.snapshot.last_receipts = self._cached_receipts

        if self.snapshot.last_receipts:
            last = self.snapshot.last_receipts[0]
            self.snapshot.verification_state = f"{last.capability_name}: {last.status}"
        else:
            self.snapshot.verification_state = "No verified actions yet."
        self._sync_snapshot_metadata()

    async def broadcast(self, reason: str = "update", force: bool = False) -> None:
        self.snapshot.last_snapshot_reason = reason
        self.refresh_context_snapshot(force=force)
        for flag in self._dirty_flags:
            self._dirty_flags[flag] = False
        self._sync_snapshot_metadata()
        await self.event_hub.publish({"type": "snapshot", "payload": self.snapshot.model_dump(mode="json")})

    def has_pending_snapshot_work(self) -> bool:
        return any(self._dirty_flags.values())

    def mark_dirty(self, *flags: str) -> None:
        for flag in flags:
            if flag not in self._dirty_flags:
                continue
            self._dirty_flags[flag] = True
            self._render_versions[flag] += 1
        self._sync_snapshot_metadata()

    def _sync_snapshot_metadata(self) -> None:
        self.snapshot.dirty_flags = dict(self._dirty_flags)
        self.snapshot.render_keys.conversation = str(self._render_versions["conversation"])
        self.snapshot.render_keys.context = str(self._render_versions["context"])
        self.snapshot.render_keys.capabilities = str(self._render_versions["capabilities"])
        self.snapshot.render_keys.receipts = str(self._render_versions["receipts"])
        self.snapshot.render_keys.runtime = str(self._render_versions["runtime"])
        self.snapshot.last_model_route = self.snapshot.model_route
        self.snapshot.prompt_cache = self.model_router.cache_snapshot()

    async def _maybe_morning_prompt(self) -> None:
        now = datetime.now()
        if not (clock_time(5, 0) <= now.time() <= clock_time(11, 0)):
            self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
            self.snapshot.assistant_mode = self.local_data.assistant_mode()
            self.mark_dirty("runtime")
            await self.broadcast(reason="silence_timeout")
            return
        date_key = now.strftime("%Y-%m-%d")
        if await asyncio.to_thread(self.memory.has_morning_greeting, date_key):
            self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
            self.snapshot.last_action = "Morning greeting already delivered."
            self.mark_dirty("runtime")
            await self.broadcast(reason="silence_timeout")
            return
        await asyncio.to_thread(self.memory.mark_morning_greeting, date_key)
        title = self.local_data.preferred_title()
        text = f"Good morning, {title}. Would you like me to play something to start the day?"
        await self._store_response_text(text)
        self.snapshot.assistant_state = "muted" if self.local_data.muted() else "responding"
        self.snapshot.response_text = text
        self.snapshot.last_action = "Morning greeting delivered."
        self.snapshot.assistant_mode = "proactive"
        self.append_turn("system", text, kind="proactive")
        self.mark_dirty("context", "runtime")
        await self.broadcast(reason="proactive_prompt")

    def _reasoning_service(self) -> ReasoningService:
        return ReasoningService(
            self.platform,  # type: ignore[arg-type]
            self.memory,
            self.active_profile,
            scheduler_service=self.scheduler_service,
        )

    def _reasoning_context(self, workspace_context: object | None) -> tuple[str | None, str, str | None]:
        workspace_slug = (
            workspace_context.workspace_slug
            if workspace_context is not None and workspace_context.workspace_slug
            else self.active_profile.slug
        )
        organization_id = workspace_context.organization_id if workspace_context is not None else None
        actor_user_id = workspace_context.user_id if workspace_context is not None else None
        return organization_id, workspace_slug, actor_user_id

    def _build_preparation_reply(self, packet) -> str:
        lead = f"I prepared this for you: {packet.title}."
        lines = [lead, packet.summary]
        if packet.readiness_status == "blocked":
            lines.append("Blocked by: " + "; ".join(packet.why_blocked[:3]))
        elif packet.readiness_status == "waiting_on_input":
            lines.append("Still missing: " + "; ".join(item.label for item in packet.missing_inputs[:3]))
        else:
            lines.append("Ready because: " + "; ".join(packet.why_ready[:3]))
        evidence_items = packet.evidence_pack.items[:3]
        if evidence_items:
            rendered = []
            for item in evidence_items:
                title = str(item.get("title") or "evidence").strip()
                reason = str(item.get("reason") or "").strip()
                rendered.append(f"{title}: {reason}" if reason else title)
            lines.append("Prepared evidence: " + " | ".join(rendered))
        if packet.evidence_pack.claims:
            claim_bits = []
            for claim in packet.evidence_pack.claims[:3]:
                claim_bits.append(f"{claim.label} ({claim.status})")
            lines.append(
                f"Evidence coverage: {packet.evidence_pack.coverage_score:.2f}. Claims: " + " | ".join(claim_bits)
            )
        if packet.evidence_pack.negative_evidence:
            lines.append(
                "Missing evidence found: "
                + "; ".join(item.expected_signal for item in packet.evidence_pack.negative_evidence[:3])
            )
        if packet.missing_inputs:
            lines.append(
                "Missing inputs: "
                + "; ".join(f"{item.label}: {item.reason}" for item in packet.missing_inputs[:3])
            )
        if packet.suggested_draft is not None and packet.generation_contract.allow_draft:
            lines.append("A deterministic draft scaffold is ready if you want wording next.")
            preview_lines = [line.strip() for line in packet.suggested_draft.body.splitlines() if line.strip()][:4]
            if preview_lines:
                lines.append("Draft scaffold:\n" + "\n".join(preview_lines))
        elif packet.generation_contract.note:
            lines.append(packet.generation_contract.note)
        lines.append("This was prepared from local workflow state, memory, and ranked evidence before any language generation.")
        return "\n\n".join(line for line in lines if line)

    def _build_document_packet_reply(self, packet: DocumentAnswerPacket) -> str:
        lines = [f"I prepared a grounded document packet for you: {packet.title}.", packet.answer_intent]
        if packet.readiness_status == "blocked":
            lines.append("Blocked by: " + "; ".join(packet.why_blocked[:3]))
        elif packet.readiness_status == "waiting_on_input":
            lines.append("Still missing: " + "; ".join(item.label for item in packet.missing_inputs[:3]))
        elif packet.readiness_status == "needs_review":
            lines.append("Needs review because: " + "; ".join(packet.why_blocked[:3] or packet.why_ready[:3]))
        else:
            lines.append("Ready because: " + "; ".join(packet.why_ready[:3]))
        if packet.selected_documents:
            lines.append(
                "Target documents: "
                + " | ".join(str(item.get("title") or item.get("id") or "document") for item in packet.selected_documents[:3])
            )
        if packet.deterministic_answer:
            lines.append(packet.deterministic_answer)
        if packet.citations:
            lines.append(
                "Citations: "
                + " | ".join(
                    f"{item.title} [chunk {item.chunk_index if item.chunk_index is not None else '?'}]"
                    for item in packet.citations[:3]
                )
            )
        if packet.evidence_pack.negative_evidence:
            lines.append(
                "Missing evidence found: "
                + "; ".join(item.expected_signal for item in packet.evidence_pack.negative_evidence[:3])
            )
        if packet.generation_contract.note:
            lines.append(packet.generation_contract.note)
        lines.append("This answer was grounded by KERN before any language generation.")
        return "\n\n".join(line for line in lines if line)

    def _build_thread_packet_reply(self, packet: ThreadContextPacket) -> str:
        lines = [f"I prepared grounded thread context for you: {packet.title}.", packet.summary]
        if packet.readiness_status == "waiting_on_input":
            lines.append("Still missing: " + "; ".join(item.label for item in packet.missing_inputs[:3]))
        elif packet.readiness_status == "blocked":
            lines.append("Blocked by: " + "; ".join(packet.why_blocked[:3]))
        else:
            lines.append("Ready because: " + "; ".join(packet.why_ready[:3]))
        if packet.deterministic_answer:
            lines.append(packet.deterministic_answer)
        if packet.evidence_pack.items:
            lines.append(
                "Grounded thread evidence: "
                + " | ".join(str(item.get("title") or item.get("ref_id") or "thread") for item in packet.evidence_pack.items[:3])
            )
        if packet.generation_contract.note:
            lines.append(packet.generation_contract.note)
        return "\n\n".join(line for line in lines if line)

    def _build_person_packet_reply(self, packet: PersonContextPacket) -> str:
        lines = [f"I prepared grounded contact context for you: {packet.title}.", packet.summary]
        if packet.readiness_status == "waiting_on_input":
            lines.append("Still missing: " + "; ".join(item.label for item in packet.missing_inputs[:3]))
        elif packet.readiness_status == "blocked":
            lines.append("Blocked by: " + "; ".join(packet.why_blocked[:3]))
        else:
            lines.append("Ready because: " + "; ".join(packet.why_ready[:3]))
        if packet.deterministic_answer:
            lines.append(packet.deterministic_answer)
        if packet.evidence_pack.items:
            lines.append(
                "Grounded contact evidence: "
                + " | ".join(str(item.get("title") or item.get("ref_id") or "contact") for item in packet.evidence_pack.items[:3])
            )
        if packet.generation_contract.note:
            lines.append(packet.generation_contract.note)
        return "\n\n".join(line for line in lines if line)

    async def _render_packet_with_llm(
        self,
        packet,
        *,
        user_request: str | None = None,
    ) -> SuggestedDraftRecord | None:
        if not packet.generation_contract.allow_draft or not self.brain.llm_available:
            return None
        scaffold = packet.suggested_draft or self._reasoning_service().build_draft_from_packet(packet)
        if scaffold is None:
            return None
        supported_claims = [
            f"- {claim.label}: {claim.rationale}"
            for claim in packet.evidence_pack.claims
            if claim.status == "supported"
        ][:5]
        unresolved_claims = [
            f"- {claim.label}: {claim.rationale}"
            for claim in packet.evidence_pack.claims
            if claim.status in {"missing", "conflicted", "inferred"}
        ][:5]
        prompt_sections = [
            "Rewrite the prepared worker packet into concise, professional wording.",
            "Use only the provided scaffold and supported claims. Do not invent facts, approvals, dates, or workflow state.",
            f"Worker request: {user_request or packet.title}",
            f"Packet summary: {packet.summary}",
            "Deterministic scaffold:",
            scaffold.body,
        ]
        if supported_claims:
            prompt_sections.extend(["Supported claims:", *supported_claims])
        if unresolved_claims:
            prompt_sections.extend(
                [
                    "Unresolved claims that must not be silently resolved:",
                    *unresolved_claims,
                ]
            )
        if packet.missing_inputs:
            prompt_sections.extend(
                [
                    "Still missing according to KERN:",
                    *[f"- {item.label}: {item.reason}" for item in packet.missing_inputs[:4]],
                ]
            )
        prompt_sections.append("Return only the drafted wording.")
        rewritten = await self.brain.generate_llm_reply(
            "\n\n".join(section for section in prompt_sections if section),
            self.local_data.preferred_title(),
            conversation_history=self._build_conversation_history(),
            context_summary=self.snapshot.active_context_summary,
        )
        if not rewritten:
            return None
        return scaffold.model_copy(
            update={
                "body": rewritten.strip(),
                "mode": "llm_rewrite",
                "provenance": {
                    **scaffold.provenance,
                    "reasoning_source": "llm_generated_wording",
                    "recommendation_id": packet.recommendation_id,
                    "workflow_id": packet.workflow_id,
                },
            }
        )

    async def _render_document_packet_with_llm(
        self,
        packet: DocumentAnswerPacket,
        *,
        user_request: str | None = None,
    ) -> str | None:
        if not self.brain.llm_available:
            return None
        if not (packet.generation_contract.allow_answer or packet.generation_contract.allow_summarize or packet.generation_contract.allow_cite):
            return None
        supported_claims = [
            f"- {claim.label}: {claim.rationale}"
            for claim in packet.evidence_pack.claims
            if claim.status == "supported"
        ][:6]
        unresolved_claims = [
            f"- {claim.label}: {claim.rationale}"
            for claim in packet.evidence_pack.claims
            if claim.status in {"missing", "conflicted", "inferred"}
        ][:5]
        citation_lines = [
            f"- {item.title} [chunk {item.chunk_index if item.chunk_index is not None else '?'}]: {item.excerpt}"
            for item in packet.citations[:5]
        ]
        prompt_sections = [
            "Rewrite the grounded document packet into concise, professional wording.",
            "Use only the deterministic answer scaffold, supported claims, and attached citations. Do not invent facts, page numbers, missing sections, or document state.",
            f"User request: {user_request or packet.query_text}",
            f"Packet intent: {packet.answer_intent}",
            "Deterministic answer scaffold:",
            packet.deterministic_answer,
        ]
        if supported_claims:
            prompt_sections.extend(["Supported claims:", *supported_claims])
        if citation_lines:
            prompt_sections.extend(["Grounded citations:", *citation_lines])
        if unresolved_claims:
            prompt_sections.extend(["Unresolved claims that must stay unresolved:", *unresolved_claims])
        if packet.missing_inputs:
            prompt_sections.extend(
                ["Still missing according to KERN:", *[f"- {item.label}: {item.reason}" for item in packet.missing_inputs[:4]]]
            )
        prompt_sections.append("Return only the user-facing answer.")
        rewritten = await self.brain.generate_llm_reply(
            "\n\n".join(section for section in prompt_sections if section),
            self.local_data.preferred_title(),
            conversation_history=self._build_conversation_history(),
            context_summary=self.snapshot.active_context_summary,
        )
        return rewritten.strip() if rewritten else None

    async def _render_context_packet_with_llm(
        self,
        packet: ThreadContextPacket | PersonContextPacket,
        *,
        user_request: str | None = None,
    ) -> str | None:
        if not self.brain.llm_available:
            return None
        if not (packet.generation_contract.allow_answer or packet.generation_contract.allow_summarize):
            return None
        supported_claims = [
            f"- {claim.label}: {claim.rationale}"
            for claim in packet.evidence_pack.claims
            if claim.status == "supported"
        ][:5]
        unresolved_claims = [
            f"- {claim.label}: {claim.rationale}"
            for claim in packet.evidence_pack.claims
            if claim.status in {"missing", "conflicted", "inferred"}
        ][:4]
        prompt_sections = [
            "Rewrite the grounded local context packet into concise, professional wording.",
            "Use only the deterministic answer, supported claims, and listed blockers. Do not invent people, thread details, missing history, or workflow state.",
            f"User request: {user_request or packet.query_text}",
            f"Packet summary: {packet.summary}",
            "Deterministic answer:",
            packet.deterministic_answer,
        ]
        if supported_claims:
            prompt_sections.extend(["Supported claims:", *supported_claims])
        if unresolved_claims:
            prompt_sections.extend(["Unresolved claims:", *unresolved_claims])
        if packet.missing_inputs:
            prompt_sections.extend(["Still missing:", *[f"- {item.label}: {item.reason}" for item in packet.missing_inputs[:4]]])
        prompt_sections.append("Return only the user-facing answer.")
        rewritten = await self.brain.generate_llm_reply(
            "\n\n".join(section for section in prompt_sections if section),
            self.local_data.preferred_title(),
            conversation_history=self._build_conversation_history(),
            context_summary=self.snapshot.active_context_summary,
        )
        return rewritten.strip() if rewritten else None

    async def _try_freeform_guidance(
        self,
        transcript: str,
        *,
        trigger: str,
        workspace_context: object | None,
        allow_llm_generation: bool,
    ) -> AssistantTurn | None:
        organization_id, workspace_slug, actor_user_id = self._reasoning_context(workspace_context)
        routed = await asyncio.to_thread(
            self._reasoning_service().route_freeform_for_transcript,
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        task_intent = routed.get("task_intent")
        if not isinstance(task_intent, FreeformIntentRecord):
            return None
        packet_type = routed.get("packet_type")
        packet = routed.get("packet")
        if task_intent.task_family == "general_chat_fallback":
            return None
        clarification_source = (
            self._reasoning_service().freeform_intelligence._clarification_source_family(transcript, task_intent)
            if task_intent.task_family == "clarification_needed"
            else ""
        )
        if task_intent.task_family == "clarification_needed" and packet is not None and getattr(packet, "readiness_status", None) != "ready_now":
            reply = task_intent.clarification_prompt or task_intent.clarification_reason or "I need one more detail before I can ground that locally."
            self._pending_interaction = PendingInteraction(
                kind="clarification",
                prompt=reply,
                original_utterance=transcript,
                trigger_source=trigger,
                missing_slots=["local_target"],
            )
            await self._store_response_text(reply)
            await asyncio.to_thread(self.memory.append_conversation_entry, f"User: {transcript}\nKern: {reply}")
            await asyncio.to_thread(self.dialogue_state.set_last_response, reply)
            self.snapshot.active_plan = None
            self.snapshot.response_text = reply
            self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
            self.snapshot.last_action = "Asked for clarification before grounding a freeform request."
            self.append_turn("assistant", reply, meta={"reasoning_source": "system_decision", "task_family": task_intent.task_family, "packet_type": packet_type, "packet_id": getattr(packet, "id", None)})
            self.add_history("route", f"freeform:clarification:{task_intent.confidence:.2f}")
            self.mark_dirty("runtime")
            await self.broadcast(reason="freeform_clarification")
            return AssistantTurn(trigger=trigger, transcript=transcript, intent_type="query", response_text=reply, reasoning_source="system_decision")
        if packet is None and task_intent.task_family == "clarification_needed":
            if clarification_source == "generic":
                return None
            reply = task_intent.clarification_prompt or task_intent.clarification_reason or "I need one more detail before I can ground that locally."
            self._pending_interaction = PendingInteraction(
                kind="clarification",
                prompt=reply,
                original_utterance=transcript,
                trigger_source=trigger,
                missing_slots=["local_target"],
            )
            await self._store_response_text(reply)
            await asyncio.to_thread(self.memory.append_conversation_entry, f"User: {transcript}\nKern: {reply}")
            await asyncio.to_thread(self.dialogue_state.set_last_response, reply)
            self.snapshot.active_plan = None
            self.snapshot.response_text = reply
            self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
            self.snapshot.last_action = "Asked for clarification before grounding a freeform request."
            self.append_turn("assistant", reply, meta={"reasoning_source": "system_decision", "task_family": task_intent.task_family})
            self.add_history("route", f"freeform:clarification:{task_intent.confidence:.2f}")
            self.mark_dirty("runtime")
            await self.broadcast(reason="freeform_clarification")
            return AssistantTurn(trigger=trigger, transcript=transcript, intent_type="query", response_text=reply, reasoning_source="system_decision")
        if packet_type == "document_answer_packet" and isinstance(packet, DocumentAnswerPacket):
            rendered = None
            if allow_llm_generation and (packet.generation_contract.allow_answer or packet.generation_contract.allow_summarize or packet.generation_contract.allow_cite):
                rendered = await self._render_document_packet_with_llm(packet, user_request=transcript)
            if rendered:
                reply = "\n\n".join(
                    filter(
                        None,
                        [
                            f"I prepared this from KERN's grounded document packet: {packet.title}.",
                            f"Ready because: {'; '.join(packet.why_ready[:3])}" if packet.why_ready else packet.answer_intent,
                            rendered,
                            (
                                "Grounded citations: "
                                + " | ".join(f"{item.title} [chunk {item.chunk_index if item.chunk_index is not None else '?'}]" for item in packet.citations[:3])
                            ) if packet.citations else "",
                        ],
                    )
                )
                reasoning_source = "llm_generated_wording"
                outcome_type = "llm_rewrite_used"
            else:
                reply = self._build_document_packet_reply(packet)
                reasoning_source = "system_decision"
                outcome_type = "packet_used"
            await asyncio.to_thread(
                self._reasoning_service().record_interaction_outcome,
                packet_type="document_answer",
                packet_id=packet.id,
                outcome_type=outcome_type,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                metadata={
                    "task_family": packet.task_intent.task_family,
                    "linked_entity_refs": packet.linked_entity_refs,
                    "selected_document_ids": packet.selected_document_ids,
                },
            )
            recommendation_id = None
            workflow_type = None
            last_action = f"Prepared {packet.task_intent.task_family.replace('_', ' ')} from freeform document grounding."
        elif packet_type == "preparation_packet":
            rendered_draft = None
            if allow_llm_generation and self._transcript_requests_wording(transcript):
                rendered_draft = await self._render_packet_with_llm(packet, user_request=transcript)
            if rendered_draft is not None:
                reply = "\n\n".join(
                    [
                        f"I prepared this for you and rewrote the wording from KERN's evidence-backed packet: {packet.title}.",
                        f"Ready because: {'; '.join(packet.why_ready[:3])}" if packet.why_ready else packet.summary,
                        rendered_draft.body,
                    ]
                )
                reasoning_source = "llm_generated_wording"
                outcome_type = "llm_rewrite_used"
            else:
                reply = self._build_preparation_reply(packet)
                reasoning_source = "system_decision"
                outcome_type = "packet_used"
            await asyncio.to_thread(
                self._reasoning_service().record_interaction_outcome,
                packet_type="preparation",
                packet_id=packet.id,
                outcome_type=outcome_type,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                metadata={
                    "preparation_type": packet.preparation_type,
                    "linked_entity_refs": packet.linked_entity_refs,
                    "workflow_id": packet.workflow_id,
                },
            )
            recommendation_id = packet.recommendation_id
            workflow_type = packet.workflow_type
            last_action = f"Prepared {packet.preparation_type.replace('_', ' ')} from freeform local reasoning."
        elif packet_type == "thread_context_packet" and isinstance(packet, ThreadContextPacket):
            rendered = await self._render_context_packet_with_llm(packet, user_request=transcript) if allow_llm_generation else None
            reply = rendered or self._build_thread_packet_reply(packet)
            reasoning_source = "llm_generated_wording" if rendered else "system_decision"
            outcome_type = "same_thread_packet_accepted" if packet.readiness_status == "ready_now" else "packet_used"
            await asyncio.to_thread(
                self._reasoning_service().record_interaction_outcome,
                packet_type="thread_context",
                packet_id=packet.id,
                outcome_type=outcome_type,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                metadata={
                    "task_family": packet.task_intent.task_family,
                    "linked_entity_refs": packet.linked_entity_refs,
                    "thread_refs": packet.thread_refs,
                },
            )
            recommendation_id = None
            workflow_type = None
            last_action = "Prepared grounded thread context from freeform routing."
        elif packet_type == "person_context_packet" and isinstance(packet, PersonContextPacket):
            rendered = await self._render_context_packet_with_llm(packet, user_request=transcript) if allow_llm_generation else None
            reply = rendered or self._build_person_packet_reply(packet)
            reasoning_source = "llm_generated_wording" if rendered else "system_decision"
            outcome_type = "same_contact_packet_accepted" if packet.readiness_status == "ready_now" else "packet_used"
            await asyncio.to_thread(
                self._reasoning_service().record_interaction_outcome,
                packet_type="person_context",
                packet_id=packet.id,
                outcome_type=outcome_type,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                metadata={
                    "task_family": packet.task_intent.task_family,
                    "linked_entity_refs": packet.linked_entity_refs,
                    "person_ref": packet.person_ref,
                },
            )
            recommendation_id = None
            workflow_type = None
            last_action = "Prepared grounded person context from freeform routing."
        else:
            return None
        await self._store_response_text(reply)
        await asyncio.to_thread(self.memory.append_conversation_entry, f"User: {transcript}\nKern: {reply}")
        await asyncio.to_thread(self.dialogue_state.set_last_response, reply)
        self.snapshot.active_plan = None
        self.snapshot.response_text = reply
        self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
        self.snapshot.last_action = last_action
        self.append_turn("assistant", reply, meta={"reasoning_source": reasoning_source, "task_family": task_intent.task_family, "packet_type": packet_type, "packet_id": getattr(packet, "id", None)})
        self.add_history("route", f"freeform:{task_intent.task_family}:{task_intent.confidence:.2f}")
        self.mark_dirty("runtime")
        await self.broadcast(reason="deterministic_freeform")
        return AssistantTurn(
            trigger=trigger,
            transcript=transcript,
            intent_type="query",
            response_text=reply,
            reasoning_source=reasoning_source,
            recommendation_id=recommendation_id,
            workflow_type=workflow_type,
        )

    def _transcript_requests_wording(self, transcript: str) -> bool:
        lowered = (transcript or "").lower()
        markers = (
            "write it",
            "draft it",
            "compose",
            "rewrite",
            "word it",
            "prepare the email",
            "write the email",
            "draft the email",
            "turn this into",
            "formuliere",
            "schreibe",
            "verfasse",
            "entwirf",
            "angebotsabsatz",
            "antwort",
            "nachricht",
        )
        if any(marker in lowered for marker in markers):
            return True
        return ("draft" in lowered or "write" in lowered or "rewrite" in lowered or "compose" in lowered) and (
            "email" in lowered or "reply" in lowered or "follow-up" in lowered or "follow up" in lowered or "message" in lowered
        )

    def _is_plain_chat_request(self, transcript: str) -> bool:
        lowered = " ".join((transcript or "").strip().lower().split())
        if not lowered or len(lowered) > 180:
            return False
        local_workflow_markers = (
            "document",
            "file",
            "upload",
            "evidence",
            "citation",
            "source",
            "compliance",
            "legal",
            "retention",
            "erasure",
            "export",
            "audit",
            "policy",
            "workspace",
            "customer",
            "client",
            "supplier",
            "email",
            "draft",
            "review",
            "finalize",
            "schedule",
            "reminder",
            "backup",
            "summarize",
            "compare",
        )
        if any(marker in lowered for marker in local_workflow_markers):
            return False
        if lowered in {"hi", "hello", "hey"}:
            return True
        prefix_markers = (
            "what can you do",
            "who are you",
            "answer this",
            "reply with",
            "respond with",
            "say ",
        )
        if any(lowered.startswith(marker) for marker in prefix_markers):
            return True
        return "smoke test" in lowered or "one short sentence" in lowered

    def _is_low_signal_text(self, text: str) -> bool:
        value = " ".join((text or "").strip().lower().split())
        if not value or len(value) <= 2:
            return True
        if len(value.split()) == 1 and len(value) <= 10:
            if not re.search(r"[aeiouÃƒÂ¤ÃƒÂ¶ÃƒÂ¼]", value):
                return True
            known_short_words = {
                "hi",
                "hey",
                "hello",
                "help",
                "hallo",
                "danke",
                "yes",
                "no",
                "ok",
                "okay",
                "policy",
                "risk",
                "draft",
                "write",
                "summarize",
                "explain",
            }
            if re.fullmatch(r"[a-zÃƒÂ¤ÃƒÂ¶ÃƒÂ¼ÃƒÅ¸]{3,10}", value) and value not in known_short_words:
                return True
        return False

    def _local_generation_unavailable_reply(self, transcript: str) -> str:
        if self._is_low_signal_text(transcript):
            return "I could not parse that as a request. Send a complete question or instruction."
        return "The local model is not available, so I cannot generate a free-form answer right now."

    async def _reply_plain_chat(
        self,
        transcript: str,
        *,
        trigger: str,
        context_summary=None,
        allow_llm_fallback: bool,
    ) -> AssistantTurn:
        title = self.local_data.preferred_title()
        llm_reply_text = None
        if allow_llm_fallback:
            try:
                llm_reply_text = await asyncio.wait_for(
                    self._try_llm_chat(transcript, title, context_summary=context_summary),
                    timeout=max(1.0, min(float(settings.llama_server_timeout), self.PLAIN_CHAT_LLM_TIMEOUT_SECONDS)),
                )
            except asyncio.TimeoutError:
                self.add_history("system", "plain_chat_llm_timeout")
            except Exception as exc:
                logger.debug("Plain chat LLM reply failed: %s", exc, exc_info=True)
        if llm_reply_text:
            reply_display = llm_reply_text
            reply_spoken = llm_reply_text
            reasoning_source = "llm_generated_wording"
        elif allow_llm_fallback:
            if not self.brain.llm_available:
                reply_display = self._local_generation_unavailable_reply(transcript)
                reply_spoken = reply_display
            else:
                try:
                    persona_reply = self.brain.generate_chat_persona_reply(transcript, title)
                    reply_display = persona_reply.display_text
                    reply_spoken = persona_reply.display_text
                except Exception as exc:
                    logger.warning("Persona reply generation failed: %s", exc)
                    reply_display = "The local model did not return a usable answer. Please try again."
                    reply_spoken = reply_display
            reasoning_source = "system_decision"
        else:
            reply_display = "I could not resolve that with deterministic local reasoning alone."
            reply_spoken = reply_display
            reasoning_source = "system_decision"
        await self._store_response_text(reply_spoken)
        await asyncio.to_thread(self.memory.append_conversation_entry, f"User: {transcript}\nKern: {reply_display}")
        await asyncio.to_thread(self.dialogue_state.set_last_response, reply_display)
        self.snapshot.active_plan = None
        self.snapshot.response_text = reply_display
        self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
        self.snapshot.last_action = "Replied conversationally."
        self.append_turn("assistant", reply_display, meta={"reasoning_source": reasoning_source, "route": "plain_chat"})
        self.add_history("system", "plain_chat")
        self.mark_dirty("runtime")
        await self.broadcast(reason="plain_chat_reply")
        return AssistantTurn(
            trigger=trigger,
            transcript=transcript,
            intent_type="chat",
            response_text=reply_display,
            reasoning_source=reasoning_source,
        )

    async def _try_document_guidance(
        self,
        transcript: str,
        *,
        trigger: str,
        workspace_context: object | None,
        allow_llm_generation: bool,
    ) -> AssistantTurn | None:
        organization_id, workspace_slug, actor_user_id = self._reasoning_context(workspace_context)
        task_intent = await asyncio.to_thread(
            self._reasoning_service().classify_task_intent_for_transcript,
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        if task_intent.task_family not in {
            "document_qa",
            "document_citation",
            "document_summary",
            "document_key_sections",
            "document_compare",
            "clarification_needed",
        }:
            return None
        packet = await asyncio.to_thread(
            self._reasoning_service().get_document_answer_packet_for_transcript,
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        if packet is None:
            return None
        rendered_answer = None
        if allow_llm_generation and (
            packet.generation_contract.allow_answer
            or packet.generation_contract.allow_summarize
            or packet.generation_contract.allow_cite
        ):
            rendered_answer = await self._render_document_packet_with_llm(packet, user_request=transcript)
        if rendered_answer:
            lines = [
                f"I prepared this from KERN's grounded document packet: {packet.title}.",
                f"Ready because: {'; '.join(packet.why_ready[:3])}" if packet.why_ready else packet.answer_intent,
                rendered_answer,
            ]
            if packet.citations:
                lines.append(
                    "Grounded citations: "
                    + " | ".join(
                        f"{item.title} [chunk {item.chunk_index if item.chunk_index is not None else '?'}]"
                        for item in packet.citations[:3]
                    )
                )
            if packet.evidence_pack.negative_evidence:
                lines.append(
                    "Still missing: "
                    + "; ".join(item.expected_signal for item in packet.evidence_pack.negative_evidence[:3])
                )
            reply = "\n\n".join(lines)
            reasoning_source = "llm_generated_wording"
        else:
            reply = self._build_document_packet_reply(packet)
            reasoning_source = "system_decision"
        await self._store_response_text(reply)
        await asyncio.to_thread(self.memory.append_conversation_entry, f"User: {transcript}\nKern: {reply}")
        await asyncio.to_thread(self.dialogue_state.set_last_response, reply)
        self.snapshot.active_plan = None
        self.snapshot.response_text = reply
        self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
        self.snapshot.last_action = f"Prepared {packet.task_intent.task_family.replace('_', ' ')} from deterministic document reasoning."
        self.append_turn(
            "assistant",
            reply,
            meta={
                "reasoning_source": reasoning_source,
                "task_family": packet.task_intent.task_family,
                "document_packet_id": packet.id,
                "readiness_status": packet.readiness_status,
            },
        )
        self.add_history("route", f"document:{packet.task_intent.task_family}:{packet.evidence_pack.coverage_score:.2f}")
        self.add_history("plan", f"Grounded document packet {packet.task_intent.task_family}.")
        self.mark_dirty("runtime")
        await self.broadcast(reason="deterministic_document_packet")
        return AssistantTurn(
            trigger=trigger,
            transcript=transcript,
            intent_type="query",
            response_text=reply,
            reasoning_source=reasoning_source,
        )

    async def _try_reasoning_guidance(
        self,
        transcript: str,
        *,
        trigger: str,
        workspace_context: object | None,
        allow_llm_generation: bool,
    ) -> AssistantTurn | None:
        organization_id, workspace_slug, actor_user_id = self._reasoning_context(workspace_context)
        packet = await asyncio.to_thread(
            self._reasoning_service().get_preparation_packet_for_transcript,
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        if packet is None:
            return None
        rendered_draft = None
        if allow_llm_generation and self._transcript_requests_wording(transcript):
            rendered_draft = await self._render_packet_with_llm(packet, user_request=transcript)
        if rendered_draft is not None:
            reply = "\n\n".join(
                [
                    f"I prepared this for you and rewrote the wording from KERN's evidence-backed packet: {packet.title}.",
                    f"Ready because: {'; '.join(packet.why_ready[:3])}" if packet.why_ready else packet.summary,
                    rendered_draft.body,
                ]
            )
            reasoning_source = "llm_generated_wording"
        else:
            reply = self._build_preparation_reply(packet)
            reasoning_source = "system_decision"
        await self._store_response_text(reply)
        await asyncio.to_thread(self.memory.append_conversation_entry, f"User: {transcript}\nKern: {reply}")
        await asyncio.to_thread(self.dialogue_state.set_last_response, reply)
        self.snapshot.active_plan = None
        self.snapshot.response_text = reply
        self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
        self.snapshot.last_action = f"Prepared {packet.preparation_type.replace('_', ' ')} from local workflow reasoning."
        self.append_turn(
            "assistant",
            reply,
            meta={
                "reasoning_source": reasoning_source,
                "workflow_type": packet.workflow_type,
                "recommendation_type": packet.preparation_type,
                "recommendation_id": packet.recommendation_id,
                "evidence_bundle_id": packet.evidence_pack.id,
                "readiness_status": packet.readiness_status,
            },
        )
        self.add_history(
            "route",
            f"deterministic:{packet.preparation_type}:{packet.focus_hint.score if packet.focus_hint else 0:.2f}",
        )
        self.add_history(
            "plan",
            f"Local preparation packet {packet.preparation_type} via {packet.workflow_type}.",
        )
        self.mark_dirty("runtime")
        await self.broadcast(reason="deterministic_preparation")
        return AssistantTurn(
            trigger=trigger,
            transcript=transcript,
            intent_type="query",
            response_text=reply,
            reasoning_source=reasoning_source,
            recommendation_id=packet.recommendation_id,
            workflow_type=packet.workflow_type,
        )

    async def process_transcript(
        self,
        transcript: str,
        trigger: str = "manual_ui",
        *,
        workspace_context: object | None = None,
        allow_llm_fallback: bool = True,
    ) -> AssistantTurn:
        if self.snapshot.action_in_progress:
            reply = "One moment. I am finishing the current action."
            self.snapshot.response_text = reply
            self.snapshot.last_action = "Ignored overlapping command."
            self.append_turn("system", reply, kind="tool_status", status="pending")
            self.mark_dirty("runtime")
            await self.broadcast(reason="overlap_blocked")
            return AssistantTurn(trigger=trigger, transcript=transcript, intent_type="chat", response_text=reply)
        pending = self._pending_interaction
        effective_transcript = transcript
        if pending and pending.kind == "clarification":
            effective_transcript = f"{pending.original_utterance.rstrip()} {transcript.strip()}".strip()
            self._pending_interaction = None

        self.snapshot.transcript = effective_transcript
        self.snapshot.assistant_state = "processing"
        self.append_turn("user", transcript)
        self.mark_dirty("context", "runtime")
        self.add_history("speech", f"Heard: {transcript}")
        self.refresh_context_snapshot(force=True)
        await self.broadcast(reason="transcript_received")

        if pending is None:
            if self._is_plain_chat_request(effective_transcript):
                context_summary = self.snapshot.active_context_summary or self.context_assembler.build()
                self.snapshot.active_context_summary = context_summary
                return await self._reply_plain_chat(
                    effective_transcript,
                    trigger=trigger,
                    context_summary=context_summary,
                    allow_llm_fallback=allow_llm_fallback,
                )
            freeform_turn = await self._try_freeform_guidance(
                effective_transcript,
                trigger=trigger,
                workspace_context=workspace_context,
                allow_llm_generation=allow_llm_fallback,
            )
            if freeform_turn is not None:
                return freeform_turn
            document_turn = await self._try_document_guidance(
                effective_transcript,
                trigger=trigger,
                workspace_context=workspace_context,
                allow_llm_generation=allow_llm_fallback,
            )
            if document_turn is not None:
                return document_turn
            reasoning_turn = await self._try_reasoning_guidance(
                effective_transcript,
                trigger=trigger,
                workspace_context=workspace_context,
                allow_llm_generation=allow_llm_fallback,
            )
            if reasoning_turn is not None:
                return reasoning_turn
            if not allow_llm_fallback:
                reply = "I could not resolve that with deterministic local reasoning alone."
                await self._store_response_text(reply)
                await asyncio.to_thread(self.dialogue_state.set_last_response, reply)
                self.snapshot.active_plan = None
                self.snapshot.response_text = reply
                self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
                self.snapshot.last_action = "No deterministic workflow route was available."
                self.append_turn(
                    "assistant",
                    reply,
                    meta={"reasoning_source": "system_decision", "deterministic_route": "none"},
                )
                self.add_history("route", "deterministic:none")
                self.mark_dirty("runtime")
                await self.broadcast(reason="deterministic_unresolved")
                return AssistantTurn(
                    trigger=trigger,
                    transcript=effective_transcript,
                    intent_type="query",
                    response_text=reply,
                    reasoning_source="system_decision",
                )

        context_summary = self.snapshot.active_context_summary or self.context_assembler.build()
        available_caps = [
            descriptor.name
            for descriptor in (self.snapshot.capability_status or self.capabilities.available_descriptors())
            if descriptor.available
        ]
        if self.brain.llm_available:
            analysis = await self.brain._cognition.analyze_async(
                effective_transcript,
                dialogue_context=self._dialogue_context(),
                context_summary=context_summary,
                available_capabilities=available_caps,
            )
        else:
            analysis = self.brain.analyze_intent(
                effective_transcript,
                dialogue_context=self._dialogue_context(),
                context_summary=context_summary,
                available_capabilities=available_caps,
            )
        parsed = analysis.parsed_intent
        plan = analysis.execution_plan
        self.snapshot.active_plan = plan
        self.snapshot.active_context_summary = context_summary
        top_candidates = ", ".join(
            f"{candidate.source}:{candidate.tool_name or candidate.name}:{candidate.confidence:.2f}"
            for candidate in sorted(analysis.candidates, key=lambda item: item.confidence, reverse=True)[:3]
        )
        self.add_history(
            "route",
            f"Selected {plan.source}:{parsed.tool_request.tool_name if parsed.tool_request else parsed.intent_name}:{plan.confidence:.2f}"
            + (f" from [{top_candidates}]" if top_candidates else ""),
        )
        self.add_history(
            "plan",
            f"Intent {parsed.intent_name} via {plan.source} with {len(plan.steps)} step(s).",
        )
        if parsed.missing_slots:
            reply = parsed.response_hint if parsed.response_hint else f"I need {', '.join(parsed.missing_slots)} before I can do that."
            self._pending_interaction = PendingInteraction(
                kind="clarification",
                prompt=reply,
                original_utterance=effective_transcript,
                trigger_source=trigger,
                missing_slots=list(parsed.missing_slots),
                plan=plan,
            )
            await self._store_response_text(reply)
            self.snapshot.response_text = reply
            self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
            self.snapshot.last_action = "Asked for clarification."
            self.append_turn("assistant", reply)
            self.add_history("system", self.snapshot.last_action)
            self.mark_dirty("runtime")
            await self.broadcast(reason="clarification")
            return AssistantTurn(
                trigger=trigger,
                transcript=effective_transcript,
                intent_type=parsed.intent_type,
                response_text=reply,
                plan=plan,
                candidates=analysis.candidates,
            )

        if not plan.steps:
            if self.brain.looks_operational_request(effective_transcript):
                reply = "I couldn't map that operational request to a supported capability yet. Try rephrasing it more explicitly."
                await self._store_response_text(reply)
                turn = AssistantTurn(
                    trigger=trigger,
                    transcript=effective_transcript,
                    intent_type=parsed.intent_type,
                    response_text=reply,
                    plan=plan,
                    candidates=analysis.candidates,
                )
                self.snapshot.response_text = reply
                self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
                self.snapshot.last_action = "Operational request could not be mapped."
                await asyncio.to_thread(self.dialogue_state.set_last_response, reply)
                self.append_turn("assistant", reply, kind="tool_status", status="failed")
                self.add_history("system", "operational_unmapped")
                self.add_history("route", "clarification_needed: operational_unmapped")
                self.mark_dirty("runtime")
                await self.broadcast(reason="operational_unmapped")
                return turn
            title = self.local_data.preferred_title()
            if not allow_llm_fallback:
                reply_display = "I could not resolve that with deterministic local reasoning alone."
                reply_spoken = reply_display
                llm_reply_text = None
            else:
                llm_reply_text = await self._try_llm_chat(transcript, title, context_summary=context_summary)
            if llm_reply_text:
                reply_display = llm_reply_text
                reply_spoken = llm_reply_text
            elif not allow_llm_fallback:
                reply_display = "I could not resolve that with deterministic local reasoning alone."
                reply_spoken = reply_display
            elif not self.brain.llm_available:
                reply_display = self._local_generation_unavailable_reply(transcript)
                reply_spoken = reply_display
            else:
                try:
                    persona_reply = self.brain.generate_chat_persona_reply(transcript, title)
                    reply_display = persona_reply.display_text
                    reply_spoken = persona_reply.display_text
                except Exception as exc:
                    logger.warning("Persona reply generation failed: %s", exc)
                    reply_display = "The local model did not return a usable answer. Please try again."
                    reply_spoken = reply_display
            await self._store_response_text(reply_spoken)
            await asyncio.to_thread(self.memory.append_conversation_entry, f"User: {transcript}\nKern: {reply_display}")
            turn = AssistantTurn(
                trigger=trigger,
                transcript=effective_transcript,
                intent_type=parsed.intent_type,
                response_text=reply_display,
                plan=plan,
                candidates=analysis.candidates,
                reasoning_source="llm_generated_wording" if llm_reply_text else None,
            )
            self.snapshot.response_text = reply_display
            self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
            self.snapshot.last_action = "Replied conversationally."
            await asyncio.to_thread(self.dialogue_state.set_last_response, reply_display)
            self.append_turn("assistant", reply_display)
            self.add_history("system", "chat_fallback")
            self.mark_dirty("runtime")
            await self.broadcast(reason="chat_reply")
            return turn

        decision = self.policy.decide_plan(plan, self.capabilities)
        await self.event_hub.publish({"type": "policy", "payload": decision.model_dump(mode="json")})
        self.add_history("plan", f"{plan.summary or 'plan'}: {decision.verdict}")
        if decision.verdict == "deny":
            reply = f"I cannot do that. {decision.message}"
            await self._store_response_text(reply)
            self.snapshot.response_text = reply
            self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
            self.snapshot.last_action = "Denied by policy."
            await asyncio.to_thread(self.dialogue_state.set_last_response, reply)
            self.append_turn("assistant", reply, kind="tool_status", status="failed")
            self.mark_dirty("runtime")
            await self.broadcast(reason="policy_deny")
            return AssistantTurn(
                trigger=trigger,
                transcript=effective_transcript,
                intent_type=parsed.intent_type,
                response_text=reply,
                tool_calls=[self.capabilities.build_request(step, effective_transcript, trigger) for step in plan.steps],
                plan=plan,
                candidates=analysis.candidates,
            )
        if decision.verdict == "confirm":
            step = plan.steps[decision.step_index or 0]
            prompt = f"{decision.message} Approve plan: {plan.summary or step.title or step.capability_name}?"
            self.set_pending_confirmation(
                plan,
                prompt=prompt,
                original_utterance=effective_transcript,
                trigger_source=trigger,
            )
            self.snapshot.pending_confirmation = PendingConfirmation(step=step, prompt=prompt)
            self.snapshot.response_text = prompt
            self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
            self.snapshot.last_action = "Waiting for confirmation."
            await self._store_response_text(prompt)
            await asyncio.to_thread(self.dialogue_state.set_last_response, prompt)
            self.append_turn("system", prompt, kind="confirmation", status="pending")
            self.mark_dirty("runtime")
            await self.broadcast(reason="policy_confirm")
            return AssistantTurn(
                trigger=trigger,
                transcript=effective_transcript,
                intent_type=parsed.intent_type,
                response_text=prompt,
                tool_calls=[self.capabilities.build_request(step, effective_transcript, trigger) for step in plan.steps],
                plan=plan,
                candidates=analysis.candidates,
            )

        receipts, reply = await self._execute_plan(plan, effective_transcript, trigger)
        self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
        self.mark_dirty("runtime")
        await self.broadcast(reason="plan_executed")
        return AssistantTurn(
            trigger=trigger,
            transcript=effective_transcript,
            intent_type=parsed.intent_type,
            response_text=reply,
            tool_calls=[self.capabilities.build_request(step, effective_transcript, trigger) for step in plan.steps],
            plan=plan,
            candidates=analysis.candidates,
            receipts=receipts,
        )

    async def confirm_pending(self, approved: bool) -> None:
        if self.snapshot.action_in_progress:
            return
        pending = self.snapshot.pending_confirmation
        interaction = self._pending_interaction
        plan = self._pending_plan
        self.snapshot.pending_confirmation = None
        self.clear_pending_confirmation()
        if not pending or plan is None:
            return
        if not approved:
            text = "I will not proceed."
            await self._store_response_text(text)
            self.snapshot.response_text = text
            self.snapshot.last_action = "Action cancelled."
            self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
            await asyncio.to_thread(self.dialogue_state.set_last_response, text)
            self.append_turn("system", text, kind="confirmation")
            self.add_history("system", text)
            self.mark_dirty("runtime")
            await self.broadcast(reason="confirmation_cancelled")
            return
        original_utterance = interaction.original_utterance if interaction else pending.prompt
        trigger_source = interaction.trigger_source if interaction else "manual_ui"
        await self._execute_plan(plan, original_utterance, trigger_source)
        self.snapshot.assistant_state = "muted" if self.local_data.muted() else "idle"
        self.mark_dirty("runtime")
        await self.broadcast(reason="confirmation_approved")

    async def maybe_emit_proactive_prompt(self) -> None:
        self.refresh_context_snapshot(force=True)
        context = self.snapshot.active_context_summary or self.context_assembler.build()
        prompt = self.attention_manager.next_prompt(context)
        if prompt is None or prompt.message == self._last_proactive_message:
            return
        self._last_proactive_message = prompt.message
        self.snapshot.proactive_prompt = prompt
        self.snapshot.response_text = prompt.message
        self.snapshot.last_action = prompt.reason
        self.snapshot.assistant_mode = "proactive"
        self.append_turn("system", prompt.message, kind="proactive")
        self.add_history("system", f"Proactive: {prompt.reason}")
        self.mark_dirty("context", "runtime")
        await self.broadcast(reason="proactive_prompt")

    async def _execute_plan(self, plan: ExecutionPlan, transcript: str, trigger: str = "manual_ui") -> tuple[list[ExecutionReceipt], str]:
        self.snapshot.action_in_progress = True
        self.snapshot.assistant_state = "responding"
        self.snapshot.active_plan = plan
        self.mark_dirty("runtime")
        receipts: list[ExecutionReceipt] = []
        last_display = ""
        try:
            for step in plan.steps:
                self.add_history("plan", f"Executing {step.capability_name}.")
                request = self.capabilities.build_request(step, transcript, trigger)
                capability = self.capabilities.get(step.capability_name) if self.capabilities.get_descriptor(step.capability_name) else None
                if capability is None:
                    result = ToolResult(
                        status="failed",
                        display_text=f"Unknown capability: {step.capability_name}.",
                        suggested_follow_up="Use a registered capability name or fix planner output.",
                    )
                    receipt = self.verifier.verify(request, result)
                    await asyncio.to_thread(self.memory.append_execution_receipt, receipt)
                    receipts.append(receipt)
                    self.snapshot.last_receipts = ([receipt] + self.snapshot.last_receipts)[: self.MAX_RECEIPT_CACHE]
                    self._cached_receipts = self.snapshot.last_receipts
                    self._receipt_cache_ready = True
                    self.snapshot.verification_state = f"{receipt.capability_name}: {receipt.status}"
                    self.snapshot.response_text = result.display_text
                    self.snapshot.last_action = result.display_text
                    self.mark_dirty("receipts", "runtime")
                    self.add_history("system", f"Failed {request.tool_name}: {result.display_text}")
                    last_display = result.display_text
                    break
                available, availability_note = self.capabilities.is_available(step.capability_name)
                if not available:
                    message = availability_note or f"{step.capability_name} is unavailable right now."
                    result = ToolResult(
                        status="failed",
                        display_text=message,
                        evidence=[availability_note] if availability_note else [],
                        suggested_follow_up=availability_note,
                    )
                    receipt = self.verifier.verify(request, result)
                    await asyncio.to_thread(self.memory.append_execution_receipt, receipt)
                    self.capabilities.update_last_status(step.capability_name, receipt.status)
                    receipts.append(receipt)
                    self.snapshot.last_receipts = ([receipt] + self.snapshot.last_receipts)[: self.MAX_RECEIPT_CACHE]
                    self._cached_receipts = self.snapshot.last_receipts
                    self._receipt_cache_ready = True
                    self.snapshot.verification_state = f"{receipt.capability_name}: {receipt.status}"
                    self.snapshot.response_text = result.display_text
                    self.snapshot.last_action = result.display_text
                    self.mark_dirty("receipts", "capabilities", "runtime")
                    self.add_history("system", f"Failed {request.tool_name}: {result.display_text}")
                    last_display = result.display_text
                    break
                preamble = self._tool_preamble(request)
                if preamble:
                    await self._store_response_text(preamble, wait=True)
                validation_error = capability.tool.validate_arguments(request.arguments)
                if validation_error:
                    result = ToolResult(
                        status="failed",
                        display_text=validation_error,
                        suggested_follow_up="Review the required arguments and retry the action.",
                    )
                    receipt = self.verifier.verify(request, result)
                    await asyncio.to_thread(self.memory.append_execution_receipt, receipt)
                    self.capabilities.update_last_status(step.capability_name, receipt.status)
                    receipts.append(receipt)
                    self.snapshot.last_receipts = ([receipt] + self.snapshot.last_receipts)[: self.MAX_RECEIPT_CACHE]
                    self._cached_receipts = self.snapshot.last_receipts
                    self._receipt_cache_ready = True
                    self.snapshot.verification_state = f"{receipt.capability_name}: {receipt.status}"
                    self.snapshot.response_text = result.display_text
                    self.snapshot.last_action = result.display_text
                    self.mark_dirty("receipts", "capabilities", "runtime")
                    self.add_history("system", f"Failed {request.tool_name}: {result.display_text}")
                    last_display = result.display_text
                    break
                timeout_seconds = capability.tool.timeout_seconds() or self._tool_timeout_seconds
                try:
                    result = await asyncio.wait_for(capability.tool.run(request), timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    result = ToolResult(
                        status="failed",
                        display_text=f"{request.tool_name} timed out.",
                        suggested_follow_up="Try the request again or reduce the scope of the action.",
                        data={"timeout_seconds": timeout_seconds},
                    )
                except Exception as exc:
                    logger.warning("Tool %s failed: %s", request.tool_name, exc, exc_info=True)
                    result = ToolResult(
                        status="failed",
                        display_text=f"{request.tool_name} failed.",
                        data={},
                    )
                receipt = self.verifier.verify(request, result)
                await asyncio.to_thread(self.memory.append_execution_receipt, receipt)
                self.capabilities.update_last_status(step.capability_name, receipt.status)
                receipts.append(receipt)
                self.snapshot.last_receipts = ([receipt] + self.snapshot.last_receipts)[: self.MAX_RECEIPT_CACHE]
                self._cached_receipts = self.snapshot.last_receipts
                self._receipt_cache_ready = True
                self.snapshot.verification_state = f"{receipt.capability_name}: {receipt.status}"
                self._apply_tool_result(result)
                self.snapshot.response_text = result.display_text
                self.snapshot.last_action = result.display_text
                await asyncio.to_thread(self.dialogue_state.set_last_response, result.display_text)
                self.mark_dirty("receipts", "capabilities", "runtime")
                last_display = result.display_text
                if receipt.status == "failed":
                    self.add_history("system", f"Failed {request.tool_name}: {result.display_text}")
                    break
                self.add_history("tool", f"{receipt.status.capitalize()} {request.tool_name}: {result.display_text}")
                await self.event_hub.publish(
                    {
                        "type": "tool_result",
                        "payload": {
                            "result": result.model_dump(mode="json"),
                            "receipt": receipt.model_dump(mode="json"),
                        },
                    }
                )
            if last_display and plan.source == "planner" and self.brain.llm_available and receipts:
                llm_summary = await self._llm_summarize_results(transcript, receipts)
                if llm_summary:
                    last_display = llm_summary
            if last_display:
                final_status = "failed" if any(receipt.status == "failed" for receipt in receipts) else "complete"
                self.append_turn(
                    "assistant",
                    last_display,
                    kind="tool_status",
                    status=final_status,
                    meta={"receipt_count": len(receipts)},
                )
            return receipts, last_display or "Plan complete."
        finally:
            self.snapshot.action_in_progress = False
            self.mark_dirty("runtime")

    def _apply_tool_result(self, result: ToolResult) -> None:
        data = result.data
        if data.get("morning_brief"):
            self.snapshot.morning_brief = MorningBrief.model_validate(data["morning_brief"])
            reminder_ids = [
                int(item["id"])
                for item in data["morning_brief"].get("reminders", [])
                if item.get("id")
            ]
            task_titles = [
                str(item["title"])
                for item in data["morning_brief"].get("tasks", [])
                if item.get("title")
            ]
            self.dialogue_state.set_last_listed_reminder_ids(reminder_ids)
            self.dialogue_state.set_last_listed_task_titles(task_titles)
        if data.get("reminder_id") or data.get("reminder"):
            self.snapshot.reminders_due = self.local_data.list_pending_reminders(limit=5)
        if "reminders" in data:
            reminder_ids = [int(item["id"]) for item in data["reminders"] if item.get("id")]
            self.dialogue_state.set_last_listed_reminder_ids(reminder_ids)
        if "tasks" in data:
            task_titles = [str(item["title"]) for item in data["tasks"] if item.get("title")]
            self.dialogue_state.set_last_listed_task_titles(task_titles)
        if "runtime_muted" in data:
            self.snapshot.runtime_muted = bool(data["runtime_muted"])
            if self.snapshot.runtime_muted and self.snapshot.assistant_state == "idle":
                self.snapshot.assistant_state = "muted"
        if "focus_until" in data:
            self.snapshot.assistant_mode = "focus"
        self.mark_dirty("context", "runtime")

    def _dialogue_context(self) -> dict[str, str]:
        next_event = self.local_data.next_upcoming_event()
        return {
            "last_response": self.dialogue_state.get_last_response() or "",
            "last_announced_reminder_id": str(self.dialogue_state.get_last_announced_reminder_id() or ""),
            "last_listed_reminder_ids": ",".join(str(item) for item in self.dialogue_state.get_last_listed_reminder_ids()),
            "last_listed_task_titles": "||".join(self.dialogue_state.get_last_listed_task_titles()),
            "next_event_starts_at": next_event.starts_at.isoformat() if next_event else "",
            "next_event_title": next_event.title if next_event else "",
        }

    def _tool_preamble(self, request: ToolRequest) -> str:
        return self.brain.tool_preamble(request, self.local_data.preferred_title())

    def _build_conversation_history(self) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        for turn in self.snapshot.conversation_turns:
            if turn.role in ("user", "assistant"):
                history.append({"role": turn.role, "content": turn.text})
        return history

    async def _try_llm_chat(
        self,
        text: str,
        preferred_title: str,
        *,
        context_summary=None,
    ) -> str | None:
        if not self.brain.llm_available:
            self.snapshot.model_route = self.model_router.choose(
                text,
                context_summary=context_summary,
                rag_candidate=self.brain.rag_available,
                llm_available=False,
            )
            self.snapshot.last_model_route = self.snapshot.model_route
            self.snapshot.prompt_cache = self.model_router.cache_snapshot()
            return None
        history = self._build_conversation_history()
        route = self.model_router.choose(
            text,
            context_summary=context_summary,
            rag_candidate=self.brain.rag_available,
            llm_available=True,
        )
        self.snapshot.model_route = route
        self.snapshot.last_model_route = route
        self.snapshot.model_info.routing_strategy = route.strategy
        context_revision = json.dumps(
            self.snapshot.current_context.model_dump(mode="json") if self.snapshot.current_context else {},
            sort_keys=True,
        )
        knowledge_revision = json.dumps(
            {
                "backend": self.snapshot.retrieval_status.backend,
                "index_health": self.snapshot.retrieval_status.index_health,
                "index_version": self.memory.get_index_metadata("index_version", ""),
                "embed_model": self.memory.get_index_metadata("embed_model", ""),
                "built_at": self.memory.get_index_metadata("built_at", ""),
            },
            sort_keys=True,
        )
        memory_revision = self.memory.prompt_cache_revision()
        cached_reply, cache_key = self.model_router.cache_lookup(
            route,
            text,
            history,
            context_revision=context_revision,
            knowledge_revision=knowledge_revision,
            memory_revision=memory_revision,
        )
        if cached_reply:
            self.snapshot.model_route.cache_hit = True
            self.snapshot.last_model_route = self.snapshot.model_route
            self.snapshot.prompt_cache = self.model_router.cache_snapshot()
            return cached_reply
        self.snapshot.prompt_cache = self.model_router.cache_snapshot()
        if self.brain.rag_available and route.used_rag:
            try:
                stream_result = RAGStreamResult()
                tokens: list[str] = []
                async for token in self.brain.generate_rag_reply_stream(
                    text,
                    preferred_title,
                    history,
                    stream_result=stream_result,
                    model_override=route.requested_model,
                ):
                    tokens.append(token)
                    await self.event_hub.publish({"type": "llm_token", "payload": {"token": token}})
                if tokens:
                    self.snapshot.model_route.used_rag = True
                    self.snapshot.last_model_route = self.snapshot.model_route
                    await self.event_hub.publish({"type": "llm_done", "payload": {"rag": True}})
                    if stream_result.sources:
                        await self.event_hub.publish({
                            "type": "rag_sources",
                            "payload": {
                                "sources": [s.model_dump() for s in stream_result.sources],
                                "query": text,
                                "retrieval_hits_count": stream_result.retrieval_hits_count,
                                "reranked_count": stream_result.reranked_count,
                                "context_tokens_used": stream_result.context_tokens_used,
                            },
                        })
                    reply = "".join(tokens)
                    self.model_router.cache_store(cache_key, reply)
                    self.snapshot.prompt_cache = self.model_router.cache_snapshot()
                    return reply
            except NoRetrievalHitsError:
                pass
            except Exception as exc:
                logger.debug("RAG streaming inference failed: %s", exc, exc_info=True)
        try:
            tokens: list[str] = []
            async for token in self.brain.generate_llm_reply_stream(
                text,
                preferred_title,
                history,
                model_override=route.requested_model,
            ):
                tokens.append(token)
                await self.event_hub.publish({"type": "llm_token", "payload": {"token": token}})
            if tokens:
                await self.event_hub.publish({"type": "llm_done", "payload": {}})
                reply = "".join(tokens)
                self.model_router.cache_store(cache_key, reply)
                self.snapshot.prompt_cache = self.model_router.cache_snapshot()
                return reply
        except Exception as exc:
            logger.debug("LLM streaming reply failed: %s", exc, exc_info=True)
        try:
            reply = await self.brain.generate_llm_reply(
                text,
                preferred_title,
                history,
                model_override=route.requested_model,
            )
            if reply and route.requested_model:
                self.snapshot.model_route.fallback_used = False
                self.snapshot.last_model_route = self.snapshot.model_route
                self.model_router.cache_store(cache_key, reply)
                self.snapshot.prompt_cache = self.model_router.cache_snapshot()
            return reply
        except Exception as exc:
            logger.debug("LLM non-streaming reply failed: %s", exc, exc_info=True)
            return None

    async def _llm_summarize_results(
        self,
        user_request: str,
        receipts: list[ExecutionReceipt],
    ) -> str | None:
        if not self.brain.llm_available:
            return None
        tool_results = "\n".join(
            f"- {r.capability_name}: {r.message}" for r in receipts
        )
        title = self.local_data.preferred_title()
        messages = [
            {
                "role": "system",
                "content": (
                    f"You are KERN. Summarize the tool results for the user (address them as '{title}'). "
                    "Be concise and natural."
                ),
            },
            {"role": "user", "content": user_request},
            {
                "role": "assistant",
                "content": f"I executed the following actions:\n{tool_results}",
            },
            {
                "role": "user",
                "content": "Now give a brief, natural summary of what was done.",
            },
        ]
        try:
            result = await self.brain.llm_client.chat(
                messages, max_tokens=256, temperature=0.3
            )
            choices = result.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "").strip()
                if content:
                    return content
        except Exception as exc:
            logger.debug("LLM summarize results failed: %s", exc, exc_info=True)
        return None

    async def _store_response_text(self, text: str, wait: bool = False) -> None:
        self.snapshot.response_text = text
        await asyncio.to_thread(self.memory.append_conversation_summary, text)
        await asyncio.to_thread(self.dialogue_state.set_last_response, text)
        self.mark_dirty("conversation", "runtime")

    def append_turn(
        self,
        role: str,
        text: str,
        kind: str = "message",
        status: str = "complete",
        meta: dict[str, object] | None = None,
    ) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        turn = ConversationTurn(role=role, text=cleaned, kind=kind, status=status, meta=meta or {})
        self.snapshot.conversation_turns = (self.snapshot.conversation_turns + [turn])[-self.MAX_CONVERSATION_TURNS :]
        self.mark_dirty("conversation")

    def reset_conversation(self) -> None:
        self.clear_pending_confirmation()
        self.snapshot.pending_confirmation = None
        self.snapshot.transcript = ""
        self.snapshot.response_text = ""
        self.snapshot.last_action = "Started a new conversation."
        self.snapshot.active_plan = None
        self.snapshot.morning_brief = None
        self.snapshot.proactive_prompt = None
        self.snapshot.conversation_turns = []
        self.dialogue_state.set_last_response("")
        self.mark_dirty("conversation", "context", "runtime")
        self.add_history("system", self.snapshot.last_action)

    def set_pending_confirmation(
        self,
        plan: ExecutionPlan,
        *,
        prompt: str,
        original_utterance: str,
        trigger_source: str,
    ) -> None:
        self._pending_plan = plan
        self._pending_interaction = PendingInteraction(
            kind="confirmation",
            prompt=prompt,
            original_utterance=original_utterance,
            trigger_source=trigger_source,
            plan=plan,
        )

    def clear_pending_confirmation(self) -> None:
        self._pending_plan = None
        self._pending_interaction = None

    def clear_legacy_rollout_prompts(self) -> None:
        legacy_markers = ("jarvis calibration", "kern calibration profile")
        filtered_turns = [
            turn
            for turn in self.snapshot.conversation_turns
            if not any(marker in turn.text.lower() for marker in legacy_markers)
        ]
        if len(filtered_turns) != len(self.snapshot.conversation_turns):
            self.snapshot.conversation_turns = filtered_turns
            self.mark_dirty("conversation")
        if self.snapshot.proactive_prompt and any(
            marker in self.snapshot.proactive_prompt.message.lower() for marker in legacy_markers
        ):
            self.snapshot.proactive_prompt = None
            self.mark_dirty("runtime")
        if any(marker in self.snapshot.response_text.lower() for marker in legacy_markers):
            self.snapshot.response_text = ""
            self.mark_dirty("runtime")
        if any(marker in self.snapshot.last_action.lower() for marker in legacy_markers):
            self.snapshot.last_action = "Cleared legacy rollout prompt."
            self.mark_dirty("runtime")
        if any(marker in self._last_proactive_message.lower() for marker in legacy_markers):
            self._last_proactive_message = ""

    def add_history(self, category: str, message: str) -> None:
        entry = ActionHistoryEntry(timestamp=datetime.now(timezone.utc), category=category, message=message)
        self.snapshot.action_history = ([entry] + self.snapshot.action_history)[: self.MAX_ACTION_HISTORY]
        self.mark_dirty("runtime")
        self.memory.append_runtime_log(category, message)
