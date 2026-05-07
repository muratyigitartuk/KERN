from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from app.auth import ensure_websocket_allowed, redact_error_detail
from app.config import settings
from app.path_safety import validate_user_import_path
from app.tracing import generate_request_id, request_id_var
from app.types import (
    BackupTarget,
    CapabilityDescriptor,
    ExecutionPlan,
    PendingConfirmation,
    PlanStep,
    UICommand,
    WorkflowDomainEvent,
)

if TYPE_CHECKING:
    from app.runtime import KernRuntime

_WS_MAX_TEXT_BYTES = 32 * 1024
_WS_BUCKETS: dict[str, list[float]] = {}
_SCHEDULE_ACTION_TYPES = frozenset({"custom_prompt", "generate_report"})
_COMMAND_ROLE_REQUIREMENTS = {
    "submit_text": {"org_owner", "org_admin", "member", "auditor", "break_glass_admin"},
    "confirm_action": {"org_owner", "org_admin", "member", "break_glass_admin"},
    "cancel_action": {"org_owner", "org_admin", "member", "auditor", "break_glass_admin"},
    "reset_conversation": {"org_owner", "org_admin", "member", "break_glass_admin"},
    "search_knowledge": {"org_owner", "org_admin", "member", "auditor", "break_glass_admin"},
    "update_settings": {"org_owner", "org_admin", "member", "break_glass_admin"},
    "create_backup": {"org_owner", "org_admin", "break_glass_admin"},
    "restore_backup": {"org_owner", "org_admin", "break_glass_admin"},
    "export_audit": {"org_owner", "org_admin", "auditor", "break_glass_admin"},
    "create_schedule": {"org_owner", "org_admin", "break_glass_admin"},
    "delete_schedule": {"org_owner", "org_admin", "break_glass_admin"},
    "toggle_schedule": {"org_owner", "org_admin", "break_glass_admin"},
    "retry_failed_task": {"org_owner", "org_admin", "break_glass_admin"},
    "lock_profile": {"org_owner", "org_admin", "member", "break_glass_admin"},
    "unlock_profile": {"org_owner", "org_admin", "member", "break_glass_admin"},
    "set_profile_pin": {"org_owner", "org_admin", "member", "break_glass_admin"},
}
_UNLISTED_COMMAND_ROLES: frozenset[str] = frozenset({"org_owner", "org_admin", "break_glass_admin"})
_EXPENSIVE_COMMANDS = frozenset({
    "submit_text",
    "search_knowledge",
    "ingest_files",
    "create_backup",
    "restore_backup",
    "export_audit",
})


