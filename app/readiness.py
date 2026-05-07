from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from app.config import settings
from app.platform import SYSTEM_SCHEMA_VERSION


def _open_readonly_sqlite(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def _schema_version(connection: sqlite3.Connection | None) -> int:
    if connection is None:
        return 0
    try:
        row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError, IndexError):
        return 0


def _profile_detection(system_connection: sqlite3.Connection | None) -> dict[str, object]:
    default_profile_root = settings.profile_root / "default"
    fallback_profile_db = default_profile_root / "kern.db"
    detection: dict[str, object] = {
        "profile_slug": "default",
        "profile_db_path": str(fallback_profile_db),
        "profile_db_exists": fallback_profile_db.exists(),
        "profile_schema_version": None,
        "profile_db_mode": "unknown",
        "profile_row_found": False,
    }
    if system_connection is None:
        return detection
    try:
        row = system_connection.execute(
            """
            SELECT slug, db_path, db_encryption_mode
            FROM profiles
            ORDER BY CASE WHEN slug = 'default' THEN 0 ELSE 1 END, id ASC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row:
        return detection
    detection["profile_row_found"] = True
    detection["profile_slug"] = str(row[0] or "default")
    profile_db_path = Path(str(row[1] or fallback_profile_db)).expanduser().resolve()
    detection["profile_db_path"] = str(profile_db_path)
    detection["profile_db_exists"] = profile_db_path.exists()
    detection["profile_db_mode"] = str(row[2] or "off")
    if detection["profile_db_exists"] and detection["profile_db_mode"] == "off":
        profile_conn = _open_readonly_sqlite(profile_db_path)
        try:
            detection["profile_schema_version"] = _schema_version(profile_conn)
        finally:
            if profile_conn is not None:
                profile_conn.close()
    elif detection["profile_db_exists"]:
        detection["profile_schema_version"] = "encrypted_profile_db"
    else:
        detection["profile_schema_version"] = "missing"
    return detection


def _task_registration() -> dict[str, object]:
    if os.name != "nt":
        return {"supported": False, "registered": False}
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", "KERN Local Runtime"],
            capture_output=True,
            text=True,
            check=False,
        )
        return {"supported": True, "registered": result.returncode == 0}
    except Exception:
        return {"supported": True, "registered": False}


def _runtime_probe(runtime_url: str | None) -> dict[str, object]:
    if not runtime_url:
        return {"checked": False, "reachable": False, "status_code": None}
    try:
        with urllib.request.urlopen(f"{runtime_url.rstrip('/')}/health", timeout=2.0) as response:
            return {"checked": True, "reachable": True, "status_code": response.status}
    except urllib.error.HTTPError as exc:
        return {"checked": True, "reachable": True, "status_code": exc.code}
    except Exception:
        return {"checked": True, "reachable": False, "status_code": None}


def _preferred_model_path() -> str | None:
    return (
        settings.llama_server_model_path
        or settings.deep_model_path
        or settings.fast_model_path
        or settings.cognition_model
    )


def _writable_path_state(path: Path) -> tuple[bool, str]:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        if os.access(resolved, os.W_OK):
            return True, f"{resolved} exists and is writable."
        return False, f"{resolved} exists but is not writable."
    parent = resolved.parent
    if parent.exists() and os.access(parent, os.W_OK):
        return True, f"{resolved} does not exist yet, but {parent} is writable."
    return False, f"{resolved} does not exist and {parent} is not writable."


def _disk_free_gb(path: Path) -> float | None:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.parent)
        return round(usage.free / 1024 / 1024 / 1024, 2)
    except Exception:
        return None


def build_readiness_report(runtime_url: str | None = None) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, object]] = []

    def add_check(
        check_id: str,
        label: str,
        *,
        status: str,
        severity: str,
        why_it_matters: str,
        operator_action: str,
        details: str = "",
        legacy_message: str | None = None,
    ) -> None:
        checks.append(
            {
                "id": check_id,
                "label": label,
                "status": status,
                "severity": severity,
                "why_it_matters": why_it_matters,
                "operator_action": operator_action,
                "details": details,
            }
        )
        if legacy_message:
            if status == "fail":
                errors.append(legacy_message)
            elif status == "warning":
                warnings.append(legacy_message)

    system_connection = _open_readonly_sqlite(settings.system_db_path)
    system_schema_version = _schema_version(system_connection)
    profile_detection = _profile_detection(system_connection)
    if system_connection is not None:
        system_connection.close()

    deployment_profile = "internal_managed" if settings.product_posture == "production" else "developer_or_personal"
    extras = {
        "pymupdf": importlib.util.find_spec("fitz") is not None,
        "python_docx": importlib.util.find_spec("docx") is not None,
        "openpyxl": importlib.util.find_spec("openpyxl") is not None,
        "cryptography": importlib.util.find_spec("cryptography") is not None,
        "croniter": importlib.util.find_spec("croniter") is not None,
        "watchdog": importlib.util.find_spec("watchdog") is not None,
        "psutil": importlib.util.find_spec("psutil") is not None,
    }
    task_status = _task_registration()

    platform_ok = os.name == "nt"
    add_check(
        "os_compatibility",
        "Windows pilot environment",
        status="pass" if platform_ok else "warning",
        severity="info" if platform_ok else "warning",
        why_it_matters="The current pilot install path is validated for one controlled Windows machine.",
        operator_action="Use the internal Windows deployment shape for pilots.",
        details=f"Detected platform: {platform.system()} ({os.name}).",
        legacy_message=None if platform_ok else "Pilot deployment is validated primarily on Windows.",
    )

    schema_ok = settings.system_db_path.exists() and system_schema_version >= SYSTEM_SCHEMA_VERSION
    schema_status = "pass" if schema_ok else "fail" if settings.system_db_path.exists() else "warning"
    schema_message = None
    if settings.system_db_path.exists() and system_schema_version < SYSTEM_SCHEMA_VERSION:
        schema_message = f"System schema version {system_schema_version} is older than the supported minimum {SYSTEM_SCHEMA_VERSION}."
    elif not settings.system_db_path.exists():
        schema_message = "System database does not exist yet."
    add_check(
        "schema_compatibility",
        "App and schema compatibility",
        status=schema_status,
        severity="error" if schema_status == "fail" else "warning" if schema_status == "warning" else "info",
        why_it_matters="KERN needs a readable, current system/profile schema before the pilot workflow can start safely.",
        operator_action="Run the install flow again or restore a compatible profile before using KERN.",
        details=f"System schema: {system_schema_version}; profile schema: {profile_detection['profile_schema_version']}.",
        legacy_message=schema_message,
    )

    profile_writable, profile_detail = _writable_path_state(settings.profile_root)
    add_check(
        "profile_storage",
        "Profile storage root",
        status="pass" if profile_writable else "fail",
        severity="error" if not profile_writable else "info",
        why_it_matters="Profile data and indexed documents must stay on writable local storage.",
        operator_action="Use a writable local profile root before starting the first workflow.",
        details=profile_detail,
        legacy_message=None if profile_writable else "Profile root is not writable.",
    )

    backup_writable, backup_detail = _writable_path_state(settings.backup_root)
    add_check(
        "backup_storage",
        "Backup storage root",
        status="pass" if backup_writable else "fail",
        severity="error" if not backup_writable else "info",
        why_it_matters="Encrypted backups and rollback bundles need a writable local destination.",
        operator_action="Configure a writable backup root before pilot use.",
        details=backup_detail,
        legacy_message=None if backup_writable else "Backup root is not writable.",
    )

    free_gb = _disk_free_gb(settings.root_path)
    disk_status = "warning" if free_gb is not None and free_gb < 5 else "pass"
    add_check(
        "disk_space",
        "Free disk space",
        status=disk_status,
        severity="warning" if disk_status == "warning" else "info",
        why_it_matters="Local documents, indexes, logs, and encrypted bundles need predictable free space.",
        operator_action="Free up disk space before loading more company files or creating backups.",
        details=f"Free space near {settings.root_path}: {free_gb if free_gb is not None else 'unknown'} GB.",
        legacy_message="Low free disk space may affect local storage and backups." if disk_status == "warning" else None,
    )

    model_path = _preferred_model_path()
    if not settings.llm_enabled:
        model_status = "warning"
        model_detail = "Local LLM is currently disabled."
        model_message = "Local LLM is disabled; grounded drafting will not be available."
    elif not model_path:
        model_status = "fail"
        model_detail = "No preferred local model path is configured."
        model_message = "No local model path is configured."
    else:
        model_exists = Path(model_path).expanduser().exists()
        model_status = "pass" if model_exists else "fail"
        model_detail = f"Preferred path: {model_path}"
        model_message = None if model_exists else f"Model path does not exist: {model_path}."
    add_check(
        "model_path",
        "Local model path",
        status=model_status,
        severity="error" if model_status == "fail" else "warning" if model_status == "warning" else "info",
        why_it_matters="The pilot workflow depends on one predictable local model path.",
        operator_action="Confirm the recommended local model path before drafting from documents.",
        details=model_detail,
        legacy_message=model_message,
    )

    runtime_probe = _runtime_probe(runtime_url or settings.llama_server_url)
    if not settings.llm_enabled:
        runtime_status = "warning"
        runtime_message = "Local runtime probe skipped because LLM is disabled."
    elif runtime_probe["reachable"]:
        runtime_status = "pass"
        runtime_message = None
    else:
        runtime_status = "fail"
        runtime_message = f"Local runtime is not reachable at {(runtime_url or settings.llama_server_url).rstrip('/')}."
    add_check(
        "local_runtime",
        "Local runtime / model service",
        status=runtime_status,
        severity="error" if runtime_status == "fail" else "warning" if runtime_status == "warning" else "info",
        why_it_matters="KERN cannot answer grounded document questions if the local runtime is not reachable.",
        operator_action="Start the local model/runtime service and rerun readiness.",
        details=f"Probe result: {runtime_probe}.",
        legacy_message=runtime_message,
    )

    folders_exist = all(
        Path(path).exists()
        for path in (
            settings.root_path,
            settings.profile_root,
            settings.backup_root,
            settings.document_root,
        )
    )
    add_check(
        "required_folders",
        "Required local folders",
        status="pass" if folders_exist else "warning",
        severity="warning" if not folders_exist else "info",
        why_it_matters="The install path is easier to support when the standard KERN folders already exist.",
        operator_action="Run the install script again if these local roots were never created.",
        details=(
            f"root={settings.root_path.exists()}, profile={settings.profile_root.exists()}, "
            f"backup={settings.backup_root.exists()}, documents={settings.document_root.exists()}"
        ),
        legacy_message=None if folders_exist else "One or more local KERN folders do not exist yet.",
    )

    if settings.policy_mode == "corporate" and not settings.audit_enabled:
        errors.append("Corporate mode requires audit logging to be enabled.")
    if settings.policy_mode == "corporate" and settings.db_encryption_mode == "off":
        errors.append("Corporate mode requires encrypted profile databases.")
    if settings.policy_mode == "corporate" and not settings.artifact_encryption_enabled:
        errors.append("Corporate mode requires encrypted artifacts.")
    if settings.policy_mode == "corporate" and not settings.retention_enforcement_enabled:
        warnings.append("Corporate mode is running without automatic retention enforcement.")
    extras_errors: list[str] = []
    if not extras["pymupdf"]:
        extras_errors.append("PyMuPDF is not installed; PDF ingestion is unavailable.")
        errors.append("PyMuPDF is not installed; PDF ingestion is unavailable.")
    if not extras["python_docx"]:
        warnings.append("python-docx is not installed; DOCX ingestion is unavailable.")
    if not extras["openpyxl"]:
        warnings.append("openpyxl is not installed; spreadsheet ingestion is unavailable.")
    if settings.scheduler_enabled and not extras["croniter"]:
        extras_errors.append("Scheduler is enabled but croniter is not installed.")
        errors.append("Scheduler is enabled but croniter is not installed.")
    if settings.file_watch_dirs and not extras["watchdog"]:
        warnings.append("Watch folders are configured but watchdog is not installed.")
    if settings.policy_mode == "corporate" and not extras["psutil"]:
        warnings.append("Corporate mode is running without psutil/system-control support.")
    if importlib.util.find_spec("cryptography") is None and (settings.db_encryption_mode != "off" or settings.artifact_encryption_enabled):
        extras_errors.append("cryptography is required for the configured encryption posture.")
        errors.append("cryptography is required for the configured encryption posture.")
    if task_status["supported"] and not task_status["registered"] and settings.product_posture == "production":
        warnings.append("Scheduled task supervision is not registered.")

    extras_status = "pass" if not extras_errors else "fail"
    extras_details = ", ".join(f"{key}={'yes' if value else 'no'}" for key, value in extras.items())
    add_check(
        "runtime_extras",
        "Runtime dependencies",
        status=extras_status,
        severity="error" if extras_errors else "info",
        why_it_matters="Document ingestion, encryption, and operator recovery depend on the runtime extras being present.",
        operator_action="Re-run install with the internal deployment preset if required extras are missing.",
        details=extras_details,
    )

    try:
        hardware = None
        if extras["psutil"]:
            import psutil

            vm = psutil.virtual_memory()
            hardware = {"memory_total_gb": round(vm.total / 1024 / 1024 / 1024, 2), "cpu_count": psutil.cpu_count(logical=True)}
        else:
            hardware = {"memory_total_gb": None, "cpu_count": os.cpu_count()}
    except Exception:
        hardware = {"memory_total_gb": None, "cpu_count": os.cpu_count()}

    if errors:
        readiness_status = "not_ready"
        headline = "Not ready for a pilot workflow."
    elif warnings:
        readiness_status = "warning"
        headline = "Ready with warnings."
    else:
        readiness_status = "ready"
        headline = "Ready for a local pilot workflow."

    return {
        "status": readiness_status,
        "headline": headline,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "runtime_probe": runtime_probe,
        "deployment_profile": deployment_profile,
        "update_channel": settings.update_channel,
        "policy_mode": settings.policy_mode,
        "artifact_encryption_enabled": settings.artifact_encryption_enabled,
        "db_encryption_mode": settings.db_encryption_mode,
        "retention_enforcement_enabled": settings.retention_enforcement_enabled,
        "root_path": str(settings.root_path),
        "system_db_path": str(settings.system_db_path),
        "profile_root": str(settings.profile_root),
        "backup_root": str(settings.backup_root),
        "extras": extras,
        "scheduled_task": task_status,
        "system_schema_version": system_schema_version,
        "schema_compatibility": {
            "system_supported": system_schema_version >= SYSTEM_SCHEMA_VERSION,
            "minimum_system_schema": SYSTEM_SCHEMA_VERSION,
            "profile_schema_known": profile_detection["profile_schema_version"] not in {None, "missing"},
        },
        "restore_ready": {
            "self_contained_restore_supported": extras["cryptography"],
            "system_db_writable_parent_exists": settings.system_db_path.parent.exists(),
            "profile_root_parent_exists": settings.profile_root.parent.exists(),
            "backup_root_parent_exists": settings.backup_root.parent.exists(),
        },
        "hardware": hardware,
        "paths": {
            "system_db_exists": settings.system_db_path.exists(),
            "profile_root_exists": settings.profile_root.exists(),
            "backup_root_exists": settings.backup_root.exists(),
            "document_root_exists": settings.document_root.exists(),
        },
        **profile_detection,
    }
