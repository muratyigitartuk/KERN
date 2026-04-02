from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time as _time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.background import BackgroundTask

from app import __version__

from app.auth import request_auth_context, require_request_auth_context
from app.compliance import ComplianceService
from app.config import settings
from app.database import connect, get_schema_version
from app.intelligence import IntelligenceService
from app.memory import MemoryRepository
from app.metrics import metrics
from app.reasoning import ReasoningService
from app.types import CapabilityDescriptor, PlanStep, SuggestedDraftRecord, WorkflowDomainEvent

logger = logging.getLogger(__name__)

UPLOAD_MAX_FILE_MB = int(os.environ.get("KERN_UPLOAD_MAX_FILE_MB", "50"))
UPLOAD_MAX_BATCH_MB = int(os.environ.get("KERN_UPLOAD_MAX_BATCH_MB", "200"))
UPLOAD_ALLOWED_EXTENSIONS = frozenset(
    ext.strip().lower()
    for ext in os.environ.get(
        "KERN_UPLOAD_ALLOWED_EXTENSIONS",
        ".txt,.md,.pdf,.csv,.xlsx,.xls,.doc,.docx,.eml,.json,.html,.htm,.rtf",
    ).split(",")
    if ext.strip()
)

UPLOAD_DOCUMENTS_DESCRIPTOR = CapabilityDescriptor(
    name="upload_documents",
    title="Upload Documents",
    summary="Upload a small set of local documents into the active profile archive.",
    domain="documents",
    risk_level="low",
    confirmation_rule="never",
    side_effectful=True,
    verification_support="database",
)


def _validate_upload_filename(name: str) -> str | None:
    """Return an error message if the filename is unsafe, else None."""
    if not name:
        return "Empty filename"
    if "\x00" in name:
        return "Filename contains null bytes"
    if "/" in name or "\\" in name:
        return "Filename contains path separators"
    parts = name.rsplit(".", maxsplit=2)
    if len(parts) >= 3 and parts[-2].lower() in {"exe", "bat", "cmd", "ps1", "sh", "vbs", "js", "msi"}:
        return f"Double extension not allowed: {name}"
    suffix = Path(name).suffix.lower()
    if suffix and suffix not in UPLOAD_ALLOWED_EXTENSIONS:
        return f"File type '{suffix}' not allowed. Accepted: {', '.join(sorted(UPLOAD_ALLOWED_EXTENSIONS))}"
    if not suffix:
        return "Files must have an extension"
    return None

if TYPE_CHECKING:
    from app.runtime import KernRuntime

_startup_time = _time.time()
_retrieval_refresh_task: asyncio.Task | None = None
_retrieval_refresh_scopes: set[str] = set()

static_dir = Path(__file__).parent / "static"

_get_runtime: callable = None  # type: ignore[assignment]


def _get_memory_mb() -> float | None:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1024 / 1024, 1)
    except Exception as exc:
        logger.debug("Failed to read memory usage: %s", exc)
        return None


async def _resolve_runtime(request: Request | None = None):
    runtime_or_manager = _get_runtime()
    if hasattr(runtime_or_manager, "get_runtime"):
        workspace_slug = None
        if request is not None:
            context = getattr(request.state, "auth_context", None)
            workspace_slug = getattr(context, "workspace_slug", None)
        return await runtime_or_manager.get_runtime(workspace_slug)
    return runtime_or_manager


async def _session_payload(request: Request, runtime, context=None) -> dict[str, object]:
    resolved_context = context or require_request_auth_context(request)
    payload = resolved_context.model_dump(mode="json")
    identity = _identity_service(request, runtime)
    workspaces = await asyncio.to_thread(identity.list_accessible_workspaces, resolved_context)
    payload["workspaces"] = [workspace.model_dump(mode="json") for workspace in workspaces]
    if resolved_context.user_id:
        user = await asyncio.to_thread(runtime.platform.get_user, resolved_context.user_id)
        payload["user"] = user.model_dump(mode="json") if user is not None else None
    else:
        payload["user"] = None
    sessions = await asyncio.to_thread(
        runtime.platform.list_sessions,
        resolved_context.organization_id,
        resolved_context.user_id if resolved_context.user_id else None,
    )
    payload["sessions"] = [session.model_dump(mode="json") for session in sessions]
    return payload


def _require_roles(request: Request | None, *roles: str):
    if request is None:
        return None
    if not hasattr(getattr(request.app, "state", object()), "identity_service"):
        return None
    context = require_request_auth_context(request)
    if context.is_bootstrap_token:
        return context
    if not roles:
        return context
    if any(role in context.roles for role in roles):
        return context
    raise HTTPException(status_code=403, detail="Insufficient role for this action.")


def _identity_service(request: Request, runtime) -> object:
    return getattr(request.app.state, "identity_service", getattr(runtime, "identity_service", None))


def _ensure_memory(runtime):
    memory = getattr(runtime, "memory", None)
    if memory is not None:
        return memory
    profile = getattr(runtime, "active_profile", None)
    if profile is None:
        raise RuntimeError("Active workspace is unavailable.")
    connection = connect(Path(profile.db_path))
    memory = MemoryRepository(connection, profile_slug=profile.slug)
    setattr(runtime, "_phase45_memory_connection", connection)
    setattr(runtime, "memory", memory)
    return memory


def _compliance_service(runtime):
    service = getattr(runtime, "compliance_service", None)
    if service is not None:
        return service
    memory = _ensure_memory(runtime)
    profile = getattr(runtime, "active_profile", None)
    if profile is None:
        raise RuntimeError("Active workspace is unavailable.")
    service = ComplianceService(runtime.platform, memory, profile)
    setattr(runtime, "compliance_service", service)
    return service


def _intelligence_service(runtime):
    service = getattr(runtime, "intelligence_service", None)
    if service is not None:
        return service
    memory = _ensure_memory(runtime)
    profile = getattr(runtime, "active_profile", None)
    if profile is None:
        raise RuntimeError("Active workspace is unavailable.")
    service = IntelligenceService(runtime.platform, memory, profile)
    setattr(runtime, "intelligence_service", service)
    return service


def _reasoning_service(runtime):
    service = getattr(runtime, "reasoning_service", None)
    if service is not None:
        return service
    memory = _ensure_memory(runtime)
    profile = getattr(runtime, "active_profile", None)
    if profile is None:
        raise RuntimeError("Active workspace is unavailable.")
    service = ReasoningService(
        runtime.platform,
        memory,
        profile,
        scheduler_service=getattr(runtime, "scheduler_service", None),
    )
    setattr(runtime, "reasoning_service", service)
    return service


def _stable_reasoning_id(*parts: object) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _workflow_id_for_scope(workspace_slug: str, workflow_key: str) -> str:
    return _stable_reasoning_id("workflow", workspace_slug, workflow_key)


def _append_domain_event(
    runtime,
    *,
    organization_id: str | None,
    workspace_slug: str | None,
    actor_user_id: str | None,
    workflow_key: str | None = None,
    workflow_id: str | None = None,
    workflow_type: str,
    event_type: str,
    detail: str,
    metadata: dict[str, object] | None = None,
) -> None:
    workspace_value = workspace_slug or runtime.active_profile.slug
    resolved_workflow_id = workflow_id or _workflow_id_for_scope(workspace_value, workflow_key or workflow_type)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "workflow_key": workflow_key,
                "workflow_id": resolved_workflow_id,
                "workflow_type": workflow_type,
                "event_type": event_type,
                "detail": detail,
                "metadata": metadata or {},
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    _ensure_memory(runtime).record_workflow_domain_event(
        WorkflowDomainEvent(
            id=f"wde-{resolved_workflow_id}-{fingerprint[:16]}",
            profile_slug=runtime.active_profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_value,
            actor_user_id=actor_user_id,
            workflow_id=resolved_workflow_id,
            workflow_type=workflow_type,
            event_type=event_type,
            detail=detail,
            fingerprint=fingerprint,
            metadata=metadata or {},
        )
    )


def _collection_response(items_name: str, items: list[object], **extra) -> JSONResponse:
    payload = {
        "status": "ok",
        "errors": [],
        "items": items,
        items_name: items,
        **extra,
    }
    return JSONResponse(payload)


def _detail_response(item_name: str, item: object, **extra) -> JSONResponse:
    payload = {
        "status": "ok",
        "errors": [],
        "item": item,
        item_name: item,
        **extra,
    }
    return JSONResponse(payload)


def _action_response(*, item_name: str | None = None, item: object | None = None, job: object | None = None, artifact: object | None = None, manifest: object | None = None, **extra) -> JSONResponse:
    payload: dict[str, object] = {"status": "ok", "errors": []}
    if item_name and item is not None:
        payload[item_name] = item
        payload["item"] = item
    if job is not None:
        payload["job"] = job
    if artifact is not None:
        payload["artifact"] = artifact
    if manifest is not None:
        payload["manifest"] = manifest
    payload.update(extra)
    return JSONResponse(payload)


def _read_training_manifest(runtime, export_id: str) -> dict[str, object] | None:
    root = Path(runtime.active_profile.profile_root) / "training-exports" / export_id / "manifest.json"
    if not root.exists():
        return None
    return json.loads(root.read_text(encoding="utf-8"))


def _http_policy_gate(
    runtime,
    capability_name: str,
    *,
    arguments: dict[str, object] | None = None,
    descriptor: CapabilityDescriptor | None = None,
) -> None:
    policy = getattr(runtime, "policy", None)
    if policy is None:
        return
    step = PlanStep(
        capability_name=capability_name,
        arguments=arguments or {},
        reason=f"HTTP route: {capability_name}",
    )
    resolved_descriptor = descriptor or runtime.orchestrator.capabilities.get_descriptor(capability_name)
    decision = policy.decide_step(step, descriptor=resolved_descriptor)
    if decision.verdict == "allow":
        return
    runtime.platform.record_audit(
        "policy",
        f"http_{capability_name}",
        "failure" if decision.verdict == "deny" else "warning",
        decision.message,
        profile_slug=runtime.active_profile.slug,
        details={"verdict": decision.verdict, "reason": decision.policy_reason},
    )
    raise HTTPException(status_code=403 if decision.verdict == "deny" else 409, detail=decision.message)


