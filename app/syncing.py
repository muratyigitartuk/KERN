from __future__ import annotations

import json
import logging
import shutil
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection
from uuid import uuid4

logger = logging.getLogger(__name__)

from app.database import connect
from app.memory import MemoryRepository
from app.platform import PlatformStore
from app.types import ProfileSummary, SyncTarget


SYNC_PATHS: dict[str, tuple[str, str]] = {
    "documents": ("documents_root", "documents"),
    "attachments": ("attachments_root", "attachments"),
    "archives": ("archives_root", "archives"),
    "meetings": ("meetings_root", "meetings"),
    "backups": ("backups_root", "backups"),
    "profile": ("profile_root", ""),
}


class SyncService:
    def __init__(
        self,
        memory_or_platform: MemoryRepository | PlatformStore | Connection,
        profile: ProfileSummary,
        *,
        nextcloud_url: str | None = None,
        platform: PlatformStore | None = None,
    ) -> None:
        if isinstance(memory_or_platform, MemoryRepository):
            self.memory = memory_or_platform if memory_or_platform.profile_slug == profile.slug else MemoryRepository(memory_or_platform.connection, profile_slug=profile.slug)
            self.platform = platform
        elif isinstance(memory_or_platform, PlatformStore):
            self.platform = memory_or_platform
            self.memory = MemoryRepository(connect(Path(profile.db_path)), profile_slug=profile.slug)
        else:
            self.memory = MemoryRepository(memory_or_platform, profile_slug=profile.slug)
            self.platform = platform
        self.profile = profile
        self.nextcloud_url = nextcloud_url.rstrip("/") if nextcloud_url else None

    def availability(self) -> tuple[bool, str | None]:
        if self.platform and self.platform.is_profile_locked(self.profile.slug):
            return False, "Unlock the active profile to access sync."
        targets = self.list_targets(audit=False)
        if not targets:
            return False, "No sync targets configured."
        if any(target.kind == "nas" for target in targets):
            if any(target.kind == "nextcloud" for target in targets):
                return True, "Local NAS sync ready / remote WebDAV encrypted export only."
            return True, "Local sync ready."
        return True, "Remote WebDAV encrypted export only; full remote sync is not implemented."

    def upsert_target(
        self,
        kind: str,
        label: str,
        path_or_url: str,
        *,
        enabled: bool = True,
        username: str | None = None,
        password: str | None = None,
    ) -> SyncTarget:
        self._ensure_unlocked("sync_target_saved")
        normalized_kind = kind if kind in {"nas", "nextcloud"} else "nas"
        existing = self.memory.find_sync_target(normalized_kind, path_or_url)
        target = SyncTarget(
            id=existing.id if existing else str(uuid4()),
            kind=normalized_kind,
            label=label,
            path_or_url=path_or_url,
            enabled=enabled,
        )
        existing_details = self.memory.get_sync_target_details(target.id or "") or {}
        metadata: dict[str, object] = dict(existing_details.get("metadata", {}))
        if username:
            metadata["username"] = username
        if password and self.platform:
            secret = self.platform.store_secret(self.profile.slug, f"sync:{target.id}:password", password)
            metadata["password_ref"] = secret.id
        elif password:
            metadata["password"] = password
        self.memory.upsert_sync_target(target, metadata=metadata or None)
        if self.platform:
            self.platform.record_audit(
                "sync",
                "sync_target_saved",
                "success",
                f"Saved {normalized_kind} sync target {label}.",
                profile_slug=self.profile.slug,
                details={"target_id": target.id, "kind": normalized_kind},
            )
        return target

    def list_targets(self, *, audit: bool = True) -> list[SyncTarget]:
        self._ensure_unlocked("list_sync_targets")
        targets = self.memory.list_sync_targets()
        enriched: list[SyncTarget] = []
        for target in targets:
            details = self.memory.get_sync_target_details(target.id or "") or {}
            metadata = details.get("metadata", {})
            status = "upload_only" if target.kind == "nextcloud" else "ready"
            if metadata.get("last_sync_status") == "failed":
                status = "degraded"
            enriched.append(
                target.model_copy(
                    update={
                        "status": status,
                        "last_sync_at": datetime.fromisoformat(metadata["last_sync_at"]) if metadata.get("last_sync_at") else None,
                        "last_failure": metadata.get("last_error"),
                        "selected_data_classes": list(metadata.get("data_classes", [])),
                    }
                )
            )
        if audit and self.platform:
            self.platform.record_audit(
                "sync",
                "list_sync_targets",
                "success",
                "Listed sync targets.",
                profile_slug=self.profile.slug,
                details={"count": len(enriched)},
            )
        return enriched

    def sync_to_target(self, target: SyncTarget, data_classes: list[str] | None = None) -> str:
        self._ensure_unlocked("sync_profile_data")
        if target.kind != "nas":
            raise RuntimeError("Remote WebDAV sync is upload-only. Use explicit WebDAV upload instead.")
        selected = self._normalized_data_classes(data_classes)
        persisted = self.upsert_target(target.kind, target.label, target.path_or_url, enabled=target.enabled)
        destination_root = Path(persisted.path_or_url).expanduser().resolve()
        staging_root = destination_root.parent / f".{destination_root.name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.stage"
        backup_root = destination_root.parent / f".{destination_root.name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.previous"
        job = self.platform.create_job(
            "profile_sync",
            f"Sync to {persisted.label}",
            profile_slug=self.profile.slug,
            detail="Preparing staged sync.",
            payload={"target_id": persisted.id, "data_classes": selected},
        ) if self.platform else None
        if self.platform and job:
            self.platform.update_job(job.id, status="running", detail="Preparing staged sync.", progress=0.05, checkpoint_stage="planned")
            self.platform.update_checkpoint(
                job.id,
                "planned",
                {
                    "target_id": persisted.id,
                    "data_classes": selected,
                    "destination_root": str(destination_root),
                    "staging_root": str(staging_root),
                    "backup_root": str(backup_root),
                },
            )

        try:
            with self._cleanup_path(staging_root):
                if destination_root.exists():
                    shutil.copytree(destination_root, staging_root, dirs_exist_ok=True)
                else:
                    staging_root.mkdir(parents=True, exist_ok=True)
                if self.platform and job:
                    self.platform.update_checkpoint(
                        job.id,
                        "staging_ready",
                        {"staging_root": str(staging_root), "backup_root": str(backup_root), "destination_root": str(destination_root)},
                    )
                    self.platform.update_job(job.id, status="running", detail="Copying selected data.", progress=0.25, checkpoint_stage="staging_ready")

                for index, data_class in enumerate(selected, start=1):
                    source_root, relative_name = self._source_for_data_class(data_class)
                    destination_path = staging_root / relative_name if relative_name else staging_root
                    if destination_path.exists() and relative_name:
                        shutil.rmtree(destination_path)
                    if destination_path.exists() and not relative_name:
                        shutil.rmtree(destination_path)
                        destination_path.mkdir(parents=True, exist_ok=True)
                    if source_root.exists():
                        if source_root.is_dir():
                            if relative_name:
                                shutil.copytree(source_root, destination_path, dirs_exist_ok=True)
                            else:
                                for child in source_root.iterdir():
                                    child_target = destination_path / child.name
                                    if child.is_dir():
                                        shutil.copytree(child, child_target, dirs_exist_ok=True)
                                    else:
                                        child_target.parent.mkdir(parents=True, exist_ok=True)
                                        shutil.copy2(child, child_target)
                        else:
                            destination_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source_root, destination_path)
                    if self.platform and job:
                        progress = 0.25 + (index / max(1, len(selected))) * 0.45
                        self.platform.update_checkpoint(job.id, f"copied_{data_class}", {"source": str(source_root)})
                        self.platform.update_job(job.id, status="running", detail=f"Copied {data_class}.", progress=min(progress, 0.7), checkpoint_stage=f"copied_{data_class}")

                if self.platform and job:
                    self.platform.update_checkpoint(
                        job.id,
                        "waiting_for_commit",
                        {"destination_root": str(destination_root), "staging_root": str(staging_root), "backup_root": str(backup_root)},
                    )
                    self.platform.update_job(job.id, status="waiting_for_commit", detail="Committing staged sync.", progress=0.8, checkpoint_stage="waiting_for_commit")

                destination_moved = False
                if destination_root.exists():
                    if backup_root.exists():
                        shutil.rmtree(backup_root)
                    destination_root.replace(backup_root)
                    destination_moved = True
                try:
                    staging_root.replace(destination_root)
                except Exception as exc:
                    logger.error("Sync rollback triggered â€” staging move failed: %s", exc)
                    if destination_moved and backup_root.exists() and not destination_root.exists():
                        backup_root.replace(destination_root)
                    raise
                if backup_root.exists():
                    shutil.rmtree(backup_root)

            self._update_target_metadata(persisted, {"last_sync_at": datetime.now(timezone.utc).isoformat(), "last_sync_status": "success", "last_error": None, "data_classes": selected})
            if self.platform and job:
                self.platform.update_checkpoint(job.id, "committed", {"destination_root": str(destination_root)})
                self.platform.update_job(job.id, status="completed", detail=f"Synced {len(selected)} data class(es).", progress=1.0, checkpoint_stage="committed", result={"destination": str(destination_root), "data_classes": selected}, recoverable=False, error_code=None, error_message=None)
                self.platform.record_audit("sync", "sync_profile_data", "success", f"Synced profile data to {persisted.label}.", profile_slug=self.profile.slug, details={"target_id": persisted.id, "data_classes": selected})
            return f"Synced {len(selected)} data class(es) to {destination_root}."
        except Exception as exc:
            if backup_root.exists() and not destination_root.exists():
                backup_root.replace(destination_root)
            self._update_target_metadata(persisted, {"last_sync_at": datetime.now(timezone.utc).isoformat(), "last_sync_status": "failed", "last_error": str(exc), "data_classes": selected})
            if self.platform and job:
                self.platform.update_checkpoint(
                    job.id,
                    "rolled_back",
                    {
                        "target_id": persisted.id,
                        "destination_root": str(destination_root),
                        "backup_root": str(backup_root),
                        "staging_root": str(staging_root),
                    },
                )
                self.platform.update_job(
                    job.id,
                    status="failed",
                    detail=str(exc),
                    progress=0.0,
                    checkpoint_stage="rolled_back",
                    recoverable=False,
                    error_code="sync_failed",
                    error_message=str(exc),
                    result={"target_id": persisted.id, "destination_root": str(destination_root)},
                )
                self.platform.record_audit("sync", "sync_profile_data", "failure", str(exc), profile_slug=self.profile.slug, details={"target_id": persisted.id, "data_classes": selected})
            raise RuntimeError(f"Profile sync failed: {exc}") from exc

    def upload_webdav(
        self,
        source_file: str,
        path_or_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        target_id: str | None = None,
    ) -> str:
        self._ensure_unlocked("upload_webdav")
        destination_url = path_or_url.strip() or self.nextcloud_url or ""
        if not destination_url:
            raise RuntimeError("WebDAV destination URL is required.")
        parsed = urllib.parse.urlparse(destination_url)
        # Reject URLs with embedded credentials
        if parsed.username or parsed.password:
            raise RuntimeError("WebDAV URLs must not contain embedded credentials.")
        # Require HTTPS for non-localhost hosts
        localhost_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme == "http" and parsed.hostname not in localhost_hosts:
            raise RuntimeError("WebDAV uploads require HTTPS unless targeting localhost.")
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError(f"Unsupported URL scheme '{parsed.scheme}'. Use http or https.")
        if target_id:
            details = self.memory.get_sync_target_details(target_id) or {}
            metadata = details.get("metadata", {})
            username = username or str(metadata.get("username") or "")
            password = password or self._resolve_secret(str(metadata.get("password_ref") or ""))
        if not username or not password:
            raise RuntimeError("WebDAV credentials are required.")
        source_path = Path(source_file).expanduser().resolve()
        if not source_path.exists():
            raise RuntimeError("Source file does not exist.")
        allowed_suffixes = {".kernbak", ".kernexp", ".kenc"}
        if not any(source_path.name.endswith(suffix) for suffix in allowed_suffixes):
            raise RuntimeError("Remote export requires an encrypted payload (.kernbak, .kernexp, or .kenc).")
        request = urllib.request.Request(destination_url, data=source_path.read_bytes(), method="PUT")
        auth = urllib.request.HTTPBasicAuthHandler()
        auth.add_password(None, destination_url, username, password)
        opener = urllib.request.build_opener(auth)
        job = self.platform.create_job(
            "webdav_upload",
            f"Upload {source_path.name}",
            profile_slug=self.profile.slug,
            detail="Uploading file to WebDAV target.",
            payload={"source_file": str(source_path), "url": destination_url, "target_id": target_id},
        ) if self.platform else None
        if self.platform and job:
            self.platform.update_job(job.id, status="running", progress=0.2, checkpoint_stage="uploading", detail="Uploading file.")
            self.platform.update_checkpoint(job.id, "uploading", {"source_file": str(source_path), "url": destination_url, "target_id": target_id})
        try:
            with opener.open(request, timeout=20):
                pass
            if target_id:
                persisted = next((target for target in self.memory.list_sync_targets() if target.id == target_id), None)
                if persisted:
                    self._update_target_metadata(
                        persisted,
                        {
                            "last_sync_at": datetime.now(timezone.utc).isoformat(),
                            "last_sync_status": "success",
                            "last_error": None,
                        },
                    )
            if self.platform and job:
                self.platform.update_checkpoint(job.id, "uploaded", {"url": destination_url})
                self.platform.update_job(job.id, status="completed", progress=1.0, checkpoint_stage="uploaded", detail="Upload complete.", result={"url": destination_url})
                self.platform.record_audit("sync", "remote_export", "success", f"Exported {source_path.name} to WebDAV.", profile_slug=self.profile.slug, details={"url": destination_url})
            return destination_url
        except urllib.error.URLError as exc:
            if target_id:
                persisted = next((target for target in self.memory.list_sync_targets() if target.id == target_id), None)
                if persisted:
                    self._update_target_metadata(
                        persisted,
                        {
                            "last_sync_at": datetime.now(timezone.utc).isoformat(),
                            "last_sync_status": "failed",
                            "last_error": str(exc),
                        },
                    )
            if self.platform and job:
                self.platform.update_checkpoint(job.id, "failed", {"url": destination_url, "target_id": target_id})
                self.platform.update_job(job.id, status="failed", progress=0.0, checkpoint_stage="failed", recoverable=False, error_code="webdav_upload_failed", error_message=str(exc), detail=str(exc))
                self.platform.record_audit("sync", "remote_export", "failure", str(exc), profile_slug=self.profile.slug, details={"url": destination_url})
            raise RuntimeError(f"Remote export failed: {exc}") from exc

    def mirror_directory(self, source: str, destination: str) -> str:
        source_root = Path(source).expanduser().resolve()
        destination_root = Path(destination).expanduser().resolve()
        staging_root = destination_root.parent / f".{destination_root.name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.mirror"
        backup_root = destination_root.parent / f".{destination_root.name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.rollback"
        with self._cleanup_path(staging_root):
            shutil.copytree(source_root, staging_root, dirs_exist_ok=True)
            destination_moved = False
            if destination_root.exists():
                if backup_root.exists():
                    shutil.rmtree(backup_root)
                destination_root.replace(backup_root)
                destination_moved = True
            try:
                staging_root.replace(destination_root)
            except Exception as exc:
                logger.error("Restore rollback triggered â€” staging move failed: %s", exc)
                if destination_moved and backup_root.exists() and not destination_root.exists():
                    backup_root.replace(destination_root)
                raise
            if backup_root.exists():
                shutil.rmtree(backup_root)
        return str(destination_root)

    def recover_jobs(self) -> None:
        if not self.platform:
            return
        for job in self.platform.list_jobs(self.profile.slug, limit=20):
            if job.job_type not in {"profile_sync", "webdav_upload"} or not job.recoverable:
                continue
            checkpoint_payload: dict[str, object] = {}
            for checkpoint in self.platform.list_checkpoints(job.id):
                payload_row = self.platform.connection.execute(
                    """
                    SELECT payload_json
                    FROM recovery_checkpoints
                    WHERE job_id = ? AND stage = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (job.id, checkpoint.stage),
                ).fetchone()
                if payload_row:
                    try:
                        checkpoint_payload.update(json.loads(payload_row["payload_json"] or "{}"))
                    except Exception as exc:
                        logger.debug("Failed to parse sync checkpoint payload JSON: %s", exc)
            if job.job_type == "profile_sync":
                destination_root = Path(str(checkpoint_payload.get("destination_root", "") or "")).expanduser().resolve() if checkpoint_payload.get("destination_root") else None
                staging_root = Path(str(checkpoint_payload.get("staging_root", "") or "")).expanduser().resolve() if checkpoint_payload.get("staging_root") else None
                backup_root = Path(str(checkpoint_payload.get("backup_root", "") or "")).expanduser().resolve() if checkpoint_payload.get("backup_root") else None
                if staging_root and staging_root.exists():
                    shutil.rmtree(staging_root, ignore_errors=True)
                if backup_root and backup_root.exists():
                    if destination_root and not destination_root.exists():
                        backup_root.replace(destination_root)
                    else:
                        shutil.rmtree(backup_root, ignore_errors=True)
                self.platform.update_job(
                    job.id,
                    status="rolled_back",
                    recoverable=False,
                    detail="Rolled back interrupted local sync on startup.",
                    checkpoint_stage="rolled_back",
                    progress=1.0,
                    result={"target_id": checkpoint_payload.get("target_id"), "destination_root": str(destination_root) if destination_root else None},
                )
                self.platform.update_checkpoint(
                    job.id,
                    "rolled_back",
                    {"target_id": checkpoint_payload.get("target_id"), "destination_root": str(destination_root) if destination_root else None},
                )
                self.platform.record_audit(
                    "sync",
                    "sync_profile_recovery",
                    "warning",
                    "Rolled back interrupted local sync on startup.",
                    profile_slug=self.profile.slug,
                    details={"job_id": job.id, "target_id": checkpoint_payload.get("target_id")},
                )
                continue
            target_id = str(checkpoint_payload.get("target_id", "") or "")
            if target_id:
                persisted = next((target for target in self.memory.list_sync_targets() if target.id == target_id), None)
                if persisted:
                    self._update_target_metadata(
                        persisted,
                        {
                            "last_sync_at": datetime.now(timezone.utc).isoformat(),
                            "last_sync_status": "failed",
                            "last_error": "Remote export was interrupted before completion.",
                        },
                    )
            self.platform.update_job(
                job.id,
                status="failed",
                recoverable=False,
                detail="Remote export was interrupted. Retry the upload manually.",
                checkpoint_stage="failed",
                progress=0.0,
                error_code=job.error_code or "remote_export_interrupted",
                error_message=job.error_message or "Remote export was interrupted.",
                result={"url": checkpoint_payload.get("url")},
            )
            self.platform.update_checkpoint(
                job.id,
                "failed",
                {"url": checkpoint_payload.get("url"), "target_id": target_id or None},
            )
            self.platform.record_audit(
                "sync",
                "remote_export_recovery",
                "warning",
                "Marked interrupted remote export as failed; retry required.",
                profile_slug=self.profile.slug,
                details={"job_id": job.id, "url": checkpoint_payload.get("url")},
            )

    def _source_for_data_class(self, data_class: str) -> tuple[Path, str]:
        root_attr, relative_name = SYNC_PATHS.get(data_class, ("profile_root", data_class))
        return Path(getattr(self.profile, root_attr)).resolve(), relative_name

    def _normalized_data_classes(self, data_classes: list[str] | None) -> list[str]:
        selected = [item for item in (data_classes or ["documents", "attachments", "archives", "meetings"]) if item in SYNC_PATHS]
        return selected or ["documents", "attachments", "archives", "meetings"]

    def _update_target_metadata(self, target: SyncTarget, metadata: dict[str, object]) -> None:
        existing = self.memory.get_sync_target_details(target.id or "") or {}
        merged = dict(existing.get("metadata", {}))
        merged.update(metadata)
        self.memory.upsert_sync_target(target, metadata=merged)

    def _resolve_secret(self, secret_ref: str) -> str | None:
        if self.platform:
            return self.platform.resolve_secret(secret_ref, profile_slug=self.profile.slug)
        return None

    def _ensure_unlocked(self, action: str) -> None:
        if self.platform:
            self.platform.assert_profile_unlocked(self.profile.slug, "sync", action)

    class _cleanup_path:
        def __init__(self, path: Path) -> None:
            self.path = path

        def __enter__(self):
            return self.path

        def __exit__(self, exc_type, exc, _tb) -> bool:
            if self.path.exists():
                shutil.rmtree(self.path, ignore_errors=True)
            return False