def _consume_ws_budget(key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    now = time.monotonic()
    cutoff = now - window_seconds
    bucket = [stamp for stamp in _WS_BUCKETS.get(key, []) if stamp > cutoff]
    if len(bucket) >= limit:
        retry_after = int(bucket[0] + window_seconds - now) + 1
        _WS_BUCKETS[key] = bucket
        return False, retry_after
    bucket.append(now)
    _WS_BUCKETS[key] = bucket
    return True, 0


def _ws_client_key(websocket: WebSocket, suffix: str) -> str:
    client_host = websocket.client.host if websocket.client else "unknown"
    return f"{client_host}:{suffix}"


def _ws_principal_key(websocket: WebSocket, auth_context: object | None, suffix: str) -> str:
    for attr in ("session_id", "user_id", "workspace_slug"):
        value = str(getattr(auth_context, attr, "") or "").strip()
        if value:
            return f"{attr}:{value}:{suffix}"
    return _ws_client_key(websocket, suffix)


async def _receive_command(websocket: WebSocket) -> UICommand:
    raw = await websocket.receive_text()
    if len(raw.encode("utf-8")) > _WS_MAX_TEXT_BYTES:
        raise ValueError("WebSocket frame exceeds the dashboard size limit.")
    return UICommand.model_validate(json.loads(raw))


async def _reject_ws_command(runtime: "KernRuntime", reason: str, broadcast_reason: str) -> None:
    _set_redacted_error(runtime, reason)
    runtime.orchestrator.mark_dirty("runtime")
    await runtime._refresh_platform_snapshot()
    await runtime.broadcast_if_changed(force=True, reason=broadcast_reason)


def _set_redacted_error(runtime: "KernRuntime", message: str) -> None:
    runtime.orchestrator.snapshot.last_action = message
    runtime.orchestrator.snapshot.response_text = redact_error_detail()["detail"]


def _record_scheduler_domain_event(runtime: "KernRuntime", auth_context, *, event_type: str, detail: str, metadata: dict[str, object] | None = None) -> None:
    if not getattr(runtime, "memory", None):
        return
    payload = {
        "workflow_type": "scheduling_follow_through",
        "event_type": event_type,
        "detail": detail,
        "metadata": metadata or {},
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    workflow_id = hashlib.sha1(f"workflow|{runtime.active_profile.slug}|scheduler".encode("utf-8")).hexdigest()
    runtime.memory.record_workflow_domain_event(
        WorkflowDomainEvent(
            id=f"wde-{workflow_id}-{fingerprint[:16]}",
            profile_slug=runtime.active_profile.slug,
            organization_id=getattr(auth_context, "organization_id", None),
            workspace_slug=runtime.active_profile.slug,
            actor_user_id=getattr(auth_context, "user_id", None),
            workflow_id=workflow_id,
            workflow_type="scheduling_follow_through",
            event_type=event_type,
            detail=detail,
            fingerprint=fingerprint,
            metadata=metadata or {},
        )
    )


def _is_websocket_transport_error(exc: Exception) -> bool:
    if isinstance(exc, WebSocketDisconnect):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc)
    return (
        "WebSocket is not connected" in message
        or "Need to call \"accept\" first" in message
        or "close message has been sent" in message
    )


async def _safe_send_json(websocket: WebSocket, payload: dict) -> bool:
    try:
        await websocket.send_json(payload)
        return True
    except Exception as exc:  # pragma: no cover - transport-specific behavior
        if _is_websocket_transport_error(exc):
            return False
        raise


async def _policy_gate_dashboard_action(
    runtime: "KernRuntime",
    capability_name: str,
    *,
    arguments: dict[str, object] | None = None,
    descriptor: CapabilityDescriptor | None = None,
    summary: str | None = None,
) -> bool:
    resolved_descriptor = descriptor or runtime.orchestrator.capabilities.get_descriptor(capability_name)
    step = PlanStep(
        capability_name=capability_name,
        arguments=arguments or {},
        reason=f"Dashboard action: {capability_name}",
        title=summary or capability_name.replace("_", " "),
    )
    decision = runtime.policy.decide_step(step, descriptor=resolved_descriptor)
    await runtime.event_hub.publish({"type": "policy", "payload": decision.model_dump(mode="json")})
    if decision.verdict == "allow":
        return True
    runtime.platform.record_audit(
        "policy",
        f"dashboard_{capability_name}",
        "failure" if decision.verdict == "deny" else "warning",
        decision.message,
        profile_slug=runtime.active_profile.slug,
        details={"verdict": decision.verdict, "reason": decision.policy_reason},
    )
    if decision.verdict == "confirm":
        plan = ExecutionPlan(
            summary=summary or resolved_descriptor.title if resolved_descriptor else summary or capability_name.replace("_", " "),
            steps=[step],
            source="dashboard",
        )
        prompt = f"{decision.message} Approve action: {plan.summary or step.title or step.capability_name}?"
        runtime.orchestrator.set_pending_confirmation(
            plan,
            prompt=prompt,
            original_utterance=summary or capability_name.replace("_", " "),
            trigger_source="manual_ui",
        )
        runtime.orchestrator.snapshot.pending_confirmation = PendingConfirmation(step=step, prompt=prompt)
        runtime.orchestrator.snapshot.response_text = prompt
        runtime.orchestrator.snapshot.last_action = "Waiting for confirmation."
        runtime.orchestrator.snapshot.assistant_state = "muted" if runtime.local_data.muted() else "idle"
        runtime.orchestrator.append_turn("system", prompt, kind="confirmation", status="pending")
        runtime.orchestrator.mark_dirty("runtime")
        await runtime.broadcast_if_changed(force=True, reason="dashboard_policy_confirm")
        return False
    reply = f"I cannot do that. {decision.message}"
    runtime.orchestrator.snapshot.response_text = reply
    runtime.orchestrator.snapshot.last_action = "Denied by policy."
    runtime.orchestrator.snapshot.assistant_state = "muted" if runtime.local_data.muted() else "idle"
    runtime.orchestrator.append_turn("assistant", reply, kind="tool_status", status="failed")
    runtime.orchestrator.mark_dirty("runtime")
    await runtime.broadcast_if_changed(force=True, reason="dashboard_policy_deny")
    return False


async def websocket_endpoint(websocket: WebSocket, runtime: KernRuntime, *, auth_checked: bool = False) -> None:
    if not auth_checked:
        ensure_websocket_allowed(websocket)
    auth_context = getattr(websocket.state, "auth_context", None)
    allowed, retry_after = _consume_ws_budget(_ws_client_key(websocket, "connect"), limit=10, window_seconds=60)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Too many connection attempts. Retry in {retry_after}s.")
    await websocket.accept()
    ws_request_id = generate_request_id()
    token = request_id_var.set(ws_request_id)
    if not hasattr(runtime, '_ws_connection_count'):
        runtime._ws_connection_count = 0
    runtime._ws_connection_count += 1
    forward_task = None
    try:
        await runtime._refresh_platform_snapshot()
        if not await _safe_send_json(websocket, {"type": "snapshot", "payload": runtime.orchestrator.snapshot.model_dump(mode="json")}):
            return

        async def forward_events() -> None:
            async for event in runtime.event_hub.subscribe():
                if not await _safe_send_json(websocket, event):
                    return

        forward_task = asyncio.create_task(forward_events())
        while True:
            try:
                command = await _receive_command(websocket)
            except ValueError:
                await _reject_ws_command(runtime, "WebSocket command exceeded the dashboard size limit.", "ws_frame_rejected")
                continue
            except json.JSONDecodeError:
                await _reject_ws_command(runtime, "WebSocket command was not valid JSON.", "ws_json_rejected")
                continue
            ok, retry_after = _consume_ws_budget(
                _ws_principal_key(websocket, auth_context, "commands"),
                limit=120,
                window_seconds=60,
            )
            if not ok:
                runtime.orchestrator.snapshot.last_action = f"Command rate limit exceeded. Try again in {retry_after} seconds."
                runtime.orchestrator.snapshot.response_text = "Too many dashboard commands. Please wait and try again."
                runtime.orchestrator.mark_dirty("runtime")
                if not await _safe_send_json(
                    websocket,
                    {
                        "type": "rate_limit",
                        "scope": "dashboard_commands",
                        "message": f"Command rate limit exceeded. Retry in {retry_after} seconds.",
                        "retry_after_seconds": retry_after,
                    },
                ):
                    return
                await runtime._refresh_platform_snapshot()
                await runtime.broadcast_if_changed(force=True, reason="ws_rate_limited")
                continue
            if command.type in _EXPENSIVE_COMMANDS:
                ok, retry_after = _consume_ws_budget(
                    _ws_principal_key(websocket, auth_context, f"expensive:{command.type}"),
                    limit=8,
                    window_seconds=60,
                )
                if not ok:
                    runtime.orchestrator.snapshot.last_action = f"Expensive command rate limit exceeded. Try again in {retry_after} seconds."
                    runtime.orchestrator.snapshot.response_text = "That workspace action is temporarily rate limited."
                    runtime.orchestrator.mark_dirty("runtime")
                    if not await _safe_send_json(
                        websocket,
                        {
                            "type": "rate_limit",
                            "scope": f"expensive:{command.type}",
                            "message": f"Workspace action is temporarily rate limited. Retry in {retry_after} seconds.",
                            "retry_after_seconds": retry_after,
                        },
                    ):
                        return
                    await runtime._refresh_platform_snapshot()
                    await runtime.broadcast_if_changed(force=True, reason="ws_expensive_rate_limited")
                    continue
            required_roles = _COMMAND_ROLE_REQUIREMENTS.get(command.type, _UNLISTED_COMMAND_ROLES)
            if required_roles:
                current_roles = set(getattr(auth_context, "roles", []) or [])
                if not getattr(auth_context, "is_break_glass", False) and required_roles.isdisjoint(current_roles):
                    runtime.orchestrator.snapshot.last_action = "You are not allowed to run that workspace action."
                    runtime.orchestrator.snapshot.response_text = "Permission denied."
                    runtime.orchestrator.mark_dirty("runtime")
                    await runtime._refresh_platform_snapshot()
                    await runtime.broadcast_if_changed(force=True, reason="workspace_role_denied")
                    continue
            locked_commands = {
                "submit_text",
                "confirm_action",
                "cancel_action",
                "lock_profile",
                "unlock_profile",
                "set_profile_pin",
                "create_backup",
                "restore_backup",
                "export_audit",
                "ingest_files",
                "search_knowledge",
                "review_action_item",
                "reminder_action",
                "create_schedule",
                "delete_schedule",
                "toggle_schedule",
                "retry_failed_task",
            }
            if command.type in locked_commands and not runtime.profile_session.unlocked:
                runtime.orchestrator.snapshot.last_action = "Active profile is locked."
                runtime.orchestrator.snapshot.response_text = "Unlock the active KERN profile before continuing."
                runtime.orchestrator.mark_dirty("runtime")
                await runtime._refresh_platform_snapshot()
                await runtime.broadcast_if_changed(force=True, reason="profile_locked")
                continue
            production_commands = {
                "create_backup",
                "restore_backup",
                "ingest_files",
                "export_audit",
                "create_schedule",
                "delete_schedule",
                "toggle_schedule",
                "retry_failed_task",
            }
            if command.type in production_commands and not runtime.ensure_production_access(blocked_scope=command.type):
                runtime.orchestrator.snapshot.last_action = "Production access is blocked by license state."
                runtime.orchestrator.snapshot.response_text = "Import a valid offline license before continuing."
                runtime.orchestrator.mark_dirty("runtime")
                await runtime._refresh_platform_snapshot()
                await runtime.broadcast_if_changed(force=True, reason="license_block")
                continue
            if command.type == "submit_text" and command.text:
                if len(command.text.encode("utf-8")) > _WS_MAX_TEXT_BYTES:
                    _set_redacted_error(runtime, "Submitted text exceeded the dashboard size limit.")
                    runtime.orchestrator.mark_dirty("runtime")
                    await runtime.broadcast_if_changed(force=True, reason="submit_text_rejected")
                    continue
                if not runtime.orchestrator.snapshot.action_in_progress:
                    try:
                        await runtime.orchestrator.process_transcript(
                            command.text,
                            trigger="manual_ui",
                            auth_context=auth_context,
                        )
                    except Exception as exc:
                        logger.exception("Dashboard transcript processing failed.")
                        runtime.platform.record_audit(
                            "runtime",
                            "transcript_processing_error",
                            "failure",
                            f"Dashboard transcript processing failed: {exc}",
                            profile_slug=runtime.active_profile.slug,
                            details={"exception": type(exc).__name__},
                        )
                        _set_redacted_error(runtime, "The request could not be completed.")
                        runtime.orchestrator.snapshot.assistant_state = (
                            "muted" if runtime.local_data.muted() else "idle"
                        )
                        runtime.orchestrator.mark_dirty("runtime")
                        await runtime.broadcast_if_changed(force=True, reason="transcript_error")
            elif command.type == "confirm_action":
                await runtime.orchestrator.confirm_pending(True)
            elif command.type == "cancel_action":
                await runtime.orchestrator.confirm_pending(False)
            elif command.type == "update_settings":
                if "local_mode_enabled" in command.settings:
                    runtime.brain.set_local_mode(bool(command.settings["local_mode_enabled"]))
                runtime.refresh_interaction_snapshot()
                await runtime._refresh_platform_snapshot()
                await runtime.broadcast_if_changed(force=True, reason="settings_update")
            elif command.type == "rerun_readiness":
                runtime.orchestrator.snapshot.last_action = "Readiness checks reran."
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="readiness_rerun")
            elif command.type == "rerun_license_check":
                runtime.orchestrator.snapshot.last_action = "Offline license state refreshed."
                runtime.clear_failure("license_required", "license_expired", "license_invalid")
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="license_rerun")
            elif command.type == "start_sample_workspace":
                await asyncio.to_thread(runtime.start_sample_workspace)
                await runtime._refresh_platform_snapshot()
                await runtime.broadcast_if_changed(force=True, reason="sample_workspace_start")
            elif command.type == "start_real_workspace":
                await asyncio.to_thread(runtime.start_real_workspace)
                await runtime._refresh_platform_snapshot()
                await runtime.broadcast_if_changed(force=True, reason="sample_workspace_exit")
            elif command.type == "retry_failure_action":
                retry_action = str(command.settings.get("retry_action", "") or "").strip()
                failure_id = str(command.settings.get("failure_id", "") or "").strip()
                if retry_action == "rerun_readiness":
                    if failure_id:
                        runtime.clear_failure(failure_id)
                    runtime.orchestrator.snapshot.last_action = "Retrying after a fresh readiness check."
                    await runtime._refresh_platform_snapshot()
                    runtime.orchestrator.mark_dirty("runtime")
                    await runtime.broadcast_if_changed(force=True, reason="failure_retry")
                elif retry_action == "rerun_license_check":
                    if failure_id:
                        runtime.clear_failure(failure_id)
                    runtime.orchestrator.snapshot.last_action = "Retrying after a fresh license check."
                    await runtime._refresh_platform_snapshot()
                    runtime.orchestrator.mark_dirty("runtime")
                    await runtime.broadcast_if_changed(force=True, reason="failure_retry")
            elif command.type == "toggle_runtime_mute":
                muted = bool(command.settings.get("muted", False))
                runtime.local_data.set_muted(muted)
                runtime.orchestrator.snapshot.runtime_muted = muted
                runtime.orchestrator.snapshot.assistant_state = "muted" if muted else "idle"
                runtime.orchestrator.snapshot.last_action = "Runtime muted." if muted else "Runtime unmuted."
                runtime.orchestrator.add_history("system", runtime.orchestrator.snapshot.last_action)
                runtime.refresh_interaction_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="mute_toggle")
            elif command.type == "reset_conversation":
                runtime.orchestrator.reset_conversation()
                runtime.refresh_interaction_snapshot()
                await runtime.broadcast_if_changed(force=True, reason="conversation_reset")
            elif command.type == "lock_profile":
                if not runtime.active_profile.has_pin:
                    runtime.orchestrator.snapshot.last_action = "Set a profile PIN before locking the active profile."
                    runtime.orchestrator.snapshot.response_text = "Locking without a configured PIN is disabled."
                    runtime.orchestrator.mark_dirty("runtime")
                    await runtime.broadcast_if_changed(force=True, reason="profile_lock_rejected")
                    continue
                runtime.profile_session = await asyncio.to_thread(
                    runtime.lock_active_profile,
                    "Locked from the KERN dashboard.",
                )
                runtime.orchestrator.snapshot.assistant_state = "muted"
                runtime.orchestrator.snapshot.last_action = "Active profile locked."
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="profile_locked")
            elif command.type == "unlock_profile":
                pin = str(command.settings.get("pin", "") or "")
                allowed, retry_after = _consume_ws_budget(_ws_client_key(websocket, "unlock"), limit=6, window_seconds=300)
                if not allowed:
                    runtime.orchestrator.snapshot.last_action = f"Too many unlock attempts. Retry in {retry_after}s."
                    runtime.orchestrator.snapshot.response_text = "Unlock temporarily blocked."
                    runtime.orchestrator.mark_dirty("runtime")
                    await runtime.broadcast_if_changed(force=True, reason="profile_unlock_limited")
                    continue
                if not pin.strip():
                    runtime.orchestrator.snapshot.last_action = "A profile PIN is required to unlock the active profile."
                    runtime.orchestrator.snapshot.response_text = "Unlock rejected."
                    runtime.orchestrator.mark_dirty("runtime")
                    await runtime.broadcast_if_changed(force=True, reason="profile_unlock_rejected")
                    continue
                runtime.profile_session = await asyncio.to_thread(
                    runtime.unlock_active_profile,
                    pin,
                )
                if runtime.profile_session.unlocked:
                    await runtime.resume_unlocked_profile_runtime()
                runtime.orchestrator.snapshot.assistant_state = "idle" if runtime.profile_session.unlocked else "muted"
                runtime.orchestrator.snapshot.last_action = (
                    "Active profile unlocked." if runtime.profile_session.unlocked else runtime.profile_session.locked_reason or "Unlock failed."
                )
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="profile_unlock")
            elif command.type == "set_profile_pin":
                pin = str(command.settings.get("pin", "") or "").strip()
                if not runtime.profile_session.unlocked:
                    runtime.orchestrator.snapshot.last_action = "Unlock the active profile before changing its PIN."
                    runtime.orchestrator.snapshot.response_text = "PIN update rejected."
                    runtime.orchestrator.mark_dirty("runtime")
                    await runtime.broadcast_if_changed(force=True, reason="profile_pin_rejected")
                    continue
                await asyncio.to_thread(runtime.platform.set_profile_pin, runtime.active_profile.slug, pin or None)
                runtime.profile_session = await asyncio.to_thread(
                    runtime.unlock_active_profile,
                    pin or None,
                )
                if runtime.profile_session.unlocked:
                    await runtime.resume_unlocked_profile_runtime()
                runtime.orchestrator.snapshot.last_action = "Profile PIN updated." if pin else "Profile PIN cleared."
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="profile_pin_updated")
            elif command.type == "create_backup":
                password = str(command.settings.get("password", "") or "")
                target_path = str(command.settings.get("target_path") or runtime.active_profile.backups_root)
                target_kind = str(command.settings.get("target_kind") or "local_folder")
                target_label = str(command.settings.get("label") or "Manual backup")
                allowed = await _policy_gate_dashboard_action(
                    runtime,
                    "create_backup",
                    arguments={"password": password, "label": target_label},
                    summary="Create encrypted backup",
                )
                if not allowed:
                    continue
                job = runtime.platform.create_job(
                    "profile_backup",
                    "Create encrypted backup",
                    profile_slug=runtime.active_profile.slug,
                    detail="Preparing encrypted backup.",
                    payload={"target_path": target_path, "target_kind": target_kind},
                )
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.snapshot.last_action = "Creating encrypted backup."
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="backup_started")
                try:
                    target = BackupTarget(kind=target_kind, path=target_path, label=target_label, writable=True)
                    runtime.platform.upsert_backup_target(runtime.active_profile.slug, target.kind, target.path, target.label, True)
                    runtime.platform.update_checkpoint(
                        job.id,
                        "planned",
                        {"target_path": target.path, "target_kind": target.kind, "label": target.label},
                    )
                    runtime.platform.update_job(
                        job.id,
                        status="running",
                        detail="Encrypting profile data.",
                        progress=0.2,
                        checkpoint_stage="encrypting",
                    )
                    backup_path = await asyncio.to_thread(
                        runtime.backup_service.create_encrypted_profile_backup,
                        runtime.active_profile,
                        target,
                        password,
                        platform_store=runtime.platform,
                    )
                    runtime.platform.update_checkpoint(job.id, "written", {"path": str(backup_path)})
                    runtime.platform.update_job(
                        job.id,
                        status="completed",
                        detail=f"Backup written to {backup_path.name}.",
                        progress=1.0,
                        result={"path": str(backup_path)},
                        checkpoint_stage="written",
                        recoverable=False,
                    )
                    runtime.platform.record_audit(
                        "backup",
                        "profile_backup",
                        "success",
                        f"Encrypted profile backup created at {backup_path.name}.",
                        profile_slug=runtime.active_profile.slug,
                    )
                    runtime.clear_failure("backup_failed")
                    runtime.orchestrator.snapshot.last_action = f"Encrypted backup created at {backup_path.name}."
                except Exception as exc:
                    runtime.record_failure(
                        error_code="backup_failed",
                        title="Encrypted backup failed",
                        message="KERN could not create the encrypted backup.",
                        blocked_scope="backup",
                        next_action="Confirm the backup location and password, then retry the backup.",
                        retry_available=True,
                        retry_action="rerun_readiness",
                        technical_detail=str(exc),
                        source="backup",
                    )
                    runtime.platform.update_job(
                        job.id,
                        status="failed",
                        detail=str(exc),
                        result={"error": str(exc)},
                        checkpoint_stage="failed",
                        recoverable=False,
                        error_code="backup_failed",
                        error_message=str(exc),
                    )
                    runtime.platform.record_audit(
                        "backup",
                        "profile_backup",
                        "failure",
                        f"Encrypted backup failed: {exc}",
                        profile_slug=runtime.active_profile.slug,
                    )
                    runtime.orchestrator.snapshot.last_action = f"Backup failed: {exc}"
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="backup_complete")
            elif command.type == "restore_backup":
                backup_path = str(command.settings.get("backup_path", "") or "")
                password = str(command.settings.get("password", "") or "")
                restore_root = str(command.settings.get("restore_root") or (Path(runtime.active_profile.backups_root) / "restore-preview"))
                allowed = await _policy_gate_dashboard_action(
                    runtime,
                    "restore_backup",
                    arguments={"backup_path": backup_path, "restore_root": restore_root, "password": password},
                    summary="Restore encrypted backup",
                )
                if not allowed:
                    continue
                job = runtime.platform.create_job(
                    "restore_backup",
                    "Restore encrypted backup",
                    profile_slug=runtime.active_profile.slug,
                    detail="Validating encrypted backup.",
                    payload={"backup_path": backup_path, "restore_root": restore_root},
                )
                runtime.orchestrator.snapshot.last_action = "Restoring encrypted backup."
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="backup_restore_started")
                try:
                    validation = await asyncio.to_thread(
                        runtime.backup_service.validate_backup,
                        backup_path,
                        password,
                    )
                    if not validation.valid:
                        raise RuntimeError("; ".join(validation.errors) or "Backup validation failed.")
                    plan = await asyncio.to_thread(
                        runtime.backup_service.prepare_restore,
                        backup_path,
                        password,
                        restore_root,
                    )
                    rollback_root = runtime.backup_service.rollback_root_for_plan(plan)
                    runtime.platform.update_checkpoint(
                        job.id,
                        "planned",
                        {
                            "staged_root": plan.staged_root,
                            "final_root": plan.final_root,
                            "requested_root": plan.requested_root,
                            "rollback_root": str(rollback_root),
                        },
                    )
                    runtime.platform.update_checkpoint(
                        job.id,
                        "validated",
                        {"entries": validation.entry_count, "profile_slug": validation.profile_slug},
                    )
                    runtime.platform.update_job(
                        job.id,
                        status="running",
                        detail="Restoring validated backup.",
                        progress=0.4,
                        checkpoint_stage="validated",
                        recoverable=True,
                    )
                    restored = await asyncio.to_thread(
                        runtime.backup_service.execute_restore_plan,
                        plan,
                        password,
                    )
                    runtime.platform.update_checkpoint(job.id, "restored", {"path": str(restored)})
                    runtime.platform.update_job(
                        job.id,
                        status="completed",
                        detail=f"Backup restored to {restored}.",
                        progress=1.0,
                        result={"path": str(restored)},
                        checkpoint_stage="restored",
                        recoverable=False,
                    )
                    runtime.platform.record_audit(
                        "backup",
                        "restore_backup",
                        "success",
                        f"Backup restored to {restored}.",
                        profile_slug=runtime.active_profile.slug,
                    )
                    runtime.clear_failure("backup_restore_failed")
                    runtime.orchestrator.snapshot.last_action = f"Backup restored to {restored}."
                except Exception as exc:
                    runtime.record_failure(
                        error_code="backup_restore_failed",
                        title="Backup restore failed",
                        message="KERN could not restore the encrypted backup.",
                        blocked_scope="restore",
                        next_action="Validate the backup password and restore target, then retry the restore.",
                        retry_available=True,
                        retry_action="rerun_readiness",
                        technical_detail=str(exc),
                        source="restore",
                    )
                    runtime.platform.update_job(
                        job.id,
                        status="failed",
                        detail=str(exc),
                        result={"error": str(exc)},
                        checkpoint_stage="failed",
                        recoverable=False,
                        error_code="backup_restore_failed",
                        error_message=str(exc),
                    )
                    runtime.platform.record_audit(
                        "backup",
                        "restore_backup",
                        "failure",
                        str(exc),
                        profile_slug=runtime.active_profile.slug,
                    )
                    runtime.orchestrator.snapshot.last_action = f"Backup restore failed: {exc}"
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="backup_restore_finished")
            elif command.type == "search_knowledge":
                query = str(command.settings.get("query", "") or "").strip()
                try:
                    hits = runtime.retrieval_service.retrieve(query, scope=runtime.orchestrator.snapshot.memory_scope, limit=8)
                    if (
                        settings.policy_mode == "corporate"
                        and settings.policy_restrict_sensitive_reads
                    ):
                        filtered_hits = [
                            hit for hit in hits
                            if not runtime.policy.is_sensitive_classification(str(hit.metadata.get("classification") or ""))
                        ]
                        if hasattr(runtime.retrieval_service, "replace_last_hits"):
                            runtime.retrieval_service.replace_last_hits(filtered_hits)
                        else:  # pragma: no cover - compatibility with older retrieval service shapes
                            runtime.retrieval_service._last_hits = filtered_hits
                        if len(filtered_hits) != len(hits):
                            runtime.orchestrator.snapshot.last_action = "Sensitive knowledge hits are restricted in corporate mode."
                    else:
                        runtime.orchestrator.snapshot.last_action = (
                            f"Searched knowledge for '{query}'." if query else "Cleared knowledge search."
                        )
                    await runtime._refresh_platform_snapshot()
                    runtime.orchestrator.mark_dirty("context", "runtime")
                    await runtime.broadcast_if_changed(force=True, reason="knowledge_search")
                except Exception as exc:
                    logger.exception("Knowledge search failed.")
                    runtime.platform.record_audit(
                        "runtime",
                        "knowledge_search_error",
                        "failure",
                        f"Knowledge search failed: {exc}",
                        profile_slug=runtime.active_profile.slug,
                        details={"exception": type(exc).__name__},
                    )
                    _set_redacted_error(runtime, "Knowledge search could not be completed.")
                    runtime.orchestrator.mark_dirty("runtime")
                    await runtime.broadcast_if_changed(force=True, reason="knowledge_search_error")
            elif command.type == "reminder_action":
                reminder_id = int(command.settings.get("reminder_id", 0))
                action = str(command.settings.get("action", "")).strip()
                if reminder_id > 0:
                    if action == "snooze":
                        runtime.reminder_service.snooze(reminder_id, minutes=int(command.settings.get("minutes", 10)))
                        runtime.orchestrator.snapshot.last_action = f"Snoozed reminder #{reminder_id}."
                    elif action == "dismiss":
                        runtime.reminder_service.dismiss(reminder_id)
                        runtime.orchestrator.snapshot.last_action = f"Dismissed reminder #{reminder_id}."
                    runtime.orchestrator.add_history("reminder", runtime.orchestrator.snapshot.last_action)
                    runtime.refresh_interaction_snapshot()
                    runtime.orchestrator.mark_dirty("context", "runtime")
                    await runtime.broadcast_if_changed(force=True, reason="reminder_action")
            elif command.type == "export_audit":
                allowed = await _policy_gate_dashboard_action(
                    runtime,
                    "export_audit_trail",
                    summary="Export audit trail",
                )
                if not allowed:
                    continue
                export_json = await asyncio.to_thread(
                    runtime.platform.export_audit_trail,
                    runtime.active_profile.slug,
                )
                if not await _safe_send_json(websocket, {"type": "audit_export", "payload": export_json}):
                    return
                runtime.platform.record_audit(
                    "audit",
                    "export_audit_trail",
                    "warning",
                    "Audit trail exported from dashboard.",
                    profile_slug=runtime.active_profile.slug,
                )
            elif command.type == "ingest_files":
                profile = runtime.active_profile
                try:
                    file_paths = [
                        validate_user_import_path(item, profile)
                        for item in list(command.settings.get("file_paths", []))
                    ]
                    folder_path_raw = str(command.settings.get("folder_path", "") or "")
                    folder_path = validate_user_import_path(folder_path_raw, profile) if folder_path_raw else None
                except ValueError as exc:
                    runtime.orchestrator.snapshot.last_action = f"Bulk ingestion rejected: {exc}"
                    runtime.orchestrator.snapshot.response_text = "The selected path is outside approved workspace storage."
                    runtime.orchestrator.mark_dirty("runtime")
                    await runtime._refresh_platform_snapshot()
                    await runtime.broadcast_if_changed(force=True, reason="ingest_path_rejected")
                    continue
                category = str(command.settings.get("category", "") or "")
                tags = list(command.settings.get("tags", []))
                allowed = await _policy_gate_dashboard_action(
                    runtime,
                    "bulk_ingest",
                    arguments={
                        "file_paths": [str(path) for path in file_paths],
                        "folder_path": str(folder_path or ""),
                        "category": category,
                        "tags": tags,
                    },
                    summary="Bulk ingest documents",
                )
                if not allowed:
                    continue
                if folder_path is not None and hasattr(runtime.document_service, "ingest_folder"):
                    await asyncio.to_thread(
                        runtime.document_service.ingest_folder,
                        folder_path,
                        recursive=bool(command.settings.get("recursive", True)),
                        source="dashboard",
                        category=category or None,
                        tags=tags,
                    )
                    runtime.orchestrator.snapshot.last_action = f"Bulk ingestion started for folder: {folder_path}"
                elif file_paths and hasattr(runtime.document_service, "ingest_batch"):
                    await asyncio.to_thread(
                        runtime.document_service.ingest_batch,
                        file_paths,
                        source="dashboard",
                        category=category or None,
                        tags=tags,
                    )
                    runtime.orchestrator.snapshot.last_action = f"Batch ingestion started for {len(file_paths)} file(s)."
                else:
                    runtime.orchestrator.snapshot.last_action = "No files specified for ingestion."
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="ingest_files")
            elif command.type == "create_schedule":
                if hasattr(runtime, "scheduler_service") and runtime.scheduler_service:
                    allowed = await _policy_gate_dashboard_action(
                        runtime,
                        "create_schedule",
                        summary="Create scheduled task",
                    )
                    if not allowed:
                        continue
                    title = str(command.settings.get("title", "Scheduled task"))
                    cron_expr = str(command.settings.get("cron_expression", "0 9 * * *"))
                    action_type = str(command.settings.get("action_type", "custom_prompt")).strip()
                    action_payload = command.settings.get("action_payload", {})
                    max_retries = int(command.settings.get("max_retries", 2) or 2)
                    try:
                        if action_type not in _SCHEDULE_ACTION_TYPES:
                            raise ValueError("Unsupported scheduled action type.")
                        if not isinstance(action_payload, dict):
                            raise ValueError("Scheduled action payload must be an object.")
                        runtime.scheduler_service.validate_cron_expression(cron_expr)
                        runtime.scheduler_service.create_task(
                            title=title,
                            cron_expression=cron_expr,
                            action_type=action_type,
                            action_payload=action_payload,
                            profile_slug=runtime.active_profile.slug,
                            max_retries=max_retries,
                        )
                        runtime.orchestrator.snapshot.last_action = f"Schedule created: {title}"
                        _record_scheduler_domain_event(
                            runtime,
                            auth_context,
                            event_type="task_created",
                            detail=f"Created schedule {title}.",
                            metadata={"title": title, "cron_expression": cron_expr, "action_type": action_type},
                        )
                        runtime.platform.record_audit(
                            "scheduler",
                            "create_schedule",
                            "success",
                            f"Created schedule: {title}",
                            profile_slug=runtime.active_profile.slug,
                        )
                    except Exception as exc:
                        logger.warning("Schedule creation failed: %s", exc, exc_info=True)
                        _set_redacted_error(runtime, "Schedule creation failed.")
                        runtime.platform.record_audit(
                            "scheduler",
                            "create_schedule",
                            "failure",
                            str(exc),
                            profile_slug=runtime.active_profile.slug,
                        )
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="schedule_created")
            elif command.type == "delete_schedule":
                if hasattr(runtime, "scheduler_service") and runtime.scheduler_service:
                    schedule_id = str(command.settings.get("schedule_id", ""))
                    runtime.scheduler_service.delete_schedule(schedule_id)
                    runtime.orchestrator.snapshot.last_action = "Schedule deleted."
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="schedule_deleted")
            elif command.type == "toggle_schedule":
                if hasattr(runtime, "scheduler_service") and runtime.scheduler_service:
                    schedule_id = str(command.settings.get("schedule_id", ""))
                    enabled = bool(command.settings.get("enabled", True))
                    runtime.scheduler_service.toggle_schedule(schedule_id, enabled)
                    runtime.orchestrator.snapshot.last_action = f"Schedule {'enabled' if enabled else 'disabled'}."
                    _record_scheduler_domain_event(
                        runtime,
                        auth_context,
                        event_type="task_toggled",
                        detail=f"Schedule {schedule_id} was {'enabled' if enabled else 'disabled'}.",
                        metadata={"schedule_id": schedule_id, "enabled": enabled},
                    )
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="schedule_toggled")
            elif command.type == "retry_failed_task":
                if hasattr(runtime, "scheduler_service") and runtime.scheduler_service:
                    schedule_id = str(command.settings.get("schedule_id", ""))
                    runtime.scheduler_service.retry_failed_task(schedule_id)
                    runtime.orchestrator.snapshot.last_action = "Failed task retried."
                    _record_scheduler_domain_event(
                        runtime,
                        auth_context,
                        event_type="task_retried",
                        detail=f"Retried failed schedule {schedule_id}.",
                        metadata={"schedule_id": schedule_id},
                    )
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="schedule_retried")
            elif command.type == "search_memory_history":
                query = str(command.settings.get("query", "")).strip()
                date_from = str(command.settings.get("date_from", "") or "").strip() or None
                date_to = str(command.settings.get("date_to", "") or "").strip() or None
                if query and hasattr(runtime, "memory") and runtime.memory:
                    hits = runtime.memory.search_conversation_history(query, date_from=date_from, date_to=date_to, limit=30)
                    runtime.orchestrator.snapshot.memory_timeline = runtime.memory.build_topic_timeline(query, limit=40)
                    await runtime.broadcast_if_changed(force=True, reason="memory_search")
                    if not await _safe_send_json(websocket, {"type": "memory_search_result", "hits": hits, "query": query}):
                        return
            elif command.type == "dismiss_all_alerts":
                if hasattr(runtime, "_pending_proactive_alerts"):
                    for alert in list(runtime._pending_proactive_alerts):
                        runtime.action_planner.record_feedback(runtime.local_data, alert, "dismissed")
                    runtime._pending_proactive_alerts.clear()
                if hasattr(runtime.orchestrator.snapshot, "proactive_alerts"):
                    runtime.orchestrator.snapshot.proactive_alerts = []
                runtime.platform.record_audit(
                    "attention",
                    "dismiss_all_alerts",
                    "success",
                    "Dismissed all proactive alerts from dashboard.",
                    profile_slug=runtime.active_profile.slug,
                )
                await runtime.broadcast_if_changed(force=True, reason="alerts_dismissed")
            elif command.type == "dismiss_alert":
                alert_index = int(command.settings.get("alert_index", -1))
                if 0 <= alert_index < len(getattr(runtime, "_pending_proactive_alerts", [])):
                    dismissed = runtime._pending_proactive_alerts.pop(alert_index)
                    runtime.action_planner.record_feedback(runtime.local_data, dismissed, "dismissed")
                    runtime.orchestrator.snapshot.proactive_alerts = list(runtime._pending_proactive_alerts[-20:])
                    runtime.platform.record_audit(
                        "attention",
                        "dismiss_alert",
                        "success",
                        f"Dismissed proactive alert: {dismissed.get('title', 'alert')}.",
                        profile_slug=runtime.active_profile.slug,
                    )
                await runtime.broadcast_if_changed(force=True, reason="alert_dismissed")
            elif command.type == "execute_suggested_action":
                action_type = str(command.settings.get("action_type", ""))
                action_payload = command.settings.get("action_payload", {})
                alert_index = int(command.settings.get("alert_index", -1))
                if action_type and hasattr(runtime, "action_planner") and runtime.action_planner:
                    try:
                        result = await runtime.action_planner.execute_action(
                            action_type, action_payload, runtime.orchestrator
                        )
                        alert = None
                        if result.get("success", False) and 0 <= alert_index < len(getattr(runtime, "_pending_proactive_alerts", [])):
                            alert = runtime._pending_proactive_alerts.pop(alert_index)
                            runtime.orchestrator.snapshot.proactive_alerts = list(runtime._pending_proactive_alerts[-20:])
                        elif 0 <= alert_index < len(getattr(runtime, "_pending_proactive_alerts", [])):
                            alert = runtime._pending_proactive_alerts[alert_index]
                        if result.get("success", False):
                            runtime.action_planner.record_feedback(runtime.local_data, alert, "accepted", action_type=action_type)
                            if action_type in {"create_reminder", "create_task"}:
                                runtime.action_planner.record_feedback(runtime.local_data, alert, "executed_later", action_type=action_type)
                        runtime.orchestrator.snapshot.last_action = result.get("message", "Action executed.")
                        runtime.platform.record_audit(
                            "attention",
                            "execute_suggested_action",
                            "success" if result.get("success", False) else "failure",
                            runtime.orchestrator.snapshot.last_action,
                            profile_slug=runtime.active_profile.slug,
                            details={"action_type": action_type},
                        )
                    except Exception as exc:
                        runtime.orchestrator.snapshot.last_action = f"Action failed: {exc}"
                        runtime.platform.record_audit(
                            "attention",
                            "execute_suggested_action",
                            "failure",
                            str(exc),
                            profile_slug=runtime.active_profile.slug,
                            details={"action_type": action_type},
                        )
                await runtime._refresh_platform_snapshot()
                runtime.orchestrator.mark_dirty("runtime")
                await runtime.broadcast_if_changed(force=True, reason="suggested_action")
            elif command.type == "get_knowledge_graph":
                if hasattr(runtime, "knowledge_graph_service") and runtime.knowledge_graph_service:
                    graph = runtime.knowledge_graph_service.graph_snapshot()
                    if not await _safe_send_json(websocket, {"type": "knowledge_graph_data", "graph": graph}):
                        return
                else:
                    if not await _safe_send_json(websocket, {"type": "knowledge_graph_data", "graph": {"nodes": [], "links": []}}):
                        return
            elif command.type == "search_knowledge_graph":
                query = str(command.settings.get("query", "")).strip()
                if hasattr(runtime, "knowledge_graph_service") and runtime.knowledge_graph_service:
                    entities = runtime.knowledge_graph_service.search_entities(query, limit=20)
                    if not await _safe_send_json(websocket, {"type": "knowledge_graph_search", "entities": entities, "query": query}):
                        return
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        if _is_websocket_transport_error(exc):
            logger.debug("WebSocket transport closed during dashboard session: %s", exc)
            return
        if isinstance(exc, HTTPException):
            with contextlib.suppress(Exception):
                await websocket.close(code=1008, reason=str(exc.detail))
            return
        runtime.platform.record_audit(
            "runtime",
            "websocket_error",
            "failure",
            f"WebSocket session failed: {exc}",
            profile_slug=runtime.active_profile.slug,
            details={"exception": type(exc).__name__},
        )
        runtime.orchestrator.snapshot.last_action = "A runtime error interrupted the dashboard session."
        runtime.orchestrator.snapshot.response_text = redact_error_detail()["detail"]
        runtime.orchestrator.mark_dirty("runtime")
        await runtime._refresh_platform_snapshot()
        with contextlib.suppress(Exception):  # cleanup â€” best-effort
            await runtime.broadcast_if_changed(force=True, reason="websocket_error")
        with contextlib.suppress(Exception):  # cleanup â€” best-effort
            await websocket.close(code=1011)
    finally:
        runtime._ws_connection_count = max(0, getattr(runtime, '_ws_connection_count', 1) - 1)
        request_id_var.reset(token)
        if forward_task:
            forward_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await forward_task
