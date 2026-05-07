from __future__ import annotations

import base64
import io
import json
import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from app.types import BackupTarget, BackupValidationResult, ProfileSummary, RestorePlan

if TYPE_CHECKING:
    from app.platform import PlatformStore


class BackupService:
    MANIFEST_NAME = "kern-manifest.json"
    PROFILE_STATE_NAME = "kern-profile-state.json"

    def create_encrypted_profile_backup(
        self,
        profile: ProfileSummary,
        target: BackupTarget,
        password: str,
        *,
        platform_store: "PlatformStore | None" = None,
    ) -> Path:
        if not password.strip():
            raise ValueError("Backup password is required.")
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("cryptography is required for encrypted backups.") from exc
        target_path = Path(target.path).resolve()
        target_path.mkdir(parents=True, exist_ok=True)
        profile_root = Path(profile.profile_root)
        backup_state = platform_store.export_profile_backup_state(profile.slug) if platform_store else None
        archive_bytes = self._zip_directory(profile_root, profile.slug, backup_state=backup_state)
        salt = Fernet.generate_key()
        key = self._derive_key(password, salt)
        encrypted = Fernet(key).encrypt(archive_bytes)
        payload = {
            "version": 3 if backup_state else 2,
            "profile_slug": profile.slug,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
            "ciphertext": encrypted.decode("ascii"),
        }
        destination = target_path / f"{profile.slug}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.kernbak"
        temp_destination = destination.with_suffix(f"{destination.suffix}.tmp")
        temp_destination.write_text(json.dumps(payload), encoding="utf-8")
        temp_destination.replace(destination)
        return destination

    def list_backups(self, profile: ProfileSummary, target: BackupTarget) -> list[str]:
        target_path = Path(target.path).resolve()
        if not target_path.exists():
            return []
        return [str(path) for path in sorted(target_path.glob(f"{profile.slug}-*.kernbak"), reverse=True)]

    def inspect_backup(self, backup_path: str | Path) -> dict[str, object]:
        path = Path(backup_path).resolve()
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            "path": str(path),
            "profile_slug": payload.get("profile_slug"),
            "created_at": payload.get("created_at"),
            "version": payload.get("version", 1),
            "self_contained": bool(payload.get("version", 1) >= 3),
        }

    def validate_backup(self, backup_path: str | Path, password: str) -> BackupValidationResult:
        path = Path(backup_path).resolve()
        if not path.exists():
            return BackupValidationResult(valid=False, errors=["Backup file does not exist."])
        try:
            archive_bytes, payload = self._load_archive_bytes(path, password)
        except Exception as exc:
            return BackupValidationResult(valid=False, errors=[str(exc)])
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
                names = archive.namelist()
                errors = self._validate_archive_members(names)
                manifest = self._read_manifest(archive)
                self_contained = self.PROFILE_STATE_NAME in names
        except zipfile.BadZipFile as exc:
            return BackupValidationResult(valid=False, errors=[f"Invalid backup archive: {exc}"])

        if not names:
            errors.append("Backup archive is empty.")
        profile_slug = str(manifest.get("profile_slug") or payload.get("profile_slug") or "").strip() or None
        created_at_raw = manifest.get("created_at") or payload.get("created_at")
        created_at = None
        if created_at_raw:
            try:
                created_at = datetime.fromisoformat(str(created_at_raw))
            except ValueError:
                errors.append("Backup created_at timestamp is invalid.")
        return BackupValidationResult(
            valid=not errors,
            profile_slug=profile_slug,
            created_at=created_at,
            entry_count=len(names),
            self_contained=self_contained,
            errors=errors,
            entries=names,
        )

    def prepare_restore(self, backup_path: str | Path, password: str, restore_root: str | Path) -> RestorePlan:
        validation = self.validate_backup(backup_path, password)
        if not validation.valid:
            raise RuntimeError("; ".join(validation.errors) or "Backup validation failed.")
        requested_root = Path(restore_root).expanduser().resolve()
        requested_root.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        profile_slug = validation.profile_slug or "restored-profile"
        final_root = requested_root
        if final_root.exists() and any(final_root.iterdir()):
            final_root = requested_root.parent / f"{requested_root.name}-{profile_slug}-{timestamp}"
        staged_root = requested_root.parent / f".{requested_root.name}-{profile_slug}-{timestamp}.stage"
        return RestorePlan(
            backup_path=str(Path(backup_path).resolve()),
            requested_root=str(requested_root),
            staged_root=str(staged_root),
            final_root=str(final_root),
            profile_slug=profile_slug,
        )

    def restore_encrypted_profile_backup(self, backup_path: str | Path, password: str, restore_root: str | Path) -> Path:
        plan = self.prepare_restore(backup_path, password, restore_root)
        return self.execute_restore_plan(plan, password)

    def restore_encrypted_profile_backup_into_platform(
        self,
        backup_path: str | Path,
        password: str,
        restore_root: str | Path,
        *,
        platform_store: "PlatformStore",
        backup_root: str | Path | None = None,
    ) -> Path:
        plan = self.prepare_restore(backup_path, password, restore_root)
        restored_root = self.execute_restore_plan(plan, password)
        _, _, backup_state = self._load_backup_package(Path(plan.backup_path), password)
        if backup_state:
            platform_store.import_profile_backup_state(
                backup_state,
                restored_root=restored_root,
                backup_root=Path(backup_root).resolve() if backup_root else None,
            )
        return restored_root

    def execute_restore_plan(self, plan: RestorePlan, password: str) -> Path:
        archive_bytes, _payload = self._load_archive_bytes(Path(plan.backup_path), password)
        staging_root = Path(plan.staged_root)
        final_root = Path(plan.final_root)
        rollback_root = self.rollback_root_for_plan(plan)
        with contextlib_suppress(FileNotFoundError):
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
                names = archive.namelist()
                errors = self._validate_archive_members(names)
                if errors:
                    raise RuntimeError("; ".join(errors))
                for member in archive.infolist():
                    # Skip symlinks to prevent overwriting arbitrary files
                    if member.external_attr >> 16 & 0o120000 == 0o120000:
                        logger.warning("Skipping symlink in backup: %s", member.filename)
                        continue
                    target_path = self._safe_destination(staging_root, member.filename)
                    if member.is_dir():
                        target_path.mkdir(parents=True, exist_ok=True)
                        continue
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member, "r") as source, target_path.open("wb") as target:
                        shutil.copyfileobj(source, target)
            manifest_path = staging_root / self.MANIFEST_NAME
            if manifest_path.exists():
                manifest_path.unlink()
            final_root.parent.mkdir(parents=True, exist_ok=True)
            destination_moved = False
            if final_root.exists():
                with contextlib_suppress(FileNotFoundError):
                    shutil.rmtree(rollback_root)
                final_root.replace(rollback_root)
                destination_moved = True
            try:
                staging_root.replace(final_root)
            except Exception as exc:
                logger.error("Failed to move staging to final root: %s", exc)
                if destination_moved and rollback_root.exists() and not final_root.exists():
                    rollback_root.replace(final_root)
                raise
            if rollback_root.exists():
                shutil.rmtree(rollback_root)
            return final_root
        except Exception as exc:
            logger.error("Restore failed, rolling back: %s", exc)
            with contextlib_suppress(FileNotFoundError):
                shutil.rmtree(staging_root)
            if rollback_root.exists() and not final_root.exists():
                rollback_root.replace(final_root)
            raise

    def rollback_root_for_plan(self, plan: RestorePlan) -> Path:
        final_root = Path(plan.final_root)
        return final_root.parent / f".{final_root.name}.rollback"

    def cleanup_restore_plan(self, plan: RestorePlan) -> None:
        staging_root = Path(plan.staged_root)
        final_root = Path(plan.final_root)
        rollback_root = self.rollback_root_for_plan(plan)
        with contextlib_suppress(FileNotFoundError):
            shutil.rmtree(staging_root)
        if rollback_root.exists() and not final_root.exists():
            rollback_root.replace(final_root)
        elif rollback_root.exists():
            shutil.rmtree(rollback_root)

    def _zip_directory(self, root: Path, profile_slug: str, *, backup_state: dict[str, object] | None = None) -> bytes:
        buffer = io.BytesIO()
        resolved_root = root.resolve()
        manifest = {
            "version": 3 if backup_state else 2,
            "profile_slug": profile_slug,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "self_contained": bool(backup_state),
        }
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(self.MANIFEST_NAME, json.dumps(manifest, sort_keys=True))
            if backup_state:
                archive.writestr(self.PROFILE_STATE_NAME, json.dumps(backup_state, sort_keys=True))
            for file_path in sorted(root.rglob("*")):
                if file_path.is_symlink():
                    logger.warning("Skipping symlink while creating backup: %s", file_path)
                    continue
                if file_path.is_dir():
                    continue
                resolved_file = file_path.resolve()
                try:
                    relative_name = resolved_file.relative_to(resolved_root)
                except ValueError:
                    logger.warning("Skipping backup entry outside profile root: %s", file_path)
                    continue
                archive.write(resolved_file, arcname=relative_name)
        return buffer.getvalue()

    def _load_archive_bytes(self, backup_path: Path, password: str) -> tuple[bytes, dict[str, object]]:
        if not password.strip():
            raise ValueError("Backup password is required.")
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("cryptography is required for encrypted backups.") from exc
        payload = json.loads(backup_path.read_text(encoding="utf-8"))
        salt = base64.urlsafe_b64decode(payload["salt"].encode("ascii"))
        key = self._derive_key(password, salt)
        archive_bytes = Fernet(key).decrypt(payload["ciphertext"].encode("ascii"))
        return archive_bytes, payload

    def _load_backup_package(self, backup_path: Path, password: str) -> tuple[bytes, dict[str, object], dict[str, object] | None]:
        archive_bytes, payload = self._load_archive_bytes(backup_path, password)
        backup_state: dict[str, object] | None = None
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
            if self.PROFILE_STATE_NAME in archive.namelist():
                try:
                    backup_state = json.loads(archive.read(self.PROFILE_STATE_NAME).decode("utf-8"))
                except Exception as exc:
                    logger.warning("Failed to parse backup profile state: %s", exc)
                    backup_state = None
        return archive_bytes, payload, backup_state

    def _read_manifest(self, archive: zipfile.ZipFile) -> dict[str, object]:
        if self.MANIFEST_NAME not in archive.namelist():
            return {}
        try:
            return json.loads(archive.read(self.MANIFEST_NAME).decode("utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse backup manifest: %s", exc)
            return {}

    def _validate_archive_members(self, entries: list[str]) -> list[str]:
        errors: list[str] = []
        for entry in entries:
            try:
                pure = PurePosixPath(entry)
            except Exception as exc:
                logger.debug("Invalid archive entry %r: %s", entry, exc)
                errors.append(f"Invalid archive entry: {entry}")
                continue
            if pure.is_absolute():
                errors.append(f"Absolute archive path rejected: {entry}")
                continue
            if ".." in pure.parts:
                errors.append(f"Path traversal rejected: {entry}")
        return errors

    def _safe_destination(self, root: Path, member_name: str) -> Path:
        candidate = (root / PurePosixPath(member_name).as_posix()).resolve()
        if root not in candidate.parents and candidate != root:
            raise RuntimeError(f"Unsafe archive member: {member_name}")
        return candidate

    def _derive_key(self, password: str, salt: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


class contextlib_suppress:
    def __init__(self, *exceptions: type[BaseException]) -> None:
        self.exceptions = exceptions

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, _tb) -> bool:
        return exc_type is not None and issubclass(exc_type, self.exceptions)