def _health_severity(runtime, snapshot) -> str:
    network_status = getattr(getattr(snapshot, "network_status", None), "status", "unknown")
    if not getattr(runtime, "active_profile", None):
        return "error"
    if not getattr(runtime, "audit_chain_ok", True) or getattr(snapshot, "runtime_degraded_reasons", []):
        return "degraded"
    if getattr(runtime, "_using_locked_scaffold", False) or network_status == "unmonitored":
        return "warning"
    return "ok"


def _health_status_code(severity: str) -> int:
    if severity == "error":
        return 500
    if severity == "degraded":
        return 503
    return 200


def _health_components(runtime) -> dict[str, str]:
    """Return per-component health status strings."""
    components: dict[str, str] = {}
    # Database
    try:
        runtime.memory.connection.execute("SELECT 1").fetchone()
        components["database"] = "ok"
    except Exception as exc:
        components["database"] = f"error — {exc}"
    # LLM
    if getattr(runtime.orchestrator.snapshot, "llm_available", False):
        components["llm"] = "ok"
    elif settings.llm_enabled:
        components["llm"] = "error — not reachable"
    else:
        components["llm"] = "disabled"
    # Scheduler
    if getattr(runtime, "scheduler_service", None):
        components["scheduler"] = "ok"
    else:
        components["scheduler"] = "disabled"
    # Email
    if settings.imap_host or settings.smtp_host:
        components["email"] = "ok"
    else:
        components["email"] = "warning — not configured"
    # TTS
    if getattr(runtime, "tts", None) and getattr(runtime.tts, "enabled", False):
        components["tts"] = "ok"
    else:
        components["tts"] = "disabled"
    return components


async def _run_retrieval_refresh(runtime: "KernRuntime") -> None:
    global _retrieval_refresh_task
    try:
        await asyncio.sleep(0.5)
        while _retrieval_refresh_scopes:
            scopes = tuple(_retrieval_refresh_scopes)
            _retrieval_refresh_scopes.clear()
            retrieval_service = getattr(runtime, "retrieval_service", None)
            if retrieval_service is None:
                return
            for scope in scopes:
                if settings.rag_enabled:
                    await asyncio.to_thread(retrieval_service.rebuild_index, scope)
                if settings.vec_enabled and hasattr(retrieval_service, "rebuild_vec_index"):
                    await asyncio.to_thread(retrieval_service.rebuild_vec_index, scope)
    except Exception as exc:
        logger.warning("Deferred retrieval refresh failed: %s", exc, exc_info=True)
    finally:
        _retrieval_refresh_task = None


def _schedule_retrieval_refresh(runtime: "KernRuntime", scope: str) -> None:
    global _retrieval_refresh_task
    _retrieval_refresh_scopes.add(scope)
    if _retrieval_refresh_task is None or _retrieval_refresh_task.done():
        _retrieval_refresh_task = asyncio.create_task(_run_retrieval_refresh(runtime))


async def _collect_logs(runtime: "KernRuntime") -> dict[str, object]:
    audit_chain_ok, audit_chain_reason = await asyncio.to_thread(runtime.verify_audit_chain, "export")
    logs = await asyncio.to_thread(runtime.local_data.list_runtime_logs, 500)
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "logs": logs,
        "audit_chain_ok": audit_chain_ok,
        "audit_chain_reason": audit_chain_reason,
    }


async def _build_governance_bundle(runtime: "KernRuntime") -> dict[str, object]:
    audit_chain_ok, audit_chain_reason = await asyncio.to_thread(runtime.verify_audit_chain, "governance_export")
    snapshot = runtime.orchestrator.snapshot
    targets = await asyncio.to_thread(runtime.platform.list_backup_targets, runtime.active_profile.slug)
    backups: list[dict[str, object]] = []
    for target in targets:
        for backup_path in await asyncio.to_thread(runtime.backup_service.list_backups, runtime.active_profile, target):
            info = await asyncio.to_thread(runtime.backup_service.inspect_backup, backup_path)
            info["target_label"] = target.label
            backups.append(info)
    document_classifications = await asyncio.to_thread(runtime.memory.summarize_document_classifications)
    events = await asyncio.to_thread(runtime.platform.list_audit_events, runtime.active_profile.slug, 50)
    audit_retention_anchors = []
    if hasattr(runtime.platform, "list_audit_retention_anchors"):
        audit_retention_anchors = await asyncio.to_thread(runtime.platform.list_audit_retention_anchors, runtime.active_profile.slug, 20)
    bundle = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "profile_slug": runtime.active_profile.slug,
        "app_version": getattr(snapshot.model_info, "app_version", "0.0.0"),
        "update_channel": settings.update_channel,
        "profile_schema_version": get_schema_version(runtime.memory.connection) if getattr(runtime, "memory", None) else None,
        "system_schema_version": get_schema_version(runtime.platform.connection),
        "policy": snapshot.policy_summary,
        "product_posture": getattr(snapshot, "product_posture", "production"),
        "retention_policies": snapshot.retention_policies,
        "retention_status": getattr(snapshot, "retention_status", {}),
        "audit_retention_anchors": audit_retention_anchors,
        "health": {
            "status": _health_severity(runtime, snapshot),
            "audit_chain_ok": audit_chain_ok,
            "audit_chain_reason": audit_chain_reason,
            "runtime_degraded_reasons": snapshot.runtime_degraded_reasons,
            "background_components": snapshot.background_components,
            "network_status": snapshot.network_status.model_dump(mode="json"),
            "last_monitor_tick_at": snapshot.last_monitor_tick_at.isoformat() if snapshot.last_monitor_tick_at else None,
        },
        "security": snapshot.security_status.model_dump(mode="json"),
        "backup_inventory": backups,
        "scheduled_tasks": snapshot.scheduled_tasks,
        "document_classifications": document_classifications,
        "audit_events": [event.model_dump(mode="json") for event in events],
    }
    return bundle


async def export_logs(request: Request | None = None) -> dict[str, object]:
    _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
    runtime: KernRuntime = await _resolve_runtime(request)
    if hasattr(runtime, "ensure_production_access") and not runtime.ensure_production_access(blocked_scope="runtime log export"):
        raise HTTPException(status_code=403, detail="Production access is blocked by license state.")
    if await asyncio.to_thread(runtime.platform.is_profile_locked, runtime.active_profile.slug):
        await asyncio.to_thread(
            runtime.platform.record_audit,
            "runtime",
            "export_logs",
            "failure",
            "Denied runtime log export while profile is locked.",
            profile_slug=runtime.active_profile.slug,
        )
        raise HTTPException(status_code=423, detail="Unlock the active KERN profile before exporting logs.")
    _http_policy_gate(
        runtime,
        "export_logs",
        descriptor=CapabilityDescriptor(
            name="export_logs",
            title="Export runtime logs",
            summary="Exports runtime logs from the active profile.",
            domain="security",
            risk_level="high",
            confirmation_rule="always",
        ),
    )
    payload = await _collect_logs(runtime)
    await asyncio.to_thread(
        runtime.platform.record_audit,
        "runtime",
        "export_logs",
        "success",
        "Exported runtime logs.",
        profile_slug=runtime.active_profile.slug,
        details={"audit_chain_ok": payload["audit_chain_ok"], "audit_chain_reason": payload["audit_chain_reason"]},
    )
    return payload


async def export_governance_bundle(request: Request | None = None) -> dict[str, object]:
    _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
    runtime: KernRuntime = await _resolve_runtime(request)
    if hasattr(runtime, "ensure_production_access") and not runtime.ensure_production_access(blocked_scope="governance export"):
        raise HTTPException(status_code=403, detail="Production access is blocked by license state.")
    if await asyncio.to_thread(runtime.platform.is_profile_locked, runtime.active_profile.slug):
        await asyncio.to_thread(
            runtime.platform.record_audit,
            "governance",
            "export_bundle",
            "failure",
            "Denied governance export while profile is locked.",
            profile_slug=runtime.active_profile.slug,
        )
        raise HTTPException(status_code=423, detail="Unlock the active KERN profile before exporting governance data.")
    _http_policy_gate(
        runtime,
        "export_governance_bundle",
        descriptor=CapabilityDescriptor(
            name="export_governance_bundle",
            title="Export governance bundle",
            summary="Exports audit, security, and governance state for the active profile.",
            domain="security",
            risk_level="high",
            confirmation_rule="always",
        ),
    )
    bundle = await _build_governance_bundle(runtime)
    await asyncio.to_thread(
        runtime.platform.record_audit,
        "governance",
        "export_bundle",
        "success",
        "Exported governance bundle.",
        profile_slug=runtime.active_profile.slug,
        details={
            "audit_chain_ok": bundle["health"]["audit_chain_ok"],
            "document_classifications": bundle["document_classifications"],
            "backup_count": len(bundle["backup_inventory"]),
        },
    )
    return bundle


def _config_summary() -> dict[str, object]:
    return {
        "product_posture": settings.product_posture,
        "policy_mode": settings.policy_mode,
        "update_channel": settings.update_channel,
        "llm_enabled": settings.llm_enabled,
        "llm_local_only": settings.llm_local_only,
        "db_encryption_mode": settings.db_encryption_mode,
        "artifact_encryption_enabled": settings.artifact_encryption_enabled,
        "audit_enabled": settings.audit_enabled,
        "retention_enforcement_enabled": settings.retention_enforcement_enabled,
        "storage_roots_configured": True,
        "license_configured": bool(settings.license_public_key or settings.license_public_key_path),
    }


