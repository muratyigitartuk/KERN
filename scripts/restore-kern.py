from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backup import BackupService
from app.config import settings
from app.platform import PlatformStore, connect_platform_db


@dataclass(slots=True)
class RestoreArtifact:
    kind: str
    validation: dict[str, object]
    payload: dict[str, object]
    archive_bytes: bytes | None = None


def _current_app_version() -> str:
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        return "0.0.0"
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def _version_tuple(raw: str) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", raw)]
    return tuple(parts or [0])


def _derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _load_payload(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_password(args: argparse.Namespace) -> str:
    if args.password:
        return str(args.password)
    if args.password_env:
        value = str(__import__("os").environ.get(args.password_env, "")).strip()
        if value:
            return value
        raise RuntimeError(f"Environment variable {args.password_env} is not set.")
    if args.password_stdin:
        value = sys.stdin.read().strip()
        if value:
            return value
        raise RuntimeError("No password was provided on stdin.")
    raise RuntimeError("A password is required.")


def _detect_kind(payload: dict[str, object]) -> str:
    return str(payload.get("format") or "kernbak")


def _load_update_bundle(path: Path, password: str) -> RestoreArtifact:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("cryptography is required for encrypted update bundles.") from exc

    payload = _load_payload(path)
    if _detect_kind(payload) != "self_contained_update_bundle":
        raise RuntimeError("Not a self-contained update bundle.")
    salt = base64.urlsafe_b64decode(str(payload["salt"]).encode("ascii"))
    archive_bytes = Fernet(_derive_key(password, salt)).decrypt(str(payload["ciphertext"]).encode("ascii"))
    with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
        names = archive.namelist()
        manifest = {}
        if "manifest.json" in names:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    validation = {
        "valid": True,
        "bundle_kind": "self_contained_update_bundle",
        "entry_count": len(names),
        "entries": names,
        "manifest": manifest,
        "errors": [],
    }
    return RestoreArtifact("self_contained_update_bundle", validation, payload, archive_bytes)


def _validate_update_bundle_compatibility(artifact: RestoreArtifact, *, force: bool) -> list[str]:
    manifest = artifact.validation.get("manifest", {}) if isinstance(artifact.validation, dict) else {}
    if not isinstance(manifest, dict):
        return []
    errors: list[str] = []
    bundle_app_version = str(manifest.get("app_version", "") or "").strip()
    if bundle_app_version and _version_tuple(bundle_app_version) > _version_tuple(_current_app_version()) and not force:
        errors.append(
            f"Bundle app version {bundle_app_version} is newer than this runtime {_current_app_version()}. Re-run with --force if intentional."
        )
    return errors


def _restore_update_bundle(
    path: Path,
    password: str,
    restore_root: str | Path | None,
    *,
    replace_root: bool = False,
) -> dict[str, object]:
    artifact = _load_update_bundle(path, password)
    requested_root = Path(restore_root or ".kern/restores/default").expanduser().resolve()
    requested_root.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    final_root = requested_root
    if not replace_root and final_root.exists() and any(final_root.iterdir()):
        final_root = requested_root.parent / f"{requested_root.name}-{timestamp}"
    staged_root = requested_root.parent / f".{requested_root.name}-{timestamp}.stage"
    rollback_root = final_root.parent / f".{final_root.name}.rollback"

    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(staged_root)
    staged_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(artifact.archive_bytes or b""), "r") as archive:
            names = archive.namelist()
            if not names:
                raise RuntimeError("Bundle archive is empty.")
            for member in archive.infolist():
                target_path = (staged_root / Path(member.filename)).resolve()
                if staged_root not in target_path.parents and target_path != staged_root:
                    raise RuntimeError(f"Unsafe archive member: {member.filename}")
                if member.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as source, target_path.open("wb") as target:
                    shutil.copyfileobj(source, target)
        manifest_path = staged_root / "manifest.json"
        if manifest_path.exists():
            manifest_path.unlink()
        if final_root.exists():
            with contextlib.suppress(FileNotFoundError):
                shutil.rmtree(rollback_root)
            final_root.replace(rollback_root)
        try:
            staged_root.replace(final_root)
        except Exception:
            if rollback_root.exists() and not final_root.exists():
                rollback_root.replace(final_root)
            raise
        if rollback_root.exists():
            shutil.rmtree(rollback_root)
        return {
            "kind": "self_contained_update_bundle",
            "restored_root": str(final_root),
            "validation": artifact.validation,
            "manifest": artifact.validation.get("manifest", {}),
        }
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(staged_root)
        if rollback_root.exists() and not final_root.exists():
            rollback_root.replace(final_root)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or restore a KERN encrypted backup")
    parser.add_argument("backup_path", help="Path to the backup file")
    parser.add_argument("--password", help="Backup password")
    parser.add_argument("--password-stdin", action="store_true", help="Read the backup password from stdin")
    parser.add_argument("--password-env", help="Read the backup password from the named environment variable")
    parser.add_argument("--restore-root", default=None, help="Destination root for restore")
    parser.add_argument("--validate-only", action="store_true", help="Validate the backup without restoring it")
    parser.add_argument("--force", action="store_true", help="Allow restore even when compatibility checks warn or fail.")
    parser.add_argument("--replace-root", action="store_true", help="Replace the destination root in place instead of restoring beside it.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    backup_path = Path(args.backup_path).expanduser().resolve()
    password = _resolve_password(args)
    try:
        payload = _load_payload(backup_path)
    except Exception as exc:
        payload_out = {"mode": "validate" if args.validate_only else "restore", "validation": {"valid": False, "errors": [str(exc)], "entries": [], "entry_count": 0}}
        if args.json:
            print(json.dumps(payload_out, indent=2, default=str))
        else:
            print("Restore aborted: backup payload could not be read.")
            print(f"ERROR: {exc}")
        return 1
    kind = _detect_kind(payload)

    if kind == "self_contained_update_bundle":
        try:
            artifact = _load_update_bundle(backup_path, password)
        except Exception as exc:
            invalid_validation = {
                "valid": False,
                "bundle_kind": "self_contained_update_bundle",
                "entry_count": 0,
                "entries": [],
                "manifest": {},
                "errors": [str(exc)],
            }
            if args.json:
                print(json.dumps({"mode": "validate" if args.validate_only else "restore", "validation": invalid_validation}, indent=2, default=str))
            else:
                print(f"Validation: failed")
                print(f"ERROR: {exc}")
            return 1
        if args.validate_only:
            if args.json:
                print(json.dumps({"mode": "validate", "validation": artifact.validation}, indent=2, default=str))
            else:
                print(f"Validation: {'ok' if artifact.validation.get('valid') else 'failed'}")
            return 0
        compatibility_errors = _validate_update_bundle_compatibility(artifact, force=bool(args.force))
        if compatibility_errors:
            payload_out = {"mode": "restore", "validation": artifact.validation, "compatibility_errors": compatibility_errors}
            if args.json:
                print(json.dumps(payload_out, indent=2, default=str))
            else:
                print("Restore aborted: bundle compatibility checks failed.")
                for error in compatibility_errors:
                    print(f"ERROR: {error}")
            return 1
        try:
            result = _restore_update_bundle(
                backup_path,
                password,
                args.restore_root,
                replace_root=bool(args.replace_root),
            )
        except Exception as exc:
            if args.json:
                print(json.dumps({"mode": "restore", "validation": artifact.validation, "error": str(exc)}, indent=2, default=str))
            else:
                print("Restore aborted: self-contained update bundle failed.")
                print(f"ERROR: {exc}")
            return 1
        if args.json:
            print(json.dumps({"mode": "restore", **result}, indent=2, default=str))
        else:
            print(f"Restored bundle to {result['restored_root']}")
        return 0

    service = BackupService()
    validation = service.validate_backup(backup_path, password)
    if args.validate_only:
        payload_out = {"mode": "validate", "validation": validation.model_dump(mode="json")}
        if args.json:
            print(json.dumps(payload_out, indent=2, default=str))
        else:
            print(f"Validation: {'ok' if validation.valid else 'failed'}")
            for error in validation.errors:
                print(f"ERROR: {error}")
        return 0 if validation.valid else 1

    if not validation.valid:
        payload_out = {"mode": "restore", "validation": validation.model_dump(mode="json")}
        if args.json:
            print(json.dumps(payload_out, indent=2, default=str))
        else:
            print("Restore aborted: backup validation failed.")
            for error in validation.errors:
                print(f"ERROR: {error}")
        return 1

    restore_root = args.restore_root or ".kern/restores/default"
    plan = service.prepare_restore(backup_path, password, restore_root)
    restored_path = None
    if validation.self_contained:
        platform_connection = connect_platform_db(settings.system_db_path)
        platform = PlatformStore(platform_connection, audit_enabled=settings.audit_enabled)
        try:
            restored_path = service.restore_encrypted_profile_backup_into_platform(
                backup_path,
                password,
                restore_root,
                platform_store=platform,
            )
        finally:
            platform_connection.close()
    else:
        restored_path = service.execute_restore_plan(plan, password)
    payload_out = {
        "mode": "restore",
        "kind": "kernbak",
        "restored_root": str(restored_path),
        "validation": validation.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "profile_state_registered": bool(validation.self_contained),
    }
    if args.json:
        print(json.dumps(payload_out, indent=2, default=str))
    else:
        print(f"Restored backup to {restored_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
