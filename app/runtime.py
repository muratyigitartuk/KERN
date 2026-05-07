from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

from app.action_planner import ActionPlanner
from app.artifacts import ArtifactStore
from app.knowledge_graph import KnowledgeGraphService
from app.backup import BackupService
from app.config import settings
from app.database import connect
from app.network_monitor import NetworkMonitor
from app.scheduler import SchedulerService
from app.attention import CalendarWatcher, DocumentWatcher, FileWatcher
from app.current_context import CurrentContextService, WindowsClipboardClient
from app.dialogue import DialogueStateStore
from app.documents import DocumentService
from app.events import EventHub
from app.german_business import GermanBusinessService
from app.embeddings import EmbeddingService
from app.identity import IdentityService
from app.llm import Brain
from app.llm_client import LlamaServerClient
from app.license_service import LicenseService
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.orchestrator import KernOrchestrator
from app.platform import PlatformStore, connect_platform_db
from app.policy import PolicyEngine
from app.rag import RAGPipeline
from app.readiness import build_readiness_report
from app.reranker import LLMReranker, ScoreFusionReranker
from app.retention import RetentionService
from app.retrieval import RetrievalService
from app.reminders import NotificationService, ReminderService
from app.sample_workspace import SampleWorkspaceService
from app.syncing import SyncService
from app.tools.calendar import CalendarService
from app.tools.tasks import TaskService
from app.types import (
    AuthContext,
    DomainStatus,
    FailureStateSnapshot,
    LicenseSummarySnapshot,
    ModelFallbackState,
    OnboardingSnapshot,
    ProfileSession,
    ReadinessCheckSnapshot,
    ReadinessSummarySnapshot,
    SecurityStatusSnapshot,
    ToolRequest,
    TrustSummarySnapshot,
    UpdateStateSnapshot,
)
from app.update_state import load_update_state


def _app_version() -> str:
    try:
        return package_version("kern")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if pyproject.exists():
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("version ="):
                    return stripped.split("=", 1)[1].strip().strip('"')
        return "0.1.0"