async def export_support_bundle(request: Request | None = None) -> FileResponse:
    _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
    runtime: KernRuntime = await _resolve_runtime(request)
    if hasattr(runtime, "ensure_production_access") and not runtime.ensure_production_access(blocked_scope="support export"):
        raise HTTPException(status_code=403, detail="Production access is blocked by license state.")
    if await asyncio.to_thread(runtime.platform.is_profile_locked, runtime.active_profile.slug):
        raise HTTPException(status_code=423, detail="Unlock the active KERN profile before exporting a support bundle.")
    _http_policy_gate(
        runtime,
        "export_support_bundle",
        descriptor=CapabilityDescriptor(
            name="export_support_bundle",
            title="Export support bundle",
            summary="Exports a support bundle with health, readiness, logs, and config summaries.",
            domain="security",
            risk_level="high",
            confirmation_rule="always",
        ),
    )
    support_root = (settings.root_path / "support").resolve()
    support_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bundle_path = support_root / f"kern-support-{timestamp}.zip"
    try:
        await runtime._refresh_platform_snapshot()
        governance = await _build_governance_bundle(runtime)
        logs = await _collect_logs(runtime)
        health_payload = {
            "status": _health_severity(runtime, runtime.orchestrator.snapshot),
            "components": _health_components(runtime),
            "app_version": getattr(runtime.orchestrator.snapshot.model_info, "app_version", "0.0.0"),
            "update_channel": settings.update_channel,
            "runtime_degraded_reasons": runtime.orchestrator.snapshot.runtime_degraded_reasons,
        }
        readiness_payload = {
            "summary": runtime.orchestrator.snapshot.readiness_summary.model_dump(mode="json"),
            "checks": [check.model_dump(mode="json") for check in runtime.orchestrator.snapshot.readiness_checks],
        }
        license_payload = runtime.orchestrator.snapshot.license_summary.model_dump(mode="json")
        update_payload = runtime.orchestrator.snapshot.update_state.model_dump(mode="json")
        failures_payload = {
            "active_failures": [failure.model_dump(mode="json") for failure in runtime.orchestrator.snapshot.active_failures],
            "last_recoverable_failure": runtime.orchestrator.snapshot.last_recoverable_failure.model_dump(mode="json")
            if runtime.orchestrator.snapshot.last_recoverable_failure
            else None,
            "background_jobs": [job.model_dump(mode="json") for job in runtime.orchestrator.snapshot.background_jobs],
        }
        manifest = {
            "bundle_type": "kern_support_bundle",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "app_version": getattr(runtime.orchestrator.snapshot.model_info, "app_version", "0.0.0"),
            "profile_slug": runtime.active_profile.slug,
            "product_posture": settings.product_posture,
            "update_channel": settings.update_channel,
            "excludes_raw_documents": True,
            "excludes_generated_business_content": True,
        }
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            archive.writestr("health.json", json.dumps(health_payload, indent=2, sort_keys=True))
            archive.writestr("readiness.json", json.dumps(readiness_payload, indent=2, sort_keys=True))
            archive.writestr("license.json", json.dumps(license_payload, indent=2, sort_keys=True))
            archive.writestr("update-state.json", json.dumps(update_payload, indent=2, sort_keys=True))
            archive.writestr("config-summary.json", json.dumps(_config_summary(), indent=2, sort_keys=True))
            archive.writestr("failures.json", json.dumps(failures_payload, indent=2, sort_keys=True))
            archive.writestr("governance.json", json.dumps(governance, indent=2, sort_keys=True))
            archive.writestr("logs/runtime-logs.json", json.dumps(logs, indent=2, sort_keys=True))
        runtime.local_data.update_support_bundle_state(
            last_export_at=datetime.now(timezone.utc).isoformat(),
            path=str(bundle_path),
        )
        runtime.clear_failure("support_bundle_failed")
        await asyncio.to_thread(
            runtime.platform.record_audit,
            "support",
            "export_bundle",
            "success",
            f"Support bundle exported to {bundle_path.name}.",
            profile_slug=runtime.active_profile.slug,
        )
        await runtime._refresh_platform_snapshot()
    except Exception as exc:
        runtime.record_failure(
            error_code="support_bundle_failed",
            title="Support bundle export failed",
            message="KERN could not package the support bundle.",
            blocked_scope="support export",
            next_action="Rerun the export after the readiness state is clean, or review operator logs.",
            retry_available=True,
            retry_action="rerun_readiness",
            technical_detail=str(exc),
            source="support_bundle",
        )
        await asyncio.to_thread(
            runtime.platform.record_audit,
            "support",
            "export_bundle",
            "failure",
            f"Support bundle export failed: {exc}",
            profile_slug=runtime.active_profile.slug,
        )
        await runtime._refresh_platform_snapshot()
        raise
    return FileResponse(
        bundle_path,
        media_type="application/zip",
        filename=bundle_path.name,
        background=BackgroundTask(lambda: None),
    )


