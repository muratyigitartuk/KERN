from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.path_safety import ensure_local_path, ensure_path_within_roots
from app.platform import PlatformStore
from app.types import ProfileSummary


class ArtifactStore:
    ENCRYPTED_SUFFIX = ".kenc"
    PAYLOAD_VERSION = 1

    def __init__(self, platform: PlatformStore | None, profile: ProfileSummary | None) -> None:
        self.platform = platform
        self.profile = profile

    @property
    def enabled(self) -> bool:
        return bool(settings.artifact_encryption_enabled and self.platform and self.profile)

    def ensure_ready(self) -> dict[str, object]:
        if not (self.platform and self.profile):
            return {"artifact_encryption_enabled": False, "artifact_encryption_migration_state": "not enabled"}
        return self.platform.ensure_profile_artifact_encryption(self.profile.slug)

    def is_encrypted_path(self, path: str | Path) -> bool:
        return str(path).endswith(self.ENCRYPTED_SUFFIX)

    def read_bytes(self, path: str | Path) -> bytes:
        self._assert_accessible("read_artifact")
        file_path = self._approved_path(path, allow_create=False)
        if not self.is_encrypted_path(file_path):
            return file_path.read_bytes()
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        ciphertext = payload.get("ciphertext")
        if not isinstance(ciphertext, str):
            raise RuntimeError("Encrypted artifact payload is invalid.")
        cipher = self._cipher()
        return cipher.decrypt(ciphertext.encode("ascii"))

    def read_text(self, path: str | Path, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding, errors="ignore")

    def write_bytes(self, destination: str | Path, payload: bytes) -> Path:
        self._assert_accessible("write_artifact")
        target = self._approved_path(destination, allow_create=True)
        if not self.enabled:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return target
        encrypted_target = self._encrypted_destination(target)
        encrypted_target.parent.mkdir(parents=True, exist_ok=True)
        cipher = self._cipher()
        envelope = {
            "version": self.PAYLOAD_VERSION,
            "profile_slug": self.profile.slug if self.profile else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ciphertext": cipher.encrypt(payload).decode("ascii"),
        }
        encrypted_target.write_text(json.dumps(envelope), encoding="utf-8")
        return encrypted_target

    def write_text(self, destination: str | Path, payload: str, encoding: str = "utf-8") -> Path:
        return self.write_bytes(destination, payload.encode(encoding))

    def import_file(self, source_path: str | Path, destination_root: str | Path) -> Path:
        self._assert_accessible("import_artifact")
        source = ensure_local_path(source_path, reject_symlink=True)
        root = self._approved_path(destination_root, allow_create=True)
        root.mkdir(parents=True, exist_ok=True)
        if self.enabled:
            try:
                if source.is_relative_to(root):
                    return source
            except AttributeError:
                try:
                    source.relative_to(root)
                    return source
                except ValueError:
                    pass
            destination = root / source.name
            if self._encrypted_destination(destination).exists():
                destination = root / f"{source.stem}-{os.urandom(4).hex()}{source.suffix}"
            return self.write_bytes(destination, source.read_bytes())
        try:
            if source.is_relative_to(root):
                return source
        except AttributeError:
            try:
                source.relative_to(root)
                return source
            except ValueError:
                pass
        candidate = root / source.name
        if candidate.exists() and candidate.resolve() != source:
            candidate = root / f"{source.stem}-{os.urandom(4).hex()}{source.suffix}"
        shutil.copy2(source, candidate)
        return candidate

    @contextlib.contextmanager
    def temporary_plaintext(self, path: str | Path):
        file_path = Path(path)
        if not self.is_encrypted_path(file_path):
            yield file_path
            return
        handle, raw_path = tempfile.mkstemp(suffix=self._plaintext_suffix(file_path))
        os.close(handle)
        temp_path = Path(raw_path)
        try:
            temp_path.write_bytes(self.read_bytes(file_path))
            yield temp_path
        finally:
            temp_path.unlink(missing_ok=True)

    def migrate_profile_artifacts(self, connection) -> int:
        if not (self.enabled and self.profile and self.platform):
            return 0
        roots = [
            Path(self.profile.documents_root),
            Path(self.profile.attachments_root),
            Path(self.profile.archives_root),
            Path(self.profile.meetings_root),
        ]
        migrated: dict[str, str] = {}
        for root in roots:
            if not root.exists():
                continue
            for file_path in sorted(root.rglob("*")):
                if not file_path.is_file():
                    continue
                if self.is_encrypted_path(file_path):
                    continue
                if file_path.suffix == ".db":
                    continue
                encrypted = self.write_bytes(file_path, file_path.read_bytes())
                file_path.unlink(missing_ok=True)
                migrated[str(file_path)] = str(encrypted)
        if not migrated:
            self.platform.set_artifact_migration_state(self.profile.slug, "ready")
            return 0
        self._rewrite_database_paths(connection, migrated)
        self.platform.set_artifact_migration_state(self.profile.slug, "ready")
        return len(migrated)

    def _rewrite_database_paths(self, connection, migrated: dict[str, str]) -> None:
        for old_path, new_path in migrated.items():
            connection.execute("UPDATE document_records SET file_path = REPLACE(file_path, ?, ?) WHERE profile_slug = ?", (old_path, new_path, self.profile.slug))
            connection.execute("UPDATE conversation_archives SET file_path = REPLACE(file_path, ?, ?) WHERE profile_slug = ?", (old_path, new_path, self.profile.slug))
            connection.execute("UPDATE meeting_records SET audio_path = REPLACE(audio_path, ?, ?), transcript_path = REPLACE(COALESCE(transcript_path, ''), ?, ?) WHERE profile_slug = ?", (old_path, new_path, old_path, new_path, self.profile.slug))
            connection.execute("UPDATE transcript_artifacts SET file_path = REPLACE(COALESCE(file_path, ''), ?, ?) WHERE profile_slug = ?", (old_path, new_path, self.profile.slug))
            connection.execute("UPDATE german_business_documents SET file_path = REPLACE(COALESCE(file_path, ''), ?, ?) WHERE profile_slug = ?", (old_path, new_path, self.profile.slug))

        rows = connection.execute(
            "SELECT id, metadata_json FROM document_records WHERE profile_slug = ?",
            (self.profile.slug,),
        ).fetchall()
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            updated = False
            for key in ("stored_path", "file_path"):
                value = metadata.get(key)
                if isinstance(value, str) and value in migrated:
                    metadata[key] = migrated[value]
                    updated = True
            if updated:
                connection.execute("UPDATE document_records SET metadata_json = ? WHERE id = ?", (json.dumps(metadata), row["id"]))

        rows = connection.execute(
            "SELECT id, attachment_paths_json, metadata_json FROM mailbox_messages WHERE profile_slug = ?",
            (self.profile.slug,),
        ).fetchall()
        for row in rows:
            attachment_paths = json.loads(row["attachment_paths_json"] or "[]")
            metadata = json.loads(row["metadata_json"] or "{}")
            changed = False
            attachment_paths = [migrated.get(path, path) for path in attachment_paths]
            if attachment_paths != json.loads(row["attachment_paths_json"] or "[]"):
                changed = True
            raw_path = metadata.get("raw_path")
            if isinstance(raw_path, str) and raw_path in migrated:
                metadata["raw_path"] = migrated[raw_path]
                changed = True
            if changed:
                connection.execute(
                    "UPDATE mailbox_messages SET attachment_paths_json = ?, metadata_json = ? WHERE id = ?",
                    (json.dumps(attachment_paths), json.dumps(metadata), row["id"]),
                )

        connection.commit()

    def _cipher(self):
        if not (self.platform and self.profile):
            raise RuntimeError("Artifact encryption is not configured.")
        security_state = self.platform.ensure_profile_artifact_encryption(self.profile.slug)
        key_ref = str(security_state.get("artifact_key_ref") or "")
        if not key_ref:
            raise RuntimeError("Artifact encryption key is unavailable.")
        key = self.platform.resolve_secret(key_ref, profile_slug=self.profile.slug, audit=False)
        if not key:
            raise PermissionError("Active profile is locked or artifact encryption key is unavailable.")
        from cryptography.fernet import Fernet

        return Fernet(key.encode("ascii"))

    def _assert_accessible(self, action: str) -> None:
        if self.platform and self.profile:
            self.platform.assert_profile_unlocked(self.profile.slug, "artifacts", action)

    def _encrypted_destination(self, target: Path) -> Path:
        if self.is_encrypted_path(target):
            return target
        return target.with_name(f"{target.name}{self.ENCRYPTED_SUFFIX}")

    def _plaintext_suffix(self, encrypted_path: Path) -> str:
        name = encrypted_path.name
        if name.endswith(self.ENCRYPTED_SUFFIX):
            name = name[: -len(self.ENCRYPTED_SUFFIX)]
        suffixes = Path(name).suffixes
        return "".join(suffixes) or ".bin"

    def _approved_roots(self) -> list[str | Path]:
        roots: list[str | Path] = [settings.root_path, settings.attachment_root, settings.archive_root]
        if self.profile:
            roots.extend(
                [
                    self.profile.profile_root,
                    self.profile.documents_root,
                    self.profile.attachments_root,
                    self.profile.archives_root,
                    self.profile.meetings_root,
                    self.profile.backups_root,
                ]
            )
        return roots

    def _approved_path(self, path: str | Path, *, allow_create: bool) -> Path:
        if not self.profile and not self.platform:
            return ensure_local_path(path, reject_symlink=True)
        return ensure_path_within_roots(
            path,
            roots=self._approved_roots(),
            reject_symlink=True,
            allow_create=allow_create,
        )
