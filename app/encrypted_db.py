from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

_PERSIST_LOCK = RLock()
logger = logging.getLogger(__name__)


def _protected_temp_dir(target: Path) -> Path:
    temp_root = target.parent / ".kern-tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    return temp_root


def _atomic_write_text(path: Path, payload: str) -> None:
    temp_dir = _protected_temp_dir(path)
    handle, raw_path = tempfile.mkstemp(dir=temp_dir, suffix=".json")
    temp_path = Path(raw_path)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


class EncryptedProfileConnection(sqlite3.Connection):
    def configure_encrypted_storage(
        self,
        *,
        encrypted_path: Path,
        fernet_key: str,
        key_version: int,
        key_derivation_version: str,
    ) -> None:
        from cryptography.fernet import Fernet

        self._encrypted_path = encrypted_path
        self._cipher = Fernet(fernet_key.encode("ascii"))
        self._key_version = key_version
        self._key_derivation_version = key_derivation_version
        self._persist_guard = False

    def rotate_encrypted_storage(
        self,
        *,
        fernet_key: str,
        key_version: int,
        key_derivation_version: str,
    ) -> None:
        from cryptography.fernet import Fernet

        self._cipher = Fernet(fernet_key.encode("ascii"))
        self._key_version = key_version
        self._key_derivation_version = key_derivation_version

    def persist_encrypted(self) -> None:
        if getattr(self, "_persist_guard", False) or not hasattr(self, "_encrypted_path"):
            return
        with _PERSIST_LOCK:
            self._persist_guard = True
            temp_path: Path | None = None
            temp_connection: sqlite3.Connection | None = None
            try:
                handle, raw_path = tempfile.mkstemp(dir=_protected_temp_dir(self._encrypted_path), suffix=".sqlite3")
                temp_path = Path(raw_path)
                try:
                    os.close(handle)
                except OSError:
                    pass
                temp_connection = sqlite3.connect(temp_path)
                self.backup(temp_connection)
                temp_connection.commit()
                payload = {
                    "version": 1,
                    "mode": "fernet",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "key_version": getattr(self, "_key_version", 0),
                    "key_derivation_version": getattr(self, "_key_derivation_version", "v1"),
                    "ciphertext": self._cipher.encrypt(temp_path.read_bytes()).decode("ascii"),
                }
                self._encrypted_path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(self._encrypted_path, json.dumps(payload))
            finally:
                if temp_connection is not None:
                    temp_connection.close()
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
                self._persist_guard = False

    def commit(self) -> None:
        super().commit()
        self.persist_encrypted()

    def close(self) -> None:
        if hasattr(self, "_encrypted_path"):
            self.persist_encrypted()
        super().close()


def hydrate_encrypted_connection(connection: EncryptedProfileConnection, encrypted_path: Path, fernet_key: str) -> str:
    from cryptography.fernet import Fernet, InvalidToken

    if not encrypted_path.exists():
        return "new"

    payload_bytes = encrypted_path.read_bytes()
    if payload_bytes.startswith(b"SQLite format 3"):
        opt_in = os.getenv("KERN_ALLOW_PLAINTEXT_DB_MIGRATION", "").strip().lower() in {"1", "true", "yes", "on"}
        if not opt_in:
            raise RuntimeError(
                "Encrypted database file contains plaintext SQLite data. "
                "Refusing migration without KERN_ALLOW_PLAINTEXT_DB_MIGRATION=1."
            )
        logger.warning(
            "SECURITY: Encrypted database file %s contains unencrypted SQLite data. "
            "Migrating only because KERN_ALLOW_PLAINTEXT_DB_MIGRATION is enabled.",
            encrypted_path,
        )
        plaintext = sqlite3.connect(encrypted_path)
        try:
            plaintext.backup(connection)
            connection.commit()
        finally:
            plaintext.close()
        return "migrated_plaintext"

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise RuntimeError("Encrypted database payload is invalid.") from exc

    if payload.get("mode") != "fernet" or "ciphertext" not in payload:
        raise RuntimeError("Unsupported encrypted database payload.")

    temp_path: Path | None = None
    temp_connection: sqlite3.Connection | None = None
    try:
        ciphertext = payload["ciphertext"].encode("ascii")
        raw_bytes = Fernet(fernet_key.encode("ascii")).decrypt(ciphertext)
        handle, raw_path = tempfile.mkstemp(dir=_protected_temp_dir(encrypted_path), suffix=".sqlite3")
        temp_path = Path(raw_path)
        try:
            os.close(handle)
        except OSError:
            pass
        temp_path.write_bytes(raw_bytes)
        temp_connection = sqlite3.connect(temp_path)
        temp_connection.backup(connection)
        connection.commit()
    except InvalidToken as exc:
        raise RuntimeError("Encrypted profile database could not be unlocked.") from exc
    finally:
        if temp_connection is not None:
            temp_connection.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return "loaded"


def rewrite_encrypted_database(
    encrypted_path: Path,
    *,
    old_fernet_key: str,
    new_fernet_key: str,
    key_version: int,
    key_derivation_version: str,
) -> None:
    from cryptography.fernet import Fernet, InvalidToken

    if not encrypted_path.exists():
        raise RuntimeError("Encrypted database file does not exist.")

    payload_bytes = encrypted_path.read_bytes()
    if payload_bytes.startswith(b"SQLite format 3"):
        raw_bytes = payload_bytes
    else:
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise RuntimeError("Encrypted database payload is invalid.") from exc

        if payload.get("mode") != "fernet" or "ciphertext" not in payload:
            raise RuntimeError("Unsupported encrypted database payload.")

        try:
            raw_bytes = Fernet(old_fernet_key.encode("ascii")).decrypt(payload["ciphertext"].encode("ascii"))
        except InvalidToken as exc:
            raise RuntimeError("Encrypted profile database could not be unlocked.") from exc

    new_payload = {
        "version": 1,
        "mode": "fernet",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "key_version": key_version,
        "key_derivation_version": key_derivation_version,
        "ciphertext": Fernet(new_fernet_key.encode("ascii")).encrypt(raw_bytes).decode("ascii"),
    }
    _atomic_write_text(encrypted_path, json.dumps(new_payload))