class KernRuntime:
    def __init__(self, *, profile_slug: str = "default") -> None:
        self._started = False
        self.defer_retrieval_refresh_after_upload = True
        platform_connection = connect_platform_db(settings.system_db_path)
        self.platform = PlatformStore(platform_connection, audit_enabled=settings.audit_enabled)
        self.identity_service = IdentityService(self.platform)
        existing_profile = self.platform.get_profile(profile_slug)
        if existing_profile is not None:
            self.active_profile = existing_profile
            for path in (
                self.active_profile.profile_root,
                self.active_profile.documents_root,
                self.active_profile.attachments_root,
                self.active_profile.archives_root,
                self.active_profile.meetings_root,
                self.active_profile.backups_root,
            ):
                Path(path).mkdir(parents=True, exist_ok=True)
        else:
            self.active_profile = self.platform.ensure_default_profile(
                profile_root=settings.profile_root,
                backup_root=settings.backup_root,
                legacy_db_path=settings.db_path,
                title="Primary profile" if profile_slug == "default" else f"Workspace {profile_slug}",
                slug=profile_slug,
            )
        event_hub = EventHub()
        self._llm_client: LlamaServerClient | None = None
        if settings.llm_enabled:
            self._llm_client = LlamaServerClient(
                base_url=settings.llama_server_url,
                timeout=settings.llama_server_timeout,
                default_model=settings.llm_model,
            )
        brain = Brain(
            settings.openai_api_key,
            allow_cloud_llm=settings.allow_cloud_llm,
            local_mode_enabled=settings.local_mode_enabled,
            cognition_backend=settings.cognition_backend,
            cognition_model=settings.cognition_model,
            llm_client=self._llm_client,
        )
        policy = PolicyEngine(
            mode=settings.policy_mode,
            allow_external_network=settings.policy_allow_external_network,
        )
        self.event_hub = event_hub
        self.brain = brain
        self.policy = policy
        self.backup_service = BackupService()
        self.profile_session = ProfileSession(profile_slug=self.active_profile.slug, unlocked=not self.platform.is_profile_locked(self.active_profile.slug))
        self.license_service = LicenseService()
        self.profile_connection = None
        self.memory = None
        self.local_data = None
        self.document_service = None
        self.retrieval_service = None
        self.german_business_service = None
        self.sync_service = None
        self.notification_service = None
        self.reminder_service = None
        self.retention_service = None
        self.sample_workspace_service = None
        self.artifact_store = ArtifactStore(self.platform, self.active_profile)
        self.current_context_service = CurrentContextService(
            clipboard_client=WindowsClipboardClient(max_chars=settings.context_clipboard_max_chars),
            window_enabled=settings.context_window_enabled,
            clipboard_enabled=settings.context_clipboard_enabled,
        )
        self._locked_scaffold_path = Path(self.active_profile.profile_root) / ".locked-session.db"
        self._using_locked_scaffold = False
        self._bind_profile_stack(use_locked_scaffold=not self.profile_session.unlocked)
        self.audit_chain_ok = True
        self.audit_chain_reason: str | None = None
        self.last_audit_verification_at: datetime | None = None
        self.proactive_enabled = settings.proactive_enabled
        self.scheduler_service: SchedulerService | None = None
        self._file_watcher: FileWatcher | None = None
        self._calendar_watcher: CalendarWatcher | None = None
        self._document_watcher: DocumentWatcher | None = None
        self._pending_proactive_alerts: list[dict] = []
        self.knowledge_graph_service = None
        self.action_planner = ActionPlanner()
        allowed_hosts = {host.strip() for host in settings.network_allowed_hosts.split(",") if host.strip()}
        self.network_monitor = NetworkMonitor(
            platform=self.platform,
            profile_slug=self.active_profile.slug,
            interval_seconds=settings.network_monitor_interval,
            enabled=settings.network_monitor_enabled,
            allowed_hosts=allowed_hosts,
        )
        self._monitor_task: asyncio.Task | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._last_retention_check_at = 0.0
        self._workspace_lock = asyncio.Lock()
        self._last_monitor_tick_at: datetime | None = None
        self._background_errors: list[str] = []
        self._manual_failures: dict[str, FailureStateSnapshot] = {}
        self._last_snapshot_payload: str = ""
        self._last_heartbeat = 0.0
        self._last_broadcast_at = 0.0
        self._monitor_cycles = 0
        self._snapshots_sent = 0
        self._snapshots_skipped = 0
        cognition_model_path = Path(settings.cognition_model).name if settings.cognition_model else None
        hybrid_details = [
            "Rule engine safety net",
            "Semantic paraphrase matcher",
            "Heuristic multi-step planner",
        ]
        if brain.cognition_backend == "llama_cpp":
            hybrid_details[-1] = "llama.cpp local planner"
        self.orchestrator.snapshot.model_info.app_version = _app_version()
        self.orchestrator.snapshot.model_info.app_name = "KERN"
        self.orchestrator.snapshot.model_info.cognition_name = (
            cognition_model_path
            or ("llama.cpp local planner" if brain.cognition_backend == "llama_cpp" else "Heuristic local planner")
        )
        self.orchestrator.snapshot.model_info.cognition_type = (
            "llama.cpp gguf" if brain.cognition_backend == "llama_cpp" else "hybrid local stack"
        )
        self.orchestrator.snapshot.model_info.cognition_backend = brain.cognition_backend
        self.orchestrator.snapshot.model_info.hybrid_details = hybrid_details
        self.orchestrator.snapshot.model_info.cognition_model_path = settings.cognition_model
        self.orchestrator.snapshot.model_info.embed_model = settings.embed_model
        self.orchestrator.snapshot.model_info.cloud_available = brain.cloud_available
        self.orchestrator.snapshot.model_info.model_mode = settings.model_mode
        self.orchestrator.snapshot.model_info.fast_model_path = settings.fast_model_path
        self.orchestrator.snapshot.model_info.deep_model_path = settings.deep_model_path
        self.orchestrator.snapshot.model_info.routing_strategy = "single" if settings.model_mode in {"", "off"} else settings.model_mode
        self.orchestrator.snapshot.model_info.preferred_runtime = self._preferred_runtime_label()
        self.orchestrator.snapshot.model_info.preferred_runtime_detail = self._preferred_runtime_detail()
        self._refresh_platform_snapshot_sync()
        self.refresh_interaction_snapshot()

    def _preferred_runtime_path(self) -> str | None:
        return (
            settings.llama_server_model_path
            or settings.deep_model_path
            or settings.fast_model_path
            or settings.cognition_model
        )

    def _preferred_runtime_label(self) -> str:
        if settings.llama_server_model_path:
            return "Local GGUF via llama.cpp"
        if settings.deep_model_path or settings.fast_model_path:
            return "Local routed model path"
        if settings.cognition_model:
            return "Local cognition model"
        return "Configured local model"

    def _preferred_runtime_detail(self) -> str:
        if settings.llama_server_model_path:
            return "Recommended for internal pilots: one local model endpoint with predictable behavior."
        if settings.deep_model_path or settings.fast_model_path:
            return "Recommended for internal pilots when fast/deep routing is already managed locally."
        if settings.cognition_model:
            return "Recommended local path for the current install."
        return "Confirm the local model path before starting the first drafting workflow."

    def _build_onboarding_snapshot(self) -> OnboardingSnapshot:
        return OnboardingSnapshot(
            active=False,
            completed=True,
            current_step="done",
            storage_confirmed=True,
            model_choice="local",
            starter_workflow="document_grounded_draft",
            selected_path="real_documents",
            sample_workspace_active=False,
            sample_workspace_seeded=False,
            title="",
            body="",
            primary_action="",
            secondary_action="",
            local_data_note=f"Profile root: {self.active_profile.profile_root}",
            model_note=self._preferred_runtime_label(),
            workflow_note="Primary workflow: draft a German business reply from local documents.",
            activation_note="",
            storage_path=self.active_profile.documents_root,
            model_path=self._preferred_runtime_path(),
        )

    def _build_trust_summary(self) -> TrustSummarySnapshot:
        network_status = self.orchestrator.snapshot.network_status.status or "checking"
        readiness = self.orchestrator.snapshot.readiness_summary.headline or (
            "Ready for the first grounded draft." if self.orchestrator.snapshot.llm_available else "Local model path needs attention before drafting."
        )
        if not self.profile_session.unlocked:
            readiness = "Unlock the active profile before starting the first workflow."
        local_posture = {
            "isolated": "Local-only posture confirmed.",
            "unmonitored": "Running locally, but outbound monitoring is disabled.",
            "network_detected": "Unexpected outbound activity needs review.",
        }.get(network_status, "Checking local runtime posture.")
        return TrustSummarySnapshot(
            local_posture=local_posture,
            storage_posture=f"Documents stay under {self.active_profile.documents_root}",
            model_posture=f"{self._preferred_runtime_label()} / {self._preferred_runtime_path() or 'path not configured'}",
            recovery_posture=f"Encrypted backups go to {self.active_profile.backups_root}",
            readiness_posture=readiness,
        )

    def build_readiness_snapshot(self) -> tuple[ReadinessSummarySnapshot, list[ReadinessCheckSnapshot]]:
        report = build_readiness_report(runtime_url=settings.llama_server_url)
        summary = ReadinessSummarySnapshot(
            status=str(report["status"]),
            headline=str(report["headline"]),
            warnings=len(report["warnings"]),
            errors=len(report["errors"]),
        )
        checks = [
            ReadinessCheckSnapshot(
                id=str(item.get("id", "")),
                label=str(item.get("label", "")),
                severity=str(item.get("severity", "info")),
                status=str(item.get("status", "pass")),
                why_it_matters=str(item.get("why_it_matters", "")),
                operator_action=str(item.get("operator_action", "")),
                details=str(item.get("details", "")),
            )
            for item in report.get("checks", [])
        ]
        return summary, checks

    def _license_summary_snapshot(self) -> LicenseSummarySnapshot:
        evaluation = self.license_service.evaluate()
        if evaluation.production_access:
            self.clear_failure("license_required", "license_expired", "license_invalid")
        else:
            failure_id = "license_expired" if evaluation.status == "expired" else "license_invalid" if evaluation.status == "invalid" else "license_required"
            self.record_failure(
                error_code=failure_id,
                title="Production access is blocked",
                message=evaluation.message,
                blocked_scope="production workspace",
                next_action=evaluation.renewal_hint or "Install a valid offline license.",
                retry_available=True,
                retry_action="rerun_license_check",
                technical_detail=evaluation.source_path,
                source="license",
            )
        return LicenseSummarySnapshot(
            status=evaluation.status,
            plan=evaluation.plan,
            activation_mode=evaluation.activation_mode,
            expires_at=evaluation.expires_at,
            grace_state=evaluation.grace_state,
            message=evaluation.message,
            renewal_hint=evaluation.renewal_hint,
            production_access=evaluation.production_access,
            sample_access=evaluation.sample_access,
            install_id=evaluation.install_id,
            source_path=evaluation.source_path,
        )

    def _update_state_snapshot(self) -> UpdateStateSnapshot:
        state = load_update_state()
        self.local_data.update_update_history_state(**state)
        last_status = state["last_status"] if state["last_status"] in {"idle", "succeeded", "failed", "rollback_performed"} else "idle"
        if last_status == "failed":
            self.record_failure(
                error_code="update_failed",
                title="Last KERN update failed",
                message="The last stable-channel update did not complete cleanly.",
                blocked_scope="update path",
                next_action="Review the operator update guide and restore from the recorded backup if needed.",
                retry_available=False,
                technical_detail=state["last_error"],
                source="update",
            )
        else:
            self.clear_failure("update_failed")
        message = "Manual stable-channel updates only."
        if last_status == "succeeded" and state["last_success_at"]:
            message = "Last stable update completed successfully."
        elif last_status == "rollback_performed":
            message = "Rollback was performed on the last update attempt."
        elif last_status == "failed":
            message = "Last stable update failed. Review the recorded backup and restore state."
        return UpdateStateSnapshot(
            policy="Manual stable-channel updates only.",
            channel=settings.update_channel,
            app_version=getattr(self.orchestrator.snapshot.model_info, "app_version", _app_version()),
            last_attempt_at=state["last_attempt_at"],
            last_success_at=state["last_success_at"],
            last_backup_at=state["last_backup_at"],
            last_restore_attempt_at=state["last_restore_attempt_at"],
            last_status=last_status,
            last_error=state["last_error"],
            message=message,
        )

    def ensure_production_access(self, *, blocked_scope: str) -> bool:
        evaluation = self.license_service.evaluate()
        if evaluation.production_access:
            self.clear_failure("license_required", "license_expired", "license_invalid")
            return True
        error_code = "license_expired" if evaluation.status == "expired" else "license_invalid" if evaluation.status == "invalid" else "license_required"
        self.record_failure(
            error_code=error_code,
            title="Production access is blocked",
            message=evaluation.message,
            blocked_scope=blocked_scope,
            next_action=evaluation.renewal_hint or "Import a valid offline license.",
            retry_available=True,
            retry_action="rerun_license_check",
            technical_detail=evaluation.source_path,
            source="license",
        )
        return False

    def start_sample_workspace(self) -> list[object]:
        records = self.sample_workspace_service.seed()
        self.orchestrator.snapshot.last_action = "Sample workspace is ready."
        self.orchestrator.snapshot.response_text = "Sample documents are ready. Review the grounded drafting flow before switching to your own files."
        self.orchestrator.mark_dirty("runtime")
        self.clear_failure("license_required")
        return records

    def start_real_workspace(self) -> int:
        archived = self.sample_workspace_service.exit()
        self.orchestrator.snapshot.last_action = "Sample workspace closed."
        self.orchestrator.snapshot.response_text = "KERN switched back to the real local-document path."
        self.orchestrator.mark_dirty("runtime")
        return archived

    def record_failure(
        self,
        *,
        error_code: str,
        title: str,
        message: str,
        blocked_scope: str,
        next_action: str,
        data_safe: bool = True,
        retry_available: bool = False,
        retry_action: str | None = None,
        technical_detail: str = "",
        source: str = "runtime",
        failure_id: str | None = None,
    ) -> FailureStateSnapshot:
        failure = FailureStateSnapshot(
            id=failure_id or error_code,
            error_code=error_code,
            title=title,
            message=message,
            data_safe=data_safe,
            blocked_scope=blocked_scope,
            retry_available=retry_available,
            retry_action=retry_action,
            next_action=next_action,
            technical_detail=technical_detail,
            source=source,
        )
        self._manual_failures[failure.id] = failure
        self.orchestrator.mark_dirty("runtime")
        return failure

    def clear_failure(self, *failure_ids: str) -> None:
        for failure_id in failure_ids:
            self._manual_failures.pop(failure_id, None)
        self.orchestrator.mark_dirty("runtime")

    def _background_job_failures(self, jobs: list[object]) -> list[FailureStateSnapshot]:
        failures: list[FailureStateSnapshot] = []
        for job in jobs:
            if getattr(job, "status", "") not in {"failed", "recoverable"}:
                continue
            error_code = str(getattr(job, "error_code", None) or f"{job.job_type}_failed")
            failures.append(
                FailureStateSnapshot(
                    id=f"job:{job.id}",
                    error_code=error_code,
                    title=str(job.title or "KERN operation failed"),
                    message=str(job.error_message or job.detail or "The operation did not complete."),
                    data_safe=True,
                    blocked_scope=str(job.job_type or "operation"),
                    retry_available=bool(getattr(job, "recoverable", False)),
                    retry_action="rerun_readiness" if error_code in {"backup_failed", "backup_restore_failed", "update_failed"} else None,
                    next_action="Open the operator details, review the failure, and retry only after the readiness state is clean.",
                    technical_detail=str(job.detail or ""),
                    source="background_job",
                )
            )
        return failures

    def _readiness_failures(self, checks: list[ReadinessCheckSnapshot]) -> list[FailureStateSnapshot]:
        mapping = {
            "model_path": ("model_path_invalid", "Local model path needs attention", "Drafting from local documents is blocked until the preferred model path is fixed."),
            "local_runtime": ("local_runtime_unreachable", "Local runtime is unreachable", "Grounded drafting is blocked until the local runtime responds again."),
            "profile_storage": ("storage_not_writable", "Profile storage is not writable", "KERN cannot safely store indexed documents or profile data in the current location."),
            "backup_storage": ("storage_not_writable", "Backup storage is not writable", "Encrypted backups and rollback bundles cannot be written to the current backup location."),
        }
        failures: list[FailureStateSnapshot] = []
        for check in checks:
            if check.status != "fail" or check.id not in mapping:
                continue
            error_code, title, message = mapping[check.id]
            failures.append(
                FailureStateSnapshot(
                    id=f"readiness:{check.id}",
                    error_code=error_code,
                    title=title,
                    message=message,
                    data_safe=True,
                    blocked_scope="pilot workflow",
                    retry_available=True,
                    retry_action="rerun_readiness",
                    next_action=check.operator_action,
                    technical_detail=check.details,
                    source="readiness",
                )
            )
        return failures

    def build_failure_snapshot(
        self,
        checks: list[ReadinessCheckSnapshot],
        jobs: list[object],
    ) -> tuple[list[FailureStateSnapshot], FailureStateSnapshot | None]:
        failures: dict[str, FailureStateSnapshot] = {}
        for failure in self._readiness_failures(checks) + self._background_job_failures(jobs) + list(self._manual_failures.values()):
            failures[failure.id] = failure
        ordered = list(failures.values())
        recoverable = next((failure for failure in ordered if failure.retry_available), None)
        return ordered, recoverable

    def _open_profile_connection(self):
        if self.platform.is_profile_locked(self.active_profile.slug):
            raise PermissionError("Unlock the active KERN profile before opening the protected profile database.")
        if settings.artifact_encryption_enabled:
            self.platform.ensure_profile_artifact_encryption(self.active_profile.slug)
        if settings.db_encryption_mode != "off":
            security_state = self.platform.ensure_profile_db_encryption(self.active_profile.slug, mode=settings.db_encryption_mode)
            encryption_key = self.platform.resolve_secret(
                str(security_state.get("db_key_ref") or ""),
                profile_slug=self.active_profile.slug,
                allow_locked=True,
                audit=False,
            )
            if not encryption_key:
                raise RuntimeError("Encrypted profile database key is unavailable for the active profile.")
            self._validate_fernet_key(encryption_key, security_state.get("db_key_ref", ""))
            connection = connect(
                Path(self.active_profile.db_path),
                encryption_mode=settings.db_encryption_mode,
                encryption_key=encryption_key,
                key_version=int(security_state.get("key_version") or 0),
                key_derivation_version=settings.key_derivation_version,
            )
            return connection, security_state
        security_state = self.platform.get_profile_security_state(self.active_profile.slug)
        return connect(Path(self.active_profile.db_path)), security_state

    def _open_locked_scaffold_connection(self):
        if self._locked_scaffold_path.exists():
            self._locked_scaffold_path.unlink(missing_ok=True)
        return connect(self._locked_scaffold_path), self.platform.get_profile_security_state(self.active_profile.slug)

    def _close_profile_stack(self) -> None:
        self._stop_watchers()
        if self.profile_connection is not None:
            with contextlib.suppress(Exception):  # cleanup â€” best-effort
                self.profile_connection.close()
        self.profile_connection = None
        self.memory = None
        self.retention_service = None

    def _switch_active_profile(self, workspace_slug: str) -> None:
        if workspace_slug == self.active_profile.slug:
            return
        profile = self.platform.get_profile(workspace_slug)
        if profile is None:
            raise RuntimeError(f"Unknown workspace: {workspace_slug}")
        self.active_profile = profile
        self.profile_session = ProfileSession(
            profile_slug=profile.slug,
            unlocked=not self.platform.is_profile_locked(profile.slug),
        )
        self._locked_scaffold_path = Path(self.active_profile.profile_root) / ".locked-session.db"
        self.network_monitor.profile_slug = self.active_profile.slug
        self._bind_profile_stack(use_locked_scaffold=not self.profile_session.unlocked)
        self._refresh_platform_snapshot_sync()

    @contextlib.asynccontextmanager
    async def workspace_context(self, auth_context: AuthContext | None):
        async with self._workspace_lock:
            target_workspace = (
                auth_context.workspace_slug
                if auth_context is not None and auth_context.workspace_slug
                else self.active_profile.slug
            )
            if target_workspace:
                await asyncio.to_thread(self._switch_active_profile, target_workspace)
            yield

    def _stop_watchers(self) -> None:
        file_watcher = getattr(self, "_file_watcher", None)
        if file_watcher is not None:
            with contextlib.suppress(Exception):  # cleanup â€” best-effort
                file_watcher.stop()
        self._file_watcher = None
        self._calendar_watcher = None
        self._document_watcher = None

    def _validate_fernet_key(self, key: str, key_ref: str) -> None:
        import binascii
        try:
            from cryptography.fernet import Fernet
            Fernet(key.encode("ascii"))
        except (ValueError, binascii.Error) as exc:
            logger.error("Invalid Fernet key for ref %s: %s", key_ref, exc)
            raise RuntimeError(f"Invalid Fernet key for ref {key_ref}: {exc}") from exc

    def _bind_profile_stack(self, *, use_locked_scaffold: bool) -> None:
        self._close_profile_stack()
        if use_locked_scaffold:
            connection, security_state = self._open_locked_scaffold_connection()
            self._using_locked_scaffold = True
        else:
            connection, security_state = self._open_profile_connection()
            self._using_locked_scaffold = False
            if self._locked_scaffold_path.exists():
                self._locked_scaffold_path.unlink(missing_ok=True)
        memory = MemoryRepository(connection, profile_slug=self.active_profile.slug)
        local_data = LocalDataService(memory, settings.user_title)
        dialogue = DialogueStateStore(memory)
        reminder_service = ReminderService(local_data)
        notification_service = NotificationService()
        task_service = TaskService(local_data)
        calendar_service = CalendarService(local_data)
        embedding_service: EmbeddingService | None = None
        if settings.vec_enabled and settings.embed_model_path:
            embedding_service = EmbeddingService(settings.embed_model_path)
        self._embedding_service = embedding_service
        retrieval_service = RetrievalService(
            memory,
            platform=self.platform,
            profile_slug=self.active_profile.slug,
            embedding_service=embedding_service,
        )
        reranker = None
        if settings.rag_reranker_backend == "llm" and self._llm_client:
            reranker = LLMReranker(self._llm_client)
        else:
            reranker = ScoreFusionReranker()
        rag_pipeline = RAGPipeline(
            retrieval=retrieval_service,
            reranker=reranker,
            llm_client=self._llm_client,
        )
        self.brain._rag = rag_pipeline
        document_service = DocumentService(connection, self.platform, self.active_profile, retrieval=retrieval_service)
        german_business_service = GermanBusinessService(connection, self.platform, self.active_profile, local_data, document_service, retrieval_service)
        sync_service = SyncService(memory, self.active_profile, nextcloud_url=settings.nextcloud_url, platform=self.platform)
        if settings.scheduler_enabled and not use_locked_scaffold:
            self.scheduler_service = SchedulerService(
                connection,
                self.active_profile.slug,
                retry_delay_minutes=settings.scheduler_retry_delay_minutes,
                max_retries=settings.scheduler_max_retries,
                stale_run_minutes=settings.scheduler_stale_run_minutes,
            )
        else:
            self.scheduler_service = None
        if not use_locked_scaffold:
            watch_dirs = [Path(d.strip()) for d in settings.file_watch_dirs.split(",") if d.strip()]
            self._file_watcher = FileWatcher(
                watch_dirs,
                document_service,
                self.active_profile.slug,
                self.platform,
                connection=connection,
            )
            self._file_watcher.start()
            self._calendar_watcher = CalendarWatcher(local_data, settings.proactive_scan_interval)
            self._document_watcher = DocumentWatcher(document_service, self.active_profile.slug, settings.proactive_scan_interval)
        else:
            self._stop_watchers()
        self.knowledge_graph_service = KnowledgeGraphService(connection, self.active_profile.slug) if not use_locked_scaffold else None
        if self.knowledge_graph_service and document_service:
            document_service.knowledge_graph = self.knowledge_graph_service
        self.profile_connection = connection
        self.memory = memory
        self.local_data = local_data
        self.document_service = document_service
        self.retrieval_service = retrieval_service
        self.german_business_service = german_business_service
        self.sync_service = sync_service
        self.notification_service = notification_service
        self.reminder_service = reminder_service
        self.retention_service = RetentionService(memory, self.platform, self.active_profile) if not use_locked_scaffold else None
        self.sample_workspace_service = SampleWorkspaceService(
            local_data=local_data,
            documents=document_service,
            memory=memory,
            retrieval=retrieval_service,
        )
        self.security_state = security_state
        self.artifact_store = ArtifactStore(self.platform, self.active_profile)
        self.orchestrator = KernOrchestrator(
            event_hub=self.event_hub,
            memory=memory,
            brain=self.brain,
            local_data=local_data,
            policy=self.policy,
            task_service=task_service,
            calendar_service=calendar_service,
            document_service=document_service,
            german_business_service=german_business_service,
            sync_service=sync_service,
            default_title=settings.user_title,
            dialogue_state=dialogue,
            reminder_service=reminder_service,
            platform_store=self.platform,
            active_profile=self.active_profile,
            backup_service=self.backup_service,
            scheduler_service=self.scheduler_service,
            file_watcher=self._file_watcher,
            knowledge_graph_service=self.knowledge_graph_service,
            current_context_service=self.current_context_service,
        )

    def lock_active_profile(self, reason: str) -> ProfileSession:
        session = self.platform.lock_profile(self.active_profile.slug, reason=reason)
        self.profile_session = session
        self._bind_profile_stack(use_locked_scaffold=True)
        return session

    def unlock_active_profile(self, pin: str | None = None) -> ProfileSession:
        session = self.platform.unlock_profile(self.active_profile.slug, pin)
        self.profile_session = session
        if session.unlocked:
            self._bind_profile_stack(use_locked_scaffold=False)
            self.platform.warm_profile_keys(self.active_profile.slug)
        return session

    async def resume_unlocked_profile_runtime(self) -> None:
        if not self.profile_session.unlocked:
            return
        await self.orchestrator.initialize()
        if self.scheduler_service:
            await asyncio.to_thread(self.scheduler_service.recover_stale_runs)
        if settings.artifact_encryption_enabled:
            await asyncio.to_thread(self.platform.ensure_profile_artifact_encryption, self.active_profile.slug)
            await asyncio.to_thread(self._migrate_artifacts_if_needed)
        if settings.rag_enabled:
            await asyncio.to_thread(self.retrieval_service.rebuild_index, self.local_data.memory_scope())
        if settings.vec_enabled:
            await asyncio.to_thread(self.retrieval_service.rebuild_vec_index, self.local_data.memory_scope())
        if hasattr(self.retrieval_service, "recover_jobs"):
            await asyncio.to_thread(self.retrieval_service.recover_jobs)
        if hasattr(self.document_service, "recover_jobs"):
            await asyncio.to_thread(self.document_service.recover_jobs)
        if hasattr(self.german_business_service, "recover_jobs"):
            await asyncio.to_thread(self.german_business_service.recover_jobs)
        await asyncio.to_thread(self.sync_service.recover_jobs)

    async def start(self) -> None:
        if self._started:
            return
        self._main_loop = asyncio.get_running_loop()
        if self._llm_client:
            await self._llm_client.startup()
            await self.refresh_llm_status(retries=20)
        await self.orchestrator.initialize()
        repaired_audit_chain = await asyncio.to_thread(self.platform.repair_legacy_audit_chain)
        if repaired_audit_chain:
            await asyncio.to_thread(
                self.platform.record_audit,
                "security",
                "repair_legacy_audit_chain",
                "success",
                "Rebuilt legacy audit hashes for retained events.",
                profile_slug=self.active_profile.slug,
            )
        if settings.product_posture == "production":
            cleanup_result = await asyncio.to_thread(self.local_data.cleanup_rollout_legacy_assistant_state)
            self.orchestrator.clear_legacy_rollout_prompts()
            self.orchestrator.dialogue_state.set_last_announced_reminder_id(None)
            if any(int(value or 0) > 0 for value in cleanup_result.values()):
                await asyncio.to_thread(
                    self.platform.record_audit,
                    "runtime",
                    "cleanup_rollout_legacy_state",
                    "info",
                    "Cleared legacy assistant-era rollout prompts and reminders.",
                    profile_slug=self.active_profile.slug,
                    details=cleanup_result,
                )
        await asyncio.to_thread(self.verify_audit_chain, "startup")
        if self.profile_session.unlocked and self.scheduler_service:
            await asyncio.to_thread(self.scheduler_service.recover_stale_runs)
        if self.profile_session.unlocked and settings.artifact_encryption_enabled:
            await asyncio.to_thread(self.platform.ensure_profile_artifact_encryption, self.active_profile.slug)
            if self.profile_session.unlocked:
                await asyncio.to_thread(self._migrate_artifacts_if_needed)
        if self.profile_session.unlocked and settings.rag_enabled:
            await asyncio.to_thread(self.retrieval_service.rebuild_index, self.local_data.memory_scope())
        if self.profile_session.unlocked and settings.vec_enabled:
            await asyncio.to_thread(self.retrieval_service.rebuild_vec_index, self.local_data.memory_scope())
        await asyncio.to_thread(self._recover_backup_jobs)
        if self.profile_session.unlocked and hasattr(self.retrieval_service, "recover_jobs"):
            await asyncio.to_thread(self.retrieval_service.recover_jobs)
        if self.profile_session.unlocked and hasattr(self.document_service, "recover_jobs"):
            await asyncio.to_thread(self.document_service.recover_jobs)
        if self.profile_session.unlocked and hasattr(self.german_business_service, "recover_jobs"):
            await asyncio.to_thread(self.german_business_service.recover_jobs)
        if self.profile_session.unlocked:
            await asyncio.to_thread(self.sync_service.recover_jobs)
            await self._run_retention(force=True, reason="startup")
        await asyncio.to_thread(
            self.platform.record_audit,
            "runtime",
            "startup",
            "info",
            "KERN runtime started.",
            profile_slug=self.active_profile.slug,
        )
        await asyncio.to_thread(self._refresh_platform_snapshot_sync)
        self.refresh_interaction_snapshot()
        await self.broadcast_if_changed(force=True, reason="startup")
        self._monitor_task = asyncio.create_task(self.monitor_runtime())
        self._started = True

    async def stop(self) -> None:
        self._started = False
        if self._monitor_task:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
        if self._llm_client:
            await self._llm_client.shutdown()
        await asyncio.to_thread(
            self.platform.record_audit,
            "runtime",
            "shutdown",
            "info",
            "KERN runtime stopped.",
            profile_slug=self.active_profile.slug,
        )
        await asyncio.to_thread(self._close_profile_stack)
        self._main_loop = None
        if self._locked_scaffold_path.exists():
            self._locked_scaffold_path.unlink(missing_ok=True)

    def verify_audit_chain(self, source: str) -> tuple[bool, str | None]:
        try:
            ok, reason = self.platform.verify_audit_chain()
        except Exception as exc:
            ok, reason = False, str(exc)
        self.audit_chain_ok = ok
        self.audit_chain_reason = reason
        self.last_audit_verification_at = datetime.now(timezone.utc)
        self.platform.record_audit(
            "security",
            "audit_chain_verification",
            "success" if ok else "failure",
            "Audit chain verified." if ok else (reason or "Audit chain verification failed."),
            profile_slug=self.active_profile.slug,
            details={"source": source, "reason": reason},
        )
        return ok, reason

    def _migrate_artifacts_if_needed(self) -> None:
        if not settings.artifact_encryption_enabled:
            return
        state = self.platform.get_profile_security_state(self.active_profile.slug)
        migration_state = str(state.get("artifact_encryption_migration_state") or "pending")
        if migration_state == "completed":
            return
        job = self.platform.create_job(
            "artifact_encryption_migration",
            "Encrypt profile artifacts",
            profile_slug=self.active_profile.slug,
            detail="Migrating plaintext artifacts into encrypted storage.",
            payload={"state": migration_state},
        )
        self.platform.update_job(job.id, status="running", progress=0.1, checkpoint_stage="artifact_migration_started")
        self.platform.set_artifact_migration_state(self.active_profile.slug, "migrating")
        try:
            moved = self.artifact_store.migrate_profile_artifacts(self.profile_connection)
            self.platform.update_checkpoint(job.id, "artifact_migration_completed", {"migrated_count": moved})
            self.platform.update_job(
                job.id,
                status="completed",
                recoverable=False,
                progress=1.0,
                checkpoint_stage="artifact_migration_completed",
                detail=f"Migrated {moved} artifact(s).",
                result={"migrated_count": moved},
            )
            self.platform.set_artifact_migration_state(self.active_profile.slug, "completed")
            self.platform.record_audit(
                "security",
                "artifact_encryption_migration",
                "success",
                f"Migrated {moved} artifact(s) into encrypted storage.",
                profile_slug=self.active_profile.slug,
                details={"migrated_count": moved},
            )
        except Exception as exc:
            self.platform.set_artifact_migration_state(self.active_profile.slug, "failed")
            self.platform.update_job(
                job.id,
                status="failed",
                recoverable=True,
                checkpoint_stage="artifact_migration_failed",
                detail=str(exc),
                error_code="artifact_migration_failed",
                error_message=str(exc),
            )
            self.platform.record_audit(
                "security",
                "artifact_encryption_migration",
                "failure",
                str(exc),
                profile_slug=self.active_profile.slug,
            )

    def _recover_backup_jobs(self) -> None:
        for job in self.platform.list_jobs(self.active_profile.slug, limit=20):
            if not job.recoverable:
                continue
            if job.job_type == "restore_backup":
                plan_payload = {}
                for checkpoint in self.platform.list_checkpoints(job.id):
                    payload_row = self.platform.connection.execute(
                        "SELECT payload_json FROM recovery_checkpoints WHERE job_id = ? AND stage = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
                        (job.id, checkpoint.stage),
                    ).fetchone()
                    if payload_row:
                        try:
                            plan_payload.update(json.loads(payload_row["payload_json"]))
                        except Exception as exc:
                            logger.debug("Failed to parse checkpoint payload JSON: %s", exc)
                staged_root = str(plan_payload.get("staged_root", "") or "")
                final_root = str(plan_payload.get("final_root", "") or "")
                requested_root = str(plan_payload.get("requested_root", final_root or job.payload.get("restore_root", "")) or "")
                if staged_root and final_root and requested_root:
                    from app.types import RestorePlan

                    plan = RestorePlan(
                        backup_path=str(job.payload.get("backup_path", "")),
                        requested_root=requested_root,
                        staged_root=staged_root,
                        final_root=final_root,
                        profile_slug=self.active_profile.slug,
                    )
                    with contextlib.suppress(Exception):  # cleanup â€” best-effort
                        self.backup_service.cleanup_restore_plan(plan)
                self.platform.update_job(
                    job.id,
                    status="rolled_back",
                    recoverable=False,
                    checkpoint_stage="rolled_back",
                    detail="Rolled back interrupted backup restore on startup.",
                    progress=1.0,
                )
                self.platform.record_audit(
                    "backup",
                    "restore_backup_recovery",
                    "warning",
                    "Rolled back interrupted backup restore on startup.",
                    profile_slug=self.active_profile.slug,
                    details={"job_id": job.id},
                )
            elif job.job_type == "profile_backup":
                self.platform.update_job(
                    job.id,
                    status="failed",
                    recoverable=False,
                    checkpoint_stage="failed",
                    detail="Backup creation was interrupted and requires a new run.",
                    error_code=job.error_code or "backup_interrupted",
                    error_message=job.error_message or "Backup creation was interrupted.",
                )

    def _runtime_signature(self) -> tuple[object, ...]:
        snapshot = self.orchestrator.snapshot
        return (
            snapshot.assistant_state,
            snapshot.last_action,
            snapshot.runtime_muted,
            snapshot.local_mode_enabled,
            snapshot.reminders_due[0].id if snapshot.reminders_due else None,
        )

    def refresh_interaction_snapshot(self) -> None:
        snapshot = self.orchestrator.snapshot
        before_signature = self._runtime_signature()
        self._refresh_platform_snapshot_sync()
        snapshot.runtime_muted = self.local_data.muted()
        snapshot.local_mode_enabled = self.brain.local_mode_enabled
        snapshot.cloud_available = self.brain.cloud_available
        snapshot.cognition_backend = self.brain.cognition_backend
        snapshot.reminders_due = self.local_data.list_pending_reminders(limit=5)
        snapshot.startup_checks = {
            "input_mode": "text_only",
            "memory": "ready",
            "cognition": snapshot.cognition_backend,
        }
        if snapshot.runtime_muted and snapshot.assistant_state == "idle":
            snapshot.assistant_state = "muted"
        if before_signature != self._runtime_signature():
            self.orchestrator.mark_dirty("runtime")

    def _refresh_platform_snapshot_sync(self) -> None:
        snapshot = self.orchestrator.snapshot
        locked = not self.profile_session.unlocked
        snapshot.product_name = "KERN"
        snapshot.product_posture = settings.product_posture
        snapshot.active_profile = self.active_profile
        snapshot.profile_session = self.profile_session
        snapshot.recent_audit_events = self.platform.list_audit_events(self.active_profile.slug, limit=8)
        snapshot.background_jobs = self.platform.list_jobs(self.active_profile.slug, limit=6)
        snapshot.backup_targets = self.platform.list_backup_targets(self.active_profile.slug)
        snapshot.audit_enabled = settings.audit_enabled
        snapshot.storage_roots = {
            "profile": self.active_profile.profile_root,
            "documents": self.active_profile.documents_root,
            "attachments": self.active_profile.attachments_root,
            "archives": self.active_profile.archives_root,
            "meetings": self.active_profile.meetings_root,
            "backups": self.active_profile.backups_root,
            "support": str((settings.root_path / "support").resolve()),
        }
        snapshot.onboarding = self._build_onboarding_snapshot()
        snapshot.readiness_summary, snapshot.readiness_checks = self.build_readiness_snapshot()
        snapshot.license_summary = self._license_summary_snapshot()
        snapshot.license_state = snapshot.license_summary.status
        snapshot.update_state = self._update_state_snapshot()
        snapshot.memory_scope = self.local_data.get_preference("memory_scope", "profile") or "profile"
        snapshot.policy_mode = settings.policy_mode
        snapshot.update_channel = settings.update_channel
        snapshot.policy_summary = self.policy.summary()
        snapshot.retention_policies = {
            "documents_days": settings.retention_documents_days,
            "transcripts_days": settings.retention_transcripts_days,
            "audit_days": settings.retention_audit_days,
            "backups_days": settings.retention_backups_days,
            "enforcement_enabled": settings.retention_enforcement_enabled,
            "run_interval_hours": settings.retention_run_interval_hours,
        }
        snapshot.retention_status = self.retention_service.status() if self.retention_service else {}
        snapshot.background_job_counts = self.platform.count_jobs(self.active_profile.slug)
        support_bundle_state = self.local_data.support_bundle_state()
        snapshot.support_bundle_last_export_at = support_bundle_state["last_export_at"]
        snapshot.support_bundle_path = support_bundle_state["path"]
        snapshot.last_update_backup_at = snapshot.update_state.last_backup_at
        snapshot.last_restore_attempt_at = snapshot.update_state.last_restore_attempt_at
        snapshot.audit_chain_ok = self.audit_chain_ok
        snapshot.audit_chain_reason = self.audit_chain_reason
        snapshot.last_audit_verification_at = self.last_audit_verification_at
        snapshot.network_status = self.network_monitor.status
        snapshot.last_monitor_tick_at = self._last_monitor_tick_at
        document_available, document_note = self.document_service.availability()
        business_available, business_note = self.german_business_service.availability()
        sync_available, sync_note = self.sync_service.availability()
        snapshot.domain_statuses = {
            "documents": DomainStatus(
                ready=document_available,
                reason=document_note or ("ready" if document_available else "unavailable"),
                lock_sensitive=True,
                degraded=document_available and bool(document_note and "unavailable" in document_note.lower()),
            ),
            "german_business": DomainStatus(
                ready=business_available,
                reason=business_note or ("ready" if business_available else "unavailable"),
                lock_sensitive=True,
                degraded=business_available and bool(business_note and "draft/support" in business_note.lower()),
            ),
            "sync": DomainStatus(
                ready=sync_available,
                reason=sync_note or ("ready" if sync_available else "unavailable"),
                lock_sensitive=True,
                degraded=sync_available and bool(sync_note and ("upload-only" in sync_note.lower() or "degraded" in sync_note.lower())),
            ),
        }
        snapshot.domain_notes = {
            domain: status.reason for domain, status in snapshot.domain_statuses.items()
        }
        if self.scheduler_service:
            try:
                snapshot.scheduled_tasks = self.scheduler_service.list_tasks()
            except Exception as exc:
                logger.warning("Failed to list scheduled tasks: %s", exc)
                snapshot.scheduled_tasks = []
        else:
            snapshot.scheduled_tasks = []
        snapshot.background_components = {
            "profile_db": "locked_scaffold" if self._using_locked_scaffold else "profile_bound",
            "scheduler": "ready" if self.scheduler_service else "disabled",
            "file_watcher": "running" if self._file_watcher else "disabled",
            "calendar_watcher": "running" if self._calendar_watcher else "disabled",
            "document_watcher": "running" if self._document_watcher else "disabled",
            "network_monitor": "disabled" if not settings.network_monitor_enabled else snapshot.network_status.status,
            "interaction_mode": "text_first",
        }
        degraded_reasons: list[str] = []
        if not snapshot.audit_chain_ok:
            degraded_reasons.append(snapshot.audit_chain_reason or "Audit chain verification failed.")
        if snapshot.network_status.status == "network_detected":
            degraded_reasons.append("Unexpected outbound network activity detected.")
        if snapshot.network_status.status == "unmonitored":
            degraded_reasons.append("Outbound network monitoring is disabled.")
        if any(task.get("run_status") in {"failed", "retry_pending"} for task in snapshot.scheduled_tasks):
            degraded_reasons.append("One or more scheduled tasks need attention.")
        if self._background_errors:
            degraded_reasons.append(f"Background errors recorded: {len(self._background_errors)}")
        snapshot.runtime_degraded_reasons = degraded_reasons
        snapshot.active_failures, snapshot.last_recoverable_failure = self.build_failure_snapshot(
            snapshot.readiness_checks,
            snapshot.background_jobs,
        )
        snapshot.trust_summary = self._build_trust_summary()
        snapshot.model_fallback_state = ModelFallbackState(enabled=settings.model_mode != "off", active_mode=settings.model_mode)
        self.security_state = self.platform.get_profile_security_state(self.active_profile.slug)
        snapshot.security_status = SecurityStatusSnapshot(
            db_encryption_enabled=bool(self.security_state.get("db_encryption_enabled")),
            db_encryption_mode=str(self.security_state.get("db_encryption_mode") or "off"),
            db_key_available=bool(self.security_state.get("db_key_available")),
            key_derivation_version=settings.key_derivation_version,
            key_version=int(self.security_state.get("key_version") or 0),
            last_key_rotation=datetime.fromisoformat(self.security_state["last_key_rotation"]) if self.security_state.get("last_key_rotation") else None,
            artifact_encryption_enabled=bool(self.security_state.get("artifact_encryption_enabled")),
            artifact_encryption_migration_state=str(self.security_state.get("artifact_encryption_migration_state") or "not enabled"),
            profile_key_loaded=bool(self.security_state.get("profile_key_loaded")),
            artifact_key_loaded=bool(self.security_state.get("artifact_key_loaded")),
            artifact_encryption_status=(
                "encrypted"
                if self.security_state.get("artifact_encryption_enabled") and self.security_state.get("artifact_encryption_migration_state") == "completed"
                else str(self.security_state.get("artifact_encryption_migration_state") or "not enabled")
            ),
        )
        snapshot.retrieval_status = self.retrieval_service.status
        snapshot.last_retrieval_query = self.retrieval_service.last_query
        snapshot.recent_retrieval_hits = self.retrieval_service.last_hits[:6]
        if locked:
            snapshot.domain_totals = {
                "documents": 0,
                "business_documents": 0,
                "sync_targets": 0,
            }
            snapshot.last_retrieval_query = ""
            snapshot.recent_retrieval_hits = []
            snapshot.recent_documents = []
            snapshot.business_documents = []
            snapshot.sync_targets = []
            snapshot.available_backups = []
            snapshot.recovery_checkpoints = []
            return
        snapshot.domain_totals = {
            "documents": self.memory.count_document_records(),
            "business_documents": self.memory.count_business_documents(),
            "sync_targets": self.memory.count_sync_targets(),
        }
        snapshot.recent_documents = self.document_service.list_documents(limit=6, audit=False) if document_available else []
        if settings.policy_mode == "corporate" and settings.policy_restrict_sensitive_reads:
            snapshot.recent_documents = [
                record
                for record in snapshot.recent_documents
                if not self.policy.is_sensitive_classification(record.classification)
            ]
        snapshot.recent_meetings = []
        snapshot.recent_transcripts = []
        snapshot.recent_meeting_reviews = []
        snapshot.business_documents = self.german_business_service.list_documents(limit=6, audit=False) if business_available else []
        snapshot.sync_targets = self.sync_service.list_targets(audit=False)
        checkpoints = []
        for job in snapshot.background_jobs[:3]:
            checkpoints.extend(self.platform.list_checkpoints(job.id)[:2])
        snapshot.recovery_checkpoints = checkpoints[:6]
        available_backups: list[str] = []
        for target in snapshot.backup_targets:
            available_backups.extend(self.backup_service.list_backups(self.active_profile, target)[:3])
        snapshot.available_backups = available_backups[:6]

    async def _refresh_platform_snapshot(self) -> None:
        await asyncio.to_thread(self._refresh_platform_snapshot_sync)

    async def refresh_llm_status(self, *, retries: int = 1) -> bool:
        if not self._llm_client:
            if not settings.llm_enabled:
                self.orchestrator.snapshot.llm_available = False
                return False
            self._llm_client = LlamaServerClient(
                base_url=settings.llama_server_url,
                timeout=settings.llama_server_timeout,
                default_model=settings.llm_model,
            )
            self.brain.llm_client = self._llm_client
            await self._llm_client.startup()
        attempts = max(1, retries)
        for attempt in range(attempts):
            if await self._llm_client.health():
                self.orchestrator.snapshot.llm_available = True
                self.orchestrator.snapshot.model_info.llm_model = settings.llm_model or "llama-server"
                return True
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5)
        self.orchestrator.snapshot.llm_available = False
        return False

    async def monitor_runtime(self) -> None:
        while True:
            self._last_monitor_tick_at = datetime.now(timezone.utc)
            self._monitor_cycles += 1
            self.orchestrator.snapshot.loop_metrics.monitor_cycles = self._monitor_cycles
            if self._llm_client and not self.orchestrator.snapshot.llm_available:
                await self.refresh_llm_status()
            self.refresh_interaction_snapshot()
            await asyncio.to_thread(self.network_monitor.check)
            self.orchestrator.snapshot.network_status = self.network_monitor.status
            await self._tick_scheduler()
            await self._poll_watchers()
            await self._maybe_notify_due_items()
            await self._run_retention(force=False, reason="monitor")
            await self.broadcast_if_changed(reason="monitor")
            await asyncio.sleep(settings.monitor_interval_seconds)

    async def _tick_scheduler(self) -> None:
        if not self.scheduler_service or not self.profile_session.unlocked:
            return
        try:
            due = await asyncio.to_thread(self.scheduler_service.tick)
            for task in due:
                try:
                    result = await self._execute_scheduled_task(task)
                    await asyncio.to_thread(
                        self.scheduler_service.record_success,
                        str(task.get("id")),
                        result,
                    )
                    self.platform.record_audit(
                        "scheduler", "task_executed", "success",
                        f"Executed scheduled task: {task.get('title', task.get('id', ''))}",
                        profile_slug=self.active_profile.slug,
                        details={"task_id": task.get("id"), "action_type": task.get("action_type", "custom_prompt"), "result": result},
                    )
                except Exception as exc:
                    failure = await asyncio.to_thread(
                        self.scheduler_service.record_failure,
                        str(task.get("id")),
                        str(exc),
                    )
                    self._background_errors.append(f"scheduler:{task.get('id')}:{exc}")
                    self.platform.record_audit(
                        "scheduler",
                        "task_executed",
                        "failure",
                        str(exc),
                        profile_slug=self.active_profile.slug,
                        details={"task_id": task.get("id"), "action_type": task.get("action_type", "custom_prompt"), "failure": failure},
                    )
                    self.orchestrator.snapshot.last_action = f"Scheduled task failed: {task.get('title', task.get('id', ''))}"
            self.orchestrator.snapshot.scheduled_tasks = await asyncio.to_thread(self.scheduler_service.list_tasks)
            self.orchestrator.mark_dirty("runtime")
        except Exception as exc:
            logger.warning("Scheduler tick error: %s", exc)
            self._background_errors.append(f"scheduler_tick:{exc}")

    async def _execute_scheduled_task(self, task: dict) -> dict:
        if not self.ensure_production_access(blocked_scope="scheduled task execution"):
            raise RuntimeError("Scheduled task execution is blocked by license state.")
        action_type = str(task.get("action_type", "custom_prompt") or "custom_prompt")
        payload = task.get("action_payload", {}) or {}
        if action_type == "custom_prompt":
            prompt = str(payload.get("prompt", "") or task.get("title", "Scheduled task"))
            if prompt and not self.orchestrator.snapshot.action_in_progress:
                turn = await self.orchestrator.process_transcript(
                    prompt,
                    trigger="scheduler",
                    allow_llm_fallback=False,
                )
                if turn.plan is None and turn.recommendation_id is None:
                    raise RuntimeError("Scheduled prompt did not resolve to a deterministic local recommendation.")
                return {
                    "mode": "prompt",
                    "prompt": prompt,
                    "reasoning_source": turn.reasoning_source,
                    "recommendation_id": turn.recommendation_id,
                }
            raise RuntimeError("Scheduled prompt could not run while another action was in progress.")
        if action_type == "generate_report":
            tool = self.orchestrator.tools.get("generate_morning_brief")
            if tool is None:
                raise RuntimeError("Report tool is unavailable.")
            request = ToolRequest(
                tool_name="generate_morning_brief",
                arguments={},
                user_utterance="",
                reason=f"Scheduled task: {task.get('title', 'generate report')}",
                trigger_source="scheduler",
            )
            result = await tool.run(request)
            if not result.success:
                raise RuntimeError(result.display_text or "Scheduled report generation failed.")
            return {"mode": "tool", "tool_name": "generate_morning_brief", "status": result.status}
        raise RuntimeError(f"Unsupported scheduled action type: {action_type}")

    async def _poll_watchers(self) -> None:
        if not self.profile_session.unlocked:
            return
        alerts: list[dict] = []
        try:
            if self._file_watcher:
                alerts.extend(await asyncio.to_thread(self._file_watcher.poll))
                alerts.extend(self._file_watcher.drain_alerts())
            if self._calendar_watcher:
                alerts.extend(await asyncio.to_thread(self._calendar_watcher.check))
            if self._document_watcher:
                alerts.extend(await asyncio.to_thread(self._document_watcher.check))
        except Exception as exc:
            logger.debug("Watcher poll error: %s", exc)
            self._background_errors.append(f"watchers:{exc}")
        if alerts:
            self._pending_proactive_alerts = self.action_planner.rank_alerts(
                self._pending_proactive_alerts + alerts,
                self.local_data,
            )
            self.orchestrator.snapshot.proactive_alerts = list(self._pending_proactive_alerts[:20])
            self.orchestrator.mark_dirty("runtime")

    async def broadcast_if_changed(self, force: bool = False, reason: str = "runtime") -> None:
        now = asyncio.get_running_loop().time()
        heartbeat_due = now - self._last_heartbeat >= settings.heartbeat_seconds
        debounce_due = now - self._last_broadcast_at >= (settings.snapshot_dirty_debounce_ms / 1000)
        if not force and not heartbeat_due and not self.orchestrator.has_pending_snapshot_work():
            self._snapshots_skipped += 1
            self.orchestrator.snapshot.loop_metrics.snapshots_skipped = self._snapshots_skipped
            return
        if not force and not heartbeat_due and not debounce_due:
            self._snapshots_skipped += 1
            self.orchestrator.snapshot.loop_metrics.snapshots_skipped = self._snapshots_skipped
            return

        payload = self.orchestrator.snapshot.model_dump_json()
        if force or heartbeat_due or payload != self._last_snapshot_payload:
            started = asyncio.get_running_loop().time()
            if heartbeat_due:
                self._last_heartbeat = now
            self._snapshots_sent += 1
            self.orchestrator.snapshot.loop_metrics.snapshots_sent = self._snapshots_sent
            await self.orchestrator.broadcast(reason=reason, force=force)
            self._last_broadcast_at = now
            self._last_snapshot_payload = self.orchestrator.snapshot.model_dump_json()
            self.orchestrator.snapshot.loop_metrics.last_broadcast_ms = round(
                (asyncio.get_running_loop().time() - started) * 1000,
                2,
            )
        else:
            self._snapshots_skipped += 1
            self.orchestrator.snapshot.loop_metrics.snapshots_skipped = self._snapshots_skipped

    async def _maybe_notify_due_items(self) -> None:
        if not self.profile_session.unlocked:
            return
        if not self.local_data.quiet_hours_active():
            due_reminders = await asyncio.to_thread(self.reminder_service.due_reminders)
            if due_reminders:
                self.orchestrator.snapshot.reminders_due = await asyncio.to_thread(self.local_data.list_pending_reminders, limit=5)
                self.orchestrator.dialogue_state.set_last_listed_reminder_ids(
                    [int(reminder.id) for reminder in self.orchestrator.snapshot.reminders_due if reminder.id is not None]
                )
                for message, reminder in zip(
                    self.notification_service.due_reminder_messages(due_reminders, self.local_data.preferred_title()),
                    due_reminders,
                ):
                    self.orchestrator.add_history("reminder", message)
                    self.orchestrator.snapshot.response_text = message
                    if reminder.id is not None:
                        self.orchestrator.dialogue_state.set_last_announced_reminder_id(reminder.id)
                        await asyncio.to_thread(self.reminder_service.mark_announced, reminder.id)
                self.orchestrator.snapshot.reminders_due = await asyncio.to_thread(self.local_data.list_pending_reminders, limit=5)

            next_event = await asyncio.to_thread(self.local_data.next_upcoming_event)
            if next_event:
                now = datetime.now()
                delta = next_event.starts_at - now
                if 0 <= delta.total_seconds() <= 600 and not self.local_data.event_alerted(next_event):
                    message = self.notification_service.event_soft_alert_message(
                        next_event.title,
                        self.local_data.preferred_title(),
                        next_event.starts_at,
                    )
                    if message:
                        self.local_data.mark_event_alerted(next_event)
                        self.orchestrator.add_history("reminder", message)
                        self.orchestrator.snapshot.response_text = message
        if self.proactive_enabled and not self.orchestrator.snapshot.action_in_progress:
            await self.orchestrator.maybe_emit_proactive_prompt()

    async def _run_retention(self, *, force: bool, reason: str) -> None:
        if not self.profile_session.unlocked or not self.retention_service:
            return
        now = asyncio.get_running_loop().time()
        if not force and now - self._last_retention_check_at < 600:
            return
        self._last_retention_check_at = now
        try:
            result = await asyncio.to_thread(self.retention_service.run_if_due, force=force, reason=reason)
            if result is not None:
                self.orchestrator.mark_dirty("runtime")
        except Exception as exc:
            logger.warning("Retention run error: %s", exc)
            self._background_errors.append(f"retention:{exc}")