def register_routes(app: FastAPI, get_runtime: callable) -> None:
    global _get_runtime
    _get_runtime = get_runtime

    @app.get("/")
    async def index(request: Request) -> Response:
        if request_auth_context(request):
            return FileResponse(static_dir / "dashboard.html")
        return RedirectResponse("/login", status_code=302)

    @app.get("/dashboard")
    async def dashboard(request: Request) -> Response:
        if request_auth_context(request):
            return FileResponse(static_dir / "dashboard.html")
        return RedirectResponse("/login", status_code=302)

    @app.get("/login")
    async def login_page() -> FileResponse:
        return FileResponse(static_dir / "login.html")

    @app.post("/auth/break-glass/bootstrap")
    async def bootstrap_break_glass(request: Request) -> JSONResponse:
        runtime: KernRuntime = await _resolve_runtime(request)
        context = request_auth_context(request)
        if context is None or not context.is_bootstrap_token:
            raise HTTPException(status_code=403, detail="Bootstrap token required.")
        identity = _identity_service(request, runtime)
        payload = await request.json()
        username = str(payload.get("username") or "").strip().lower()
        password = str(payload.get("password") or "")
        admin = await asyncio.to_thread(runtime.platform.create_break_glass_admin, username, password)
        session_id, auth_context = await asyncio.to_thread(
            identity.login_break_glass,
            admin.username,
            password,
            workspace_slug=runtime.active_profile.slug,
        )
        response = JSONResponse({"admin": admin.model_dump(mode="json"), "session_id": session_id, "context": auth_context.model_dump(mode="json")})
        identity.set_session_cookie(response, session_id, secure=request.url.scheme == "https")
        return response

    @app.post("/auth/break-glass/login")
    async def break_glass_login(request: Request) -> JSONResponse:
        runtime: KernRuntime = await _resolve_runtime(request)
        identity = _identity_service(request, runtime)
        payload = await request.json()
        username = str(payload.get("username") or "").strip().lower()
        password = str(payload.get("password") or "")
        session_id, auth_context = await asyncio.to_thread(
            identity.login_break_glass,
            username,
            password,
            workspace_slug=runtime.active_profile.slug,
        )
        response = JSONResponse({"authenticated": True, "context": auth_context.model_dump(mode="json")})
        identity.set_session_cookie(response, session_id, secure=request.url.scheme == "https")
        return response

    @app.get("/auth/oidc/login")
    async def oidc_login(request: Request, return_to: str | None = None) -> Response:
        runtime: KernRuntime = await _resolve_runtime(request)
        identity = _identity_service(request, runtime)
        redirect_url, state_cookie = await identity.begin_oidc_login(redirect_to=return_to or "/dashboard")
        response = RedirectResponse(redirect_url, status_code=302)
        response.set_cookie("kern_oidc_state", state_cookie, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/", max_age=600)
        return response

    @app.get("/auth/oidc/callback")
    async def oidc_callback(request: Request, code: str, state: str) -> Response:
        runtime: KernRuntime = await _resolve_runtime(request)
        identity = _identity_service(request, runtime)
        login_result = await identity.complete_oidc_login(
            code=code,
            state=state,
            signed_state=request.cookies.get("kern_oidc_state"),
        )
        if login_result.context is None:
            return JSONResponse({"status": login_result.status, "message": login_result.message}, status_code=202)
        response = RedirectResponse(login_result.message or "/dashboard", status_code=302)
        identity.set_session_cookie(response, str(login_result.context.session_id), secure=request.url.scheme == "https")
        response.delete_cookie("kern_oidc_state", path="/")
        return response

    @app.post("/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        runtime: KernRuntime = await _resolve_runtime(request)
        context = require_request_auth_context(request)
        identity = _identity_service(request, runtime)
        await asyncio.to_thread(identity.logout, context.session_id)
        response = JSONResponse({"authenticated": False})
        identity.clear_session_cookie(response, secure=request.url.scheme == "https")
        return response

    @app.get("/auth/session")
    async def session_state(request: Request) -> JSONResponse:
        runtime: KernRuntime = await _resolve_runtime(request)
        return JSONResponse(await _session_payload(request, runtime))

    @app.get("/auth/session/workspaces")
    async def session_workspaces(request: Request) -> JSONResponse:
        runtime: KernRuntime = await _resolve_runtime(request)
        context = require_request_auth_context(request)
        identity = _identity_service(request, runtime)
        workspaces = await asyncio.to_thread(identity.list_accessible_workspaces, context)
        payload = [workspace.model_dump(mode="json") for workspace in workspaces]
        return _collection_response("workspaces", payload)

    @app.post("/auth/session/select-workspace")
    async def select_workspace(request: Request) -> JSONResponse:
        runtime: KernRuntime = await _resolve_runtime(request)
        context = require_request_auth_context(request)
        identity = _identity_service(request, runtime)
        payload = await request.json()
        workspace_slug = str(payload.get("workspace_slug") or "").strip()
        new_context = await asyncio.to_thread(identity.select_workspace, context, workspace_slug)
        runtime_manager = _get_runtime()
        next_runtime = await runtime_manager.get_runtime(new_context.workspace_slug) if hasattr(runtime_manager, "get_runtime") else runtime
        response = JSONResponse(await _session_payload(request, next_runtime, new_context))
        identity.set_session_cookie(response, str(new_context.session_id), secure=request.url.scheme == "https")
        return response

    @app.get("/admin/workspaces")
    async def list_workspaces(request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        workspaces = await asyncio.to_thread(runtime.platform.list_profiles)
        return _collection_response("workspaces", [workspace.model_dump(mode="json") for workspace in workspaces])

    @app.post("/admin/workspaces")
    async def create_workspace(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime_or_manager = _get_runtime()
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        slug = str(payload.get("slug") or "").strip() or f"workspace-{int(_time.time())}"
        title = str(payload.get("title") or "Workspace").strip()
        profile = await asyncio.to_thread(
            runtime.platform.ensure_default_profile,
            settings.profile_root,
            settings.backup_root,
            settings.db_path,
            title,
            slug,
        )
        if context and context.user_id and not context.is_break_glass:
            creator_role = "org_owner" if "org_owner" in context.roles else "org_admin"
            await asyncio.to_thread(
                runtime.platform.upsert_workspace_membership,
                user_id=context.user_id,
                workspace_slug=profile.slug,
                role=creator_role,
            )
        if hasattr(runtime_or_manager, "get_runtime"):
            await runtime_or_manager.get_runtime(profile.slug)
        workspace_payload = profile.model_dump(mode="json")
        return _action_response(item_name="workspace", item=workspace_payload)

    @app.get("/admin/workspaces/{workspace_slug}/users")
    async def list_workspace_users(workspace_slug: str, request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        users = await asyncio.to_thread(runtime.platform.list_workspace_users, workspace_slug)
        memberships_by_user: dict[str, list[dict[str, object]]] = {}
        for user in users:
            memberships = await asyncio.to_thread(runtime.platform.list_workspace_memberships, user.id)
            memberships_by_user[user.id] = [
                membership.model_dump(mode="json")
                for membership in memberships
                if membership.workspace_slug == workspace_slug
            ]
        return _collection_response(
            "users",
            [
                {
                    **user.model_dump(mode="json"),
                    "memberships": memberships_by_user.get(user.id, []),
                }
                for user in users
            ],
            workspace_slug=workspace_slug,
        )

    @app.get("/admin/users")
    async def list_users(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        users = await asyncio.to_thread(runtime.platform.list_users, context.organization_id)
        return _collection_response("users", [user.model_dump(mode="json") for user in users])

    @app.post("/admin/users")
    async def create_user(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        email = str(payload.get("email") or "").strip().lower()
        display_name = str(payload.get("display_name") or email)
        role = str(payload.get("role") or "member")
        workspace_slug = str(payload.get("workspace_slug") or context.workspace_slug or runtime.active_profile.slug)
        user = await asyncio.to_thread(
            runtime.platform.create_user,
            email=email,
            display_name=display_name,
            organization_id=context.organization_id,
            auth_source="bootstrap",
            status="active",
        )
        membership = await asyncio.to_thread(
            runtime.platform.upsert_workspace_membership,
            user_id=user.id,
            workspace_slug=workspace_slug,
            role=role,
        )
        return _action_response(
            item_name="user",
            item=user.model_dump(mode="json"),
            membership=membership.model_dump(mode="json"),
        )

    @app.post("/admin/users/{user_id}/approve")
    async def approve_user(user_id: str, request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        workspace_slug = str(payload.get("workspace_slug") or context.workspace_slug or runtime.active_profile.slug)
        role = str(payload.get("role") or "member")
        user = await asyncio.to_thread(runtime.platform.set_user_status, user_id, "active")
        membership = await asyncio.to_thread(
            runtime.platform.upsert_workspace_membership,
            user_id=user.id,
            workspace_slug=workspace_slug,
            role=role,
        )
        await asyncio.to_thread(
            runtime.platform.record_audit,
            "identity",
            "approve_user",
            "success",
            f"Approved user {user.email}.",
            profile_slug=workspace_slug,
            details={"actor_user_id": context.user_id, "approved_user_id": user.id, "role": role},
        )
        return _action_response(
            item_name="user",
            item=user.model_dump(mode="json"),
            membership=membership.model_dump(mode="json"),
        )

    @app.post("/admin/users/{user_id}/suspend")
    async def suspend_user(user_id: str, request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        user = await asyncio.to_thread(runtime.platform.set_user_status, user_id, "suspended")
        await asyncio.to_thread(runtime.platform.revoke_user_sessions, user_id)
        await asyncio.to_thread(
            runtime.platform.record_audit,
            "identity",
            "suspend_user",
            "success",
            f"Suspended user {user.email}.",
            profile_slug=runtime.active_profile.slug,
            details={"actor_user_id": context.user_id, "suspended_user_id": user.id},
        )
        return _action_response(item_name="user", item=user.model_dump(mode="json"))

    @app.post("/admin/memberships")
    async def assign_membership(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        user_id = str(payload.get("user_id") or "").strip()
        workspace_slug = str(payload.get("workspace_slug") or context.workspace_slug or runtime.active_profile.slug).strip()
        role = str(payload.get("role") or "member").strip()
        membership = await asyncio.to_thread(
            runtime.platform.upsert_workspace_membership,
            user_id=user_id,
            workspace_slug=workspace_slug,
            role=role,
        )
        await asyncio.to_thread(
            runtime.platform.record_audit,
            "identity",
            "assign_membership",
            "success",
            f"Assigned role {role} in {workspace_slug}.",
            profile_slug=workspace_slug,
            details={"actor_user_id": context.user_id, "target_user_id": user_id, "role": role},
        )
        membership_payload = membership.model_dump(mode="json")
        return _action_response(item_name="membership", item=membership_payload)

    @app.get("/admin/sessions")
    async def list_sessions(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        sessions = await asyncio.to_thread(runtime.platform.list_sessions, context.organization_id)
        return _collection_response("sessions", [session.model_dump(mode="json") for session in sessions])

    @app.post("/admin/sessions/{session_id}/revoke")
    async def revoke_session(session_id: str, request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        await asyncio.to_thread(runtime.platform.revoke_session, session_id)
        await asyncio.to_thread(
            runtime.platform.record_audit,
            "identity",
            "revoke_session",
            "success",
            "Revoked user session.",
            profile_slug=runtime.active_profile.slug,
            details={"actor_user_id": context.user_id, "revoked_session_id": session_id},
        )
        return _action_response(item_name="session", item={"revoked": True, "session_id": session_id}, revoked=True, session_id=session_id)

    @app.get("/compliance/retention-policies")
    async def list_retention_policies(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        policies = await asyncio.to_thread(runtime.platform.list_retention_policies, context.organization_id)
        return _collection_response("policies", [policy.model_dump(mode="json") for policy in policies])

    @app.post("/compliance/retention-policies")
    async def upsert_retention_policy(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        policy = await asyncio.to_thread(
            runtime.platform.upsert_retention_policy,
            organization_id=context.organization_id,
            data_class=str(payload.get("data_class") or "").strip(),
            retention_days=int(payload.get("retention_days") or 0),
            legal_hold_enabled=bool(payload.get("legal_hold_enabled", False)),
        )
        return _action_response(item_name="policy", item=policy.model_dump(mode="json"))

    @app.get("/compliance/legal-holds")
    async def list_legal_holds(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        holds = await asyncio.to_thread(runtime.platform.list_legal_holds, context.organization_id)
        return _collection_response("legal_holds", [hold.model_dump(mode="json") for hold in holds])

    @app.post("/compliance/legal-holds")
    async def create_legal_hold(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        hold = await asyncio.to_thread(
            runtime.platform.create_legal_hold,
            organization_id=context.organization_id,
            workspace_slug=str(payload.get("workspace_slug") or "").strip() or None,
            target_user_id=str(payload.get("target_user_id") or "").strip() or None,
            reason=str(payload.get("reason") or "").strip(),
        )
        _append_domain_event(
            runtime,
            organization_id=context.organization_id,
            workspace_slug=hold.workspace_slug or context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
            workflow_key="compliance",
            workflow_type="compliance_export_erasure",
            event_type="legal_hold_created",
            detail=f"Created legal hold {hold.id}.",
            metadata={"hold_id": hold.id, "target_user_id": hold.target_user_id},
        )
        return _action_response(item_name="legal_hold", item=hold.model_dump(mode="json"))

    @app.get("/compliance/erasure-requests")
    async def list_erasure_requests(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        erasure_requests = await asyncio.to_thread(runtime.platform.list_erasure_requests, context.organization_id)
        return _collection_response("erasure_requests", [item.model_dump(mode="json") for item in erasure_requests])

    @app.get("/compliance/erasure-requests/{request_id}")
    async def erasure_request_detail(request_id: str, request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        record = await asyncio.to_thread(runtime.platform.get_erasure_request, request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Erasure request not found.")
        if not (
            context.is_break_glass
            or any(role in context.roles for role in ("org_owner", "org_admin", "auditor"))
            or record.target_user_id == context.user_id
        ):
            raise HTTPException(status_code=403, detail="Erasure request access is not granted.")
        return _detail_response("erasure_request", record.model_dump(mode="json"))

    @app.post("/compliance/erasure-requests")
    async def create_erasure_request(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        target_user_id = str(payload.get("target_user_id") or context.user_id or "").strip()
        if not target_user_id:
            raise HTTPException(status_code=400, detail="A target user is required.")
        if target_user_id != context.user_id and not (
            context.is_break_glass or any(role in context.roles for role in ("org_owner", "org_admin"))
        ):
            raise HTTPException(status_code=403, detail="You can only create erasure requests for your own account.")
        erasure_request = await asyncio.to_thread(
            runtime.platform.create_erasure_request,
            organization_id=context.organization_id,
            target_user_id=target_user_id,
            requested_by_user_id=context.user_id,
            workspace_slug=str(payload.get("workspace_slug") or runtime.active_profile.slug).strip(),
            reason=str(payload.get("reason") or "").strip(),
        )
        _append_domain_event(
            runtime,
            organization_id=context.organization_id,
            workspace_slug=erasure_request.workspace_slug or context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
            workflow_key="compliance",
            workflow_type="compliance_export_erasure",
            event_type="erasure_requested",
            detail=f"Created erasure request {erasure_request.id}.",
            metadata={"request_id": erasure_request.id, "target_user_id": erasure_request.target_user_id},
        )
        return _action_response(item_name="erasure_request", item=erasure_request.model_dump(mode="json"))

    @app.post("/compliance/erasure-requests/{request_id}/execute")
    async def execute_erasure_request(request_id: str, request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        result = await asyncio.to_thread(
            _compliance_service(runtime).execute_erasure,
            request_id,
            actor_user_id=context.user_id,
        )
        _append_domain_event(
            runtime,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
            workflow_key="compliance",
            workflow_type="compliance_export_erasure",
            event_type=f"erasure_{result.get('status', 'completed')}",
            detail=f"Erasure request {request_id} was processed through the compliance workflow.",
            metadata={"request_id": request_id, "steps": result.get("steps", [])},
        )
        return _action_response(
            item_name="erasure_request",
            item=result.get("erasure_request"),
            job={
                "id": request_id,
                "status": result.get("status", "completed"),
                "steps": result.get("steps", []),
            },
            artifact={"refs": result.get("artifact_refs", [])},
            **result,
        )

    @app.get("/compliance/data-exports")
    async def list_data_exports(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        exports = await asyncio.to_thread(runtime.platform.list_data_exports, context.organization_id)
        return _collection_response("data_exports", [item.model_dump(mode="json") for item in exports])

    @app.get("/compliance/data-exports/{export_id}")
    async def data_export_detail(export_id: str, request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        record = await asyncio.to_thread(runtime.platform.get_data_export, export_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Export not found.")
        if not (
            context.is_break_glass
            or any(role in context.roles for role in ("org_owner", "org_admin", "auditor"))
            or record.target_user_id == context.user_id
        ):
            raise HTTPException(status_code=403, detail="Export access is not granted.")
        return _detail_response(
            "data_export",
            record.model_dump(mode="json"),
            artifact={"path": record.artifact_path, "refs": list(record.artifact_refs or [])},
            manifest=record.manifest.model_dump(mode="json") if record.manifest else None,
        )

    @app.get("/compliance/data-inventory")
    async def compliance_data_inventory(request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        inventory = _compliance_service(runtime).data_inventory_map()
        return _detail_response("inventory", inventory)

    @app.get("/compliance/exports/user/{user_id}")
    async def export_user_data(user_id: str, request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        if user_id != context.user_id and not (
            context.is_break_glass or any(role in context.roles for role in ("org_owner", "org_admin", "auditor"))
        ):
            raise HTTPException(status_code=403, detail="User export access is not granted.")
        runtime: KernRuntime = await _resolve_runtime(request)
        export_record = await asyncio.to_thread(
            runtime.platform.create_data_export,
            organization_id=context.organization_id,
            target_user_id=user_id,
            requested_by_user_id=context.user_id,
            status="requested",
        )
        export_record, payload = await asyncio.to_thread(
            _compliance_service(runtime).export_user_bundle,
            actor_user_id=context.user_id,
            target_user_id=user_id,
            export_record=export_record,
        )
        _append_domain_event(
            runtime,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
            workflow_key="compliance",
            workflow_type="compliance_export_erasure",
            event_type="export_generated",
            detail=f"Generated a subject export for user {user_id}.",
            metadata={"export_id": export_record.id, "target_user_id": user_id},
        )
        export_payload = export_record.model_dump(mode="json")
        return _action_response(
            item_name="export",
            item=export_payload,
            artifact=payload.get("artifact"),
            manifest=payload.get("manifest"),
            **payload,
        )

    @app.post("/compliance/exports/user/{user_id}/generate")
    async def generate_user_export(user_id: str, request: Request) -> JSONResponse:
        return await export_user_data(user_id, request)

    @app.get("/compliance/exports/workspace/{workspace_slug}")
    async def export_workspace_data(workspace_slug: str, request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        if not context.is_break_glass and not runtime.platform.has_workspace_access(context.user_id, workspace_slug, "org_owner", "org_admin", "auditor"):
            raise HTTPException(status_code=403, detail="Workspace access is not granted.")
        workspace_runtime = await _resolve_runtime(request)
        if workspace_runtime.active_profile.slug != workspace_slug:
            runtime_manager = _get_runtime()
            workspace_runtime = await runtime_manager.get_runtime(workspace_slug) if hasattr(runtime_manager, "get_runtime") else workspace_runtime
        profile = await asyncio.to_thread(workspace_runtime.platform.get_profile, workspace_slug)
        if profile is None:
            raise HTTPException(status_code=404, detail="Workspace not found.")
        export_record = await asyncio.to_thread(
            workspace_runtime.platform.create_data_export,
            organization_id=context.organization_id,
            workspace_slug=workspace_slug,
            requested_by_user_id=context.user_id,
            status="requested",
        )
        export_record, payload = await asyncio.to_thread(
            _compliance_service(workspace_runtime).export_workspace_bundle,
            actor_user_id=context.user_id,
            workspace_slug=workspace_slug,
            export_record=export_record,
        )
        _append_domain_event(
            workspace_runtime,
            organization_id=context.organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=context.user_id,
            workflow_key="compliance",
            workflow_type="compliance_export_erasure",
            event_type="workspace_export_generated",
            detail=f"Generated a workspace export for {workspace_slug}.",
            metadata={"export_id": export_record.id, "workspace_slug": workspace_slug},
        )
        export_payload = export_record.model_dump(mode="json")
        return _action_response(
            item_name="export",
            item=export_payload,
            artifact=payload.get("artifact"),
            manifest=payload.get("manifest"),
            **payload,
        )

    @app.post("/compliance/exports/workspace/{workspace_slug}/generate")
    async def generate_workspace_export(workspace_slug: str, request: Request) -> JSONResponse:
        return await export_workspace_data(workspace_slug, request)

    @app.get("/compliance/regulated-documents")
    async def list_regulated_documents_route(request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        documents = await asyncio.to_thread(_ensure_memory(runtime).list_regulated_documents, 200)
        return _collection_response("regulated_documents", [item.model_dump(mode="json") for item in documents])

    @app.get("/compliance/regulated-documents/candidates")
    async def list_regulated_document_candidates_route(request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        records = await asyncio.to_thread(runtime.memory.list_document_records, 200)
        regulated = await asyncio.to_thread(_ensure_memory(runtime).list_regulated_documents, 200)
        finalized_ids = {item.document_id for item in regulated if item.document_id}
        finalized_titles = {item.title.strip().lower() for item in regulated if item.title}
        candidates = []
        for record in records:
            metadata = record.metadata or {}
            is_candidate = bool(metadata.get("regulated_candidate")) or record.data_class == "regulated_business"
            if not is_candidate:
                continue
            if record.id in finalized_ids or record.title.strip().lower() in finalized_titles:
                continue
            candidates.append(record.model_dump(mode="json"))
        return _collection_response("regulated_document_candidates", candidates)

    @app.post("/compliance/regulated-documents/finalize")
    async def finalize_regulated_document_route(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        record = await asyncio.to_thread(
            _compliance_service(runtime).finalize_regulated_document,
            actor_user_id=context.user_id,
            title=str(payload.get("title") or "").strip() or None,
            document_id=str(payload.get("document_id") or "").strip() or None,
            business_document_id=str(payload.get("business_document_id") or "").strip() or None,
            retention_state=str(payload.get("retention_state") or "retention_locked"),
        )
        _append_domain_event(
            runtime,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
            workflow_key="regulated",
            workflow_type="regulated_document_lifecycle",
            event_type="document_finalized",
            detail=f"Regulated document {record.id} was finalized into immutable state.",
            metadata={"regulated_document_id": record.id, "current_version_id": record.current_version_id},
        )
        return _action_response(item_name="regulated_document", item=record.model_dump(mode="json"))

    @app.get("/compliance/regulated-documents/{regulated_id}/versions")
    async def regulated_document_versions_route(regulated_id: str, request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        versions = await asyncio.to_thread(_ensure_memory(runtime).list_regulated_document_versions, regulated_id)
        return _collection_response("versions", [item.model_dump(mode="json") for item in versions], regulated_document_id=regulated_id)

    @app.get("/intelligence/world-state")
    async def intelligence_world_state(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        snapshot = await asyncio.to_thread(
            _reasoning_service(runtime).world_state,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        return _detail_response("world_state", snapshot.model_dump(mode="json"))

    @app.get("/intelligence/workbench")
    async def intelligence_workbench(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await asyncio.to_thread(
            _reasoning_service(runtime).build_worker_workbench,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        world_state = payload["world_state"]
        recommendations = payload["recommendations"]
        focus_hints = payload["focus_hints"]
        decisions = payload["decisions"]
        return _detail_response(
            "workbench",
            {
                "world_state": world_state.model_dump(mode="json"),
                "recommendations": [item.model_dump(mode="json") for item in recommendations],
                "focus_hints": [item.model_dump(mode="json") for item in focus_hints],
                "decisions": [item.model_dump(mode="json") for item in decisions],
            },
        )

    @app.get("/intelligence/workflows")
    async def intelligence_workflows(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        workflows = await asyncio.to_thread(
            _reasoning_service(runtime).list_workflows,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        return _collection_response("workflows", [item.model_dump(mode="json") for item in workflows])

    @app.get("/intelligence/workflows/{workflow_id}")
    async def intelligence_workflow_detail(workflow_id: str, request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        workflow = await asyncio.to_thread(
            _reasoning_service(runtime).get_workflow,
            workflow_id,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        events = await asyncio.to_thread(_ensure_memory(runtime).list_workflow_events, workflow_id)
        domain_events = await asyncio.to_thread(
            _ensure_memory(runtime).list_workflow_domain_events,
            workflow_id=workflow_id,
            limit=50,
        )
        return _detail_response(
            "workflow",
            workflow.model_dump(mode="json"),
            events=[item.model_dump(mode="json") for item in events],
            domain_events=[item.model_dump(mode="json") for item in domain_events],
        )

    @app.get("/intelligence/obligations")
    async def intelligence_obligations(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        obligations = await asyncio.to_thread(
            _reasoning_service(runtime).list_obligations,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        return _collection_response("obligations", [item.model_dump(mode="json") for item in obligations])

    @app.get("/intelligence/recommendations")
    async def intelligence_recommendations(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        recommendations = await asyncio.to_thread(
            _reasoning_service(runtime).list_recommendations,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        return _collection_response("recommendations", [item.model_dump(mode="json") for item in recommendations])

    @app.get("/intelligence/recommendations/{recommendation_id}")
    async def intelligence_recommendation_detail(recommendation_id: str, request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        recommendation = await asyncio.to_thread(
            _reasoning_service(runtime).get_recommendation,
            recommendation_id,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        if recommendation is None:
            raise HTTPException(status_code=404, detail="Recommendation not found.")
        return _detail_response(
            "recommendation",
            recommendation.model_dump(mode="json"),
            evidence_bundle=recommendation.evidence_bundle.model_dump(mode="json"),
            ranking_explanation=recommendation.ranking_explanation.model_dump(mode="json"),
        )

    @app.get("/intelligence/focus-hints")
    async def intelligence_focus_hints(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        hints = await asyncio.to_thread(
            _reasoning_service(runtime).list_focus_hints,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        return _collection_response("focus_hints", [item.model_dump(mode="json") for item in hints])

    @app.get("/intelligence/preparation")
    async def intelligence_preparation(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        query = str(request.query_params.get("query") or "").strip()
        if query:
            packet = await asyncio.to_thread(
                _reasoning_service(runtime).get_preparation_packet_for_transcript,
                query,
                organization_id=context.organization_id,
                workspace_slug=context.workspace_slug or runtime.active_profile.slug,
                actor_user_id=context.user_id,
            )
        else:
            recommendations = await asyncio.to_thread(
                _reasoning_service(runtime).list_recommendations,
                organization_id=context.organization_id,
                workspace_slug=context.workspace_slug or runtime.active_profile.slug,
                actor_user_id=context.user_id,
            )
            packet = None
            if recommendations:
                packet = await asyncio.to_thread(
                    _reasoning_service(runtime).get_preparation_packet,
                    recommendations[0].id,
                    organization_id=context.organization_id,
                    workspace_slug=context.workspace_slug or runtime.active_profile.slug,
                    actor_user_id=context.user_id,
                )
        if packet is None:
            raise HTTPException(status_code=404, detail="No preparation packet is available.")
        return _detail_response("preparation_packet", packet.model_dump(mode="json"), query=query or None)

    @app.post("/intelligence/document-query")
    async def intelligence_document_query(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        query = str(payload.get("query") or request.query_params.get("query") or "").strip()
        selected_document_ids = [
            str(item).strip()
            for item in (payload.get("document_ids") or payload.get("selected_document_ids") or [])
            if str(item).strip()
        ]
        if not query:
            raise HTTPException(status_code=400, detail="A query is required.")
        packet = await asyncio.to_thread(
            _reasoning_service(runtime).get_document_answer_packet_for_transcript,
            query,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
            selected_document_ids=selected_document_ids,
        )
        if packet is None:
            raise HTTPException(status_code=404, detail="No document answer packet is available.")
        return _detail_response("document_answer_packet", packet.model_dump(mode="json"), query=query)

    @app.post("/intelligence/freeform-route")
    async def intelligence_freeform_route(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        query = str(payload.get("query") or request.query_params.get("query") or "").strip()
        selected_document_ids = [
            str(item).strip()
            for item in (payload.get("document_ids") or payload.get("selected_document_ids") or [])
            if str(item).strip()
        ]
        if not query:
            raise HTTPException(status_code=400, detail="A query is required.")
        routed = await asyncio.to_thread(
            _reasoning_service(runtime).route_freeform_for_transcript,
            query,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
            selected_document_ids=selected_document_ids,
        )
        response: dict[str, object] = {
            "task_intent": routed["task_intent"].model_dump(mode="json") if routed.get("task_intent") is not None else None,
            "packet_type": routed.get("packet_type"),
            "query": query,
        }
        packet = routed.get("packet")
        if packet is not None and hasattr(packet, "model_dump"):
            response["packet"] = packet.model_dump(mode="json")
        return _detail_response("freeform_route", response, query=query)

    @app.post("/intelligence/thread-context")
    async def intelligence_thread_context(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        query = str(payload.get("query") or request.query_params.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="A query is required.")
        packet = await asyncio.to_thread(
            _reasoning_service(runtime).get_thread_context_packet_for_transcript,
            query,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        return _detail_response("thread_context_packet", packet.model_dump(mode="json"), query=query)

    @app.post("/intelligence/person-context")
    async def intelligence_person_context(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        query = str(payload.get("query") or request.query_params.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="A query is required.")
        packet = await asyncio.to_thread(
            _reasoning_service(runtime).get_person_context_packet_for_transcript,
            query,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        return _detail_response("person_context_packet", packet.model_dump(mode="json"), query=query)

    @app.get("/intelligence/document-query/{packet_id}")
    async def intelligence_document_query_detail(packet_id: str, request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        packet = await asyncio.to_thread(
            _reasoning_service(runtime).get_document_answer_packet,
            packet_id,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        if packet is None:
            raise HTTPException(status_code=404, detail="Document answer packet not found.")
        return _detail_response("document_answer_packet", packet.model_dump(mode="json"))

    @app.get("/intelligence/preparation/{recommendation_id}")
    async def intelligence_preparation_detail(recommendation_id: str, request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        packet = await asyncio.to_thread(
            _reasoning_service(runtime).get_preparation_packet,
            recommendation_id,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        if packet is None:
            raise HTTPException(status_code=404, detail="Preparation packet not found.")
        return _detail_response("preparation_packet", packet.model_dump(mode="json"))

    @app.post("/intelligence/preparation/{recommendation_id}/draft")
    async def intelligence_preparation_draft(recommendation_id: str, request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        mode = str(request.query_params.get("mode") or "").strip()
        with contextlib.suppress(Exception):
            payload = await request.json()
            if isinstance(payload, dict):
                mode = str(payload.get("mode") or mode).strip()
        packet = await asyncio.to_thread(
            _reasoning_service(runtime).get_preparation_packet,
            recommendation_id,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        if packet is None:
            raise HTTPException(status_code=404, detail="Preparation packet not found.")
        draft: SuggestedDraftRecord | None = None
        if mode == "llm_rewrite" and getattr(runtime, "orchestrator", None) is not None:
            draft = await runtime.orchestrator._render_packet_with_llm(packet)
        if draft is None:
            draft = await asyncio.to_thread(_reasoning_service(runtime).build_draft_from_packet, packet)
        if draft is None:
            raise HTTPException(status_code=409, detail="This packet does not support deterministic draft generation.")
        return _action_response(
            item_name="suggested_draft",
            item=draft.model_dump(mode="json"),
            preparation_packet=packet.model_dump(mode="json"),
            render_mode=mode or draft.mode,
        )

    @app.get("/intelligence/evidence/{bundle_id}")
    async def intelligence_evidence_bundle_detail(bundle_id: str, request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        bundle = await asyncio.to_thread(
            _reasoning_service(runtime).get_evidence_bundle,
            bundle_id,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        if bundle is None:
            raise HTTPException(status_code=404, detail="Evidence bundle not found.")
        return _detail_response("evidence_bundle", bundle.model_dump(mode="json"))

    @app.get("/intelligence/decisions")
    async def intelligence_decisions(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        decisions = await asyncio.to_thread(
            _reasoning_service(runtime).list_decisions,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
        )
        return _collection_response("decisions", [item.model_dump(mode="json") for item in decisions])

    @app.get("/intelligence/memory")
    async def intelligence_memory(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        query = str(request.query_params.get("query") or "").strip()
        service = _intelligence_service(runtime)
        if query:
            items = await asyncio.to_thread(
                service.retrieve_memory_context,
                query,
                organization_id=context.organization_id,
                workspace_slug=context.workspace_slug or runtime.active_profile.slug,
                user_id=context.user_id,
                limit=20,
            )
        else:
            items = await asyncio.to_thread(
                service.list_memory,
                organization_id=context.organization_id,
                workspace_slug=context.workspace_slug or runtime.active_profile.slug,
                user_id=context.user_id,
                limit=50,
            )
        return _collection_response("memory", items, query=query or None)

    @app.get("/intelligence/memory/{memory_item_id}")
    async def intelligence_memory_detail(memory_item_id: str, request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        item = await asyncio.to_thread(_ensure_memory(runtime).get_structured_memory_item, memory_item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Memory item not found.")
        if item.get("organization_id") not in {None, context.organization_id}:
            raise HTTPException(status_code=403, detail="Memory access is not granted.")
        if item.get("scope") == "user_private" and item.get("user_id") not in {None, context.user_id} and not (
            context.is_break_glass or any(role in context.roles for role in ("org_owner", "org_admin", "auditor"))
        ):
            raise HTTPException(status_code=403, detail="Private memory access is not granted.")
        return _detail_response("memory_item", item)

    @app.post("/intelligence/feedback")
    async def intelligence_feedback(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        signal_type = str(payload.get("signal_type") or "").strip()
        source_type = str(payload.get("source_type") or "").strip()
        source_id = str(payload.get("source_id") or "").strip()
        signal = await asyncio.to_thread(
            _intelligence_service(runtime).capture_feedback,
            actor_user_id=context.user_id,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            signal_type=signal_type,
            source_type=source_type,
            source_id=source_id,
            memory_item_id=str(payload.get("memory_item_id") or "").strip() or None,
            metadata=dict(payload.get("metadata") or {}),
            approved_for_training=bool(payload.get("approved_for_training", False)),
        )
        if source_type == "preparation":
            recommendation = await asyncio.to_thread(
                _reasoning_service(runtime).get_recommendation,
                source_id,
                organization_id=context.organization_id,
                workspace_slug=context.workspace_slug or runtime.active_profile.slug,
                actor_user_id=context.user_id,
            )
            event_type = signal_type if signal_type.startswith("packet_") else f"packet_{signal_type}"
            _append_domain_event(
                runtime,
                organization_id=context.organization_id,
                workspace_slug=context.workspace_slug or runtime.active_profile.slug,
                actor_user_id=context.user_id,
                workflow_key="correspondence" if recommendation is None else None,
                workflow_id=recommendation.workflow_id if recommendation is not None else None,
                workflow_type=recommendation.workflow_type if recommendation and recommendation.workflow_type else "correspondence_follow_up",
                event_type=event_type,
                detail=f"Preparation packet feedback recorded as {signal_type}.",
                metadata={"source_id": source_id, "signal_id": signal.id},
            )
        return _action_response(item_name="feedback_signal", item=signal.model_dump(mode="json"))

    @app.post("/intelligence/memory/promote")
    async def intelligence_promote(request: Request) -> JSONResponse:
        context = require_request_auth_context(request)
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        signal_type = str(payload.get("signal_type") or "promote_workspace").strip()
        source_type = str(payload.get("source_type") or "memory").strip()
        source_id = str(payload.get("source_id") or payload.get("memory_item_id") or "").strip()
        signal = await asyncio.to_thread(
            _intelligence_service(runtime).capture_feedback,
            actor_user_id=context.user_id,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            signal_type=signal_type,
            source_type=source_type,
            source_id=source_id,
            memory_item_id=str(payload.get("memory_item_id") or "").strip() or None,
            metadata=dict(payload.get("metadata") or {}),
            approved_for_training=bool(payload.get("approved_for_training", True)),
        )
        _append_domain_event(
            runtime,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
            workflow_key="review",
            workflow_type="review_approval_queue",
            event_type=f"promotion_{signal_type}",
            detail=f"Promotion feedback recorded as {signal_type}.",
            metadata={"source_id": source_id, "signal_id": signal.id},
        )
        return _action_response(item_name="feedback_signal", item=signal.model_dump(mode="json"))

    @app.get("/intelligence/promotion-candidates")
    async def intelligence_promotion_candidates(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        candidates = await asyncio.to_thread(
            _intelligence_service(runtime).list_promotion_candidates,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
        )
        return _collection_response("promotion_candidates", candidates)

    @app.get("/intelligence/promotion-candidates/{memory_item_id}")
    async def intelligence_promotion_candidate_detail(memory_item_id: str, request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        candidate = await asyncio.to_thread(_intelligence_service(runtime).get_promotion_candidate, memory_item_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Promotion candidate not found.")
        if candidate.get("organization_id") not in {None, context.organization_id}:
            raise HTTPException(status_code=403, detail="Promotion candidate access is not granted.")
        return _detail_response("promotion_candidate", candidate)

    @app.post("/intelligence/promotion-candidates/{memory_item_id}/review")
    async def intelligence_review_promotion_candidate(memory_item_id: str, request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        decision = str(payload.get("decision") or "").strip()
        candidate = await asyncio.to_thread(
            _intelligence_service(runtime).review_promotion_candidate,
            actor_user_id=context.user_id,
            memory_item_id=memory_item_id,
            decision=decision,
            reason=str(payload.get("reason") or "").strip(),
        )
        if candidate is None:
            raise HTTPException(status_code=404, detail="Promotion candidate not found.")
        _append_domain_event(
            runtime,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
            workflow_key="review",
            workflow_type="review_approval_queue",
            event_type="promotion_candidate_reviewed",
            detail=f"Promotion candidate {memory_item_id} was reviewed as {decision}.",
            metadata={"memory_item_id": memory_item_id, "decision": decision},
        )
        return _action_response(item_name="promotion_candidate", item=candidate, decision=decision)

    @app.get("/intelligence/training-examples")
    async def intelligence_training_examples(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        examples = await asyncio.to_thread(
            _intelligence_service(runtime).list_training_examples,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            user_id=None if (context.is_break_glass or any(role in context.roles for role in ("org_owner", "org_admin", "auditor"))) else context.user_id,
        )
        return _collection_response("training_examples", [item.model_dump(mode="json") for item in examples])

    @app.get("/intelligence/training-examples/{example_id}")
    async def intelligence_training_example_detail(example_id: str, request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        example = await asyncio.to_thread(_ensure_memory(runtime).get_training_example, example_id)
        if example is None:
            raise HTTPException(status_code=404, detail="Training example not found.")
        if example.organization_id not in {None, context.organization_id}:
            raise HTTPException(status_code=403, detail="Training example access is not granted.")
        return _detail_response("training_example", example.model_dump(mode="json"))

    @app.post("/intelligence/training-examples/{example_id}/review")
    async def intelligence_review_training_example(example_id: str, request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        status = str(payload.get("status") or "").strip()
        example = await asyncio.to_thread(
            _intelligence_service(runtime).review_training_example,
            example_id,
            status=status,
            actor_user_id=context.user_id,
        )
        _append_domain_event(
            runtime,
            organization_id=context.organization_id,
            workspace_slug=context.workspace_slug or runtime.active_profile.slug,
            actor_user_id=context.user_id,
            workflow_key="review",
            workflow_type="review_approval_queue",
            event_type="training_example_reviewed",
            detail=f"Training example {example_id} was reviewed as {status}.",
            metadata={"example_id": example_id, "status": status},
        )
        return _action_response(item_name="training_example", item=example.model_dump(mode="json"), decision=status)

    @app.post("/intelligence/training-exports")
    async def create_training_export(request: Request) -> JSONResponse:
        context = _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        payload = await request.json()
        target_workspace = str(payload.get("workspace_slug") or context.workspace_slug or runtime.active_profile.slug).strip()
        export = await asyncio.to_thread(
            _intelligence_service(runtime).export_training_dataset,
            actor_user_id=context.user_id,
            workspace_slug=target_workspace,
        )
        _append_domain_event(
            runtime,
            organization_id=context.organization_id,
            workspace_slug=target_workspace,
            actor_user_id=context.user_id,
            workflow_key="review",
            workflow_type="review_approval_queue",
            event_type="training_export_generated",
            detail=f"Training export {export.get('dataset', {}).get('id')} was generated for workspace review.",
            metadata={"export_id": export.get("dataset", {}).get("id"), "workspace_slug": target_workspace},
        )
        return _action_response(
            item_name="training_export",
            item=export.get("dataset"),
            artifact={"path": next(iter(export.get("dataset", {}).get("artifacts", [])), None)},
            manifest=export.get("dataset"),
            **export,
        )

    @app.get("/intelligence/training-exports")
    async def list_training_exports(request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        root = Path(runtime.active_profile.profile_root) / "training-exports"
        exports = []
        if root.exists():
            for manifest in sorted(root.glob("*/manifest.json"), reverse=True):
                exports.append(json.loads(manifest.read_text(encoding="utf-8")))
        return _collection_response("training_exports", exports)

    @app.get("/intelligence/training-exports/{export_id}")
    async def training_export_detail(export_id: str, request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        manifest = _read_training_manifest(runtime, export_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="Training export not found.")
        return _detail_response(
            "training_export",
            {
                "id": export_id,
                "artifact_path": next(iter(manifest.get("artifacts", [])), None),
                "workspace_slug": manifest.get("workspace_slug"),
                "generated_at": manifest.get("generated_at") or manifest.get("created_at"),
            },
            manifest=manifest,
            artifact={"path": next(iter(manifest.get("artifacts", [])), None)},
        )

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        runtime: KernRuntime = await _resolve_runtime(request)
        snapshot = runtime.orchestrator.snapshot
        severity = _health_severity(runtime, snapshot)
        payload = {
            "status": severity,
            "components": _health_components(runtime),
            "version": __version__,
            "app_version": getattr(snapshot.model_info, "app_version", "0.0.0"),
            "profile_locked": runtime.platform.is_profile_locked(runtime.active_profile.slug),
            "audit_chain_ok": runtime.audit_chain_ok,
            "llm_available": runtime.orchestrator.snapshot.llm_available,
            "scheduler_enabled": bool(getattr(runtime, "scheduler_service", None)),
            "runtime_degraded_reasons": getattr(snapshot, "runtime_degraded_reasons", []),
            "using_locked_scaffold": bool(getattr(runtime, "_using_locked_scaffold", False)),
            "uptime_seconds": int(_time.time() - _startup_time),
            "memory_mb": _get_memory_mb(),
        }
        return JSONResponse(payload, status_code=_health_status_code(severity))

    @app.get("/health/live")
    async def health_live(request: Request) -> JSONResponse:
        runtime: KernRuntime = await _resolve_runtime(request)
        severity = _health_severity(runtime, runtime.orchestrator.snapshot)
        payload = {"status": "live" if severity in {"ok", "warning", "degraded"} else "error", "severity": severity}
        return JSONResponse(payload, status_code=200 if severity != "error" else 500)

    @app.get("/health/ready")
    async def health_ready(request: Request) -> JSONResponse:
        runtime: KernRuntime = await _resolve_runtime(request)
        severity = _health_severity(runtime, runtime.orchestrator.snapshot)
        payload = {"status": "ready" if severity in {"ok", "warning"} else "not_ready", "severity": severity}
        return JSONResponse(payload, status_code=200 if severity in {"ok", "warning"} else 503)

    @app.get("/api/readiness")
    async def readiness(request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        await runtime._refresh_platform_snapshot()
        return JSONResponse(
            {
                "summary": runtime.orchestrator.snapshot.readiness_summary.model_dump(mode="json"),
                "checks": [check.model_dump(mode="json") for check in runtime.orchestrator.snapshot.readiness_checks],
            }
        )

    @app.get("/api/license")
    async def license_state(request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        await runtime._refresh_platform_snapshot()
        return JSONResponse(
            {
                "state": runtime.orchestrator.snapshot.license_state,
                "summary": runtime.orchestrator.snapshot.license_summary.model_dump(mode="json"),
            }
        )

    @app.post("/api/license/import")
    async def import_license(request: Request, license_file: UploadFile = File(...)) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        if runtime.platform.is_profile_locked(runtime.active_profile.slug):
            raise HTTPException(status_code=423, detail="Unlock the active KERN profile before importing a license.")
        temp_dir = Path(tempfile.mkdtemp(prefix="kern_license_upload_"))
        temp_path = temp_dir / Path(license_file.filename or "license.json").name
        try:
            with temp_path.open("wb") as handle:
                shutil.copyfileobj(license_file.file, handle)
            try:
                evaluation = await asyncio.to_thread(runtime.license_service.import_license_file, temp_path)
            except ValueError as exc:
                runtime.record_failure(
                    error_code="license_invalid",
                    title="Offline license is invalid",
                    message="KERN could not validate the uploaded offline license file.",
                    blocked_scope="production drafting",
                    next_action="Upload a valid signed license file issued for this install.",
                    retry_available=True,
                    retry_action="rerun_license_check",
                    technical_detail=str(exc),
                    source="license",
                )
                await runtime._refresh_platform_snapshot()
                raise HTTPException(status_code=422, detail=str(exc))
            runtime.clear_failure("license_required", "license_invalid", "license_expired")
            runtime.orchestrator.snapshot.last_action = "Offline license imported."
            runtime.orchestrator.snapshot.response_text = "Offline license details were updated for this install."
            await runtime._refresh_platform_snapshot()
            await asyncio.to_thread(
                runtime.platform.record_audit,
                "license",
                "import_license",
                "success",
                "Imported offline license file.",
                profile_slug=runtime.active_profile.slug,
                details={"plan": evaluation.plan, "expires_at": evaluation.expires_at},
            )
            return JSONResponse(
                {
                    "state": evaluation.status,
                    "summary": runtime.orchestrator.snapshot.license_summary.model_dump(mode="json"),
                }
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @app.get("/metrics")
    async def metrics_endpoint(request: Request) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "auditor", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        snap = metrics.snapshot()
        snap["active_websocket_connections"] = getattr(runtime, "_ws_connection_count", 0)
        return JSONResponse(snap)

    @app.get("/api/version")
    async def api_version():
        """Return the current KERN version."""
        import importlib.metadata
        try:
            version = importlib.metadata.version("kern")
        except Exception as exc:
            logger.debug("Could not read package version, using fallback: %s", exc)
            version = "0.1.0"
        return {"version": version, "product": "KERN AI Workspace", "posture": settings.product_posture}

    @app.post("/logs/export")
    async def export_logs_route(request: Request):
        return await export_logs(request)

    @app.post("/governance/export")
    async def export_governance_bundle_route(request: Request):
        return await export_governance_bundle(request)

    @app.post("/support/export")
    async def export_support_bundle_route(request: Request):
        return await export_support_bundle(request)

    @app.post("/upload")
    async def upload_files(
        request: Request,
        files: list[UploadFile] = File(...),
        category: str | None = Form(None),
        tags: str | None = Form(None),
    ) -> JSONResponse:
        _require_roles(request, "org_owner", "org_admin", "member", "break_glass_admin")
        runtime: KernRuntime = await _resolve_runtime(request)
        if hasattr(runtime, "ensure_production_access") and not runtime.ensure_production_access(blocked_scope="document upload"):
            raise HTTPException(status_code=403, detail="Production access is blocked by license state.")
        if runtime.platform.is_profile_locked(runtime.active_profile.slug):
            raise HTTPException(status_code=423, detail="Unlock the active KERN profile before uploading documents.")
        _http_policy_gate(
            runtime,
            "upload_documents",
            arguments={"category": category or "", "tags": tags or ""},
            descriptor=UPLOAD_DOCUMENTS_DESCRIPTOR,
        )
        # --- Upload validation ---
        max_file_bytes = UPLOAD_MAX_FILE_MB * 1024 * 1024
        max_batch_bytes = UPLOAD_MAX_BATCH_MB * 1024 * 1024
        batch_size = 0
        validation_items: list[dict[str, object]] = []
        accepted_uploads: list[tuple[UploadFile, int]] = []
        for upload in files:
            upload_name = upload.filename or "upload"
            filename_err = _validate_upload_filename(upload.filename or "")
            if filename_err:
                validation_items.append({
                    "name": upload_name,
                    "status": "rejected",
                    "detail": filename_err,
                })
                continue
            upload.file.seek(0, 2)
            file_size = upload.file.tell()
            upload.file.seek(0)
            if file_size > max_file_bytes:
                validation_items.append({
                    "name": upload_name,
                    "status": "rejected",
                    "detail": f"File is {file_size // (1024 * 1024)}MB. Maximum size is {UPLOAD_MAX_FILE_MB}MB.",
                })
                continue
            batch_size += file_size
            if batch_size > max_batch_bytes:
                batch_size -= file_size
                validation_items.append({
                    "name": upload_name,
                    "status": "rejected",
                    "detail": f"Batch limit reached. KERN accepts up to {UPLOAD_MAX_BATCH_MB}MB per upload.",
                })
                continue
            accepted_uploads.append((upload, file_size))

        tag_list: list[str] = [t.strip() for t in (tags or "").split(",") if t.strip()]
        tmp_dir = Path(tempfile.mkdtemp(prefix="kern_upload_"))
        try:
            file_paths: list[Path] = []
            for upload, _file_size in accepted_uploads:
                source_name = Path(upload.filename or "upload")
                safe_name = source_name.name
                dest = tmp_dir / f"{source_name.stem}-{os.urandom(6).hex()}{source_name.suffix}"
                with dest.open("wb") as f:
                    shutil.copyfileobj(upload.file, f)
                file_paths.append(dest)
            if hasattr(runtime.document_service, "ingest_batch_report"):
                report = await asyncio.to_thread(
                    runtime.document_service.ingest_batch_report,
                    file_paths,
                    source="upload",
                    category=category or None,
                    tags=tag_list,
                )
                records = list(report.get("records") or [])
                report_items = list(validation_items) + list(report.get("items") or [])
                duplicates_count = int(report.get("duplicates") or 0)
            else:
                records = list(
                    await asyncio.to_thread(
                        runtime.document_service.ingest_batch,
                        file_paths,
                        source="upload",
                        category=category or None,
                        tags=tag_list,
                    )
                )
                report_items = list(validation_items)
                duplicates_count = 0
            document_details = {}
            if records and getattr(runtime, "memory", None) is not None and hasattr(runtime.memory, "get_document_details"):
                document_details = await asyncio.to_thread(
                    runtime.memory.get_document_details,
                    [record.id for record in records],
                )
            indexed_documents = []
            ocr_low_confidence_count = 0
            for record in records:
                detail = document_details.get(record.id, {})
                if detail.get("ocr_low_confidence"):
                    ocr_low_confidence_count += 1
                indexed_documents.append({
                    "id": record.id,
                    "title": record.title,
                    "category": record.category,
                    "file_type": getattr(record, "file_type", ""),
                    "ocr_low_confidence": bool(detail.get("ocr_low_confidence", False)),
                    "ocr_used": bool(detail.get("ocr_used", False)),
                })
            if hasattr(runtime, "clear_failure"):
                runtime.clear_failure("upload_invalid", "upload_corrupt", "document_ingest_failed")
            retrieval_service = getattr(runtime, "retrieval_service", None)
            memory_scope_getter = getattr(getattr(runtime, "local_data", None), "memory_scope", None)
            scope = memory_scope_getter() if callable(memory_scope_getter) else "profile_plus_archive"
            if retrieval_service is not None and records:
                if getattr(runtime, "defer_retrieval_refresh_after_upload", False):
                    _schedule_retrieval_refresh(runtime, scope)
                else:
                    if settings.rag_enabled:
                        await asyncio.to_thread(retrieval_service.rebuild_index, scope)
                    if settings.vec_enabled and hasattr(retrieval_service, "rebuild_vec_index"):
                        await asyncio.to_thread(retrieval_service.rebuild_vec_index, scope)
            runtime.platform.record_audit(
                "documents",
                "upload_files",
                "success",
                f"Processed {len(files)} uploaded file(s) via HTTP. Indexed {len(records)}.",
                profile_slug=runtime.active_profile.slug,
                details={
                    "count": len(records),
                    "filenames": [f.filename for f in files],
                    "duplicates": duplicates_count,
                    "rejected": sum(1 for item in report_items if item.get("status") == "rejected"),
                },
            )
            rejected_count = sum(1 for item in report_items if item.get("status") == "rejected")
            failed_count = sum(1 for item in report_items if item.get("status") == "failed")
            if not records and (rejected_count or failed_count):
                if hasattr(runtime, "record_failure"):
                    runtime.record_failure(
                        error_code="upload_invalid",
                        title="Document upload was rejected",
                        message="KERN could not accept any file from this batch.",
                        blocked_scope="document upload",
                        next_action="Remove unsupported or oversized files and try the batch again.",
                        technical_detail="; ".join(str(item.get("detail") or "") for item in report_items[:3]),
                        source="upload",
                    )
                return JSONResponse(
                    status_code=422,
                    content={
                        "indexed": 0,
                        "total": len(files),
                        "accepted": len(accepted_uploads),
                        "duplicates": duplicates_count,
                        "rejected": rejected_count,
                        "failed": failed_count,
                        "documents": [],
                        "items": report_items,
                        "ocr_low_confidence_count": 0,
                        "detail": "KERN could not accept any file from this batch.",
                    },
                )
            return JSONResponse({
                "indexed": len(records),
                "total": len(files),
                "accepted": len(accepted_uploads),
                "duplicates": duplicates_count,
                "rejected": rejected_count,
                "failed": failed_count,
                "documents": indexed_documents,
                "items": report_items,
                "ocr_low_confidence_count": ocr_low_confidence_count,
            })
        except HTTPException:
            if hasattr(runtime, "_refresh_platform_snapshot"):
                await runtime._refresh_platform_snapshot()
            raise
        except Exception as exc:
            if hasattr(runtime, "record_failure"):
                runtime.record_failure(
                    error_code="document_ingest_failed",
                    title="Document ingestion failed",
                    message="KERN could not index one or more uploaded files.",
                    blocked_scope="document upload",
                    next_action="Retry the upload. If the same file fails again, export a support bundle for operator review.",
                    retry_available=True,
                    retry_action="rerun_readiness",
                    technical_detail=str(exc),
                    source="upload",
                )
            if hasattr(runtime, "_refresh_platform_snapshot"):
                await runtime._refresh_platform_snapshot()
            raise HTTPException(status_code=422, detail="KERN could not index the uploaded files. Retry the upload or review the support bundle path.")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
