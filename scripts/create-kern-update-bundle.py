from __future__ import annotations

import argparse
import base64
import io
import json
import re
import secrets
import sys
import zipfile
from datetime import datetime
from pathlib import Path


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


def _is_excluded(path: Path) -> bool:
    excluded_roots = {"upgrade-backups", "restores"}
    parts = path.parts
    return bool(parts) and parts[0] in excluded_roots


def _collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _is_excluded(relative):
            continue
        files.append(path)
    return files


def _project_version(repo_root: Path) -> str:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return "0.0.0"
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def create_update_bundle(root: Path, output_path: Path, password: str) -> dict[str, object]:
    from cryptography.fernet import Fernet

    kern_root = root / ".kern"
    if not kern_root.exists():
        raise RuntimeError("No .kern directory found; cannot create an update bundle.")

    files = _collect_files(kern_root)
    created_at = datetime.utcnow().isoformat()
    manifest = {
        "format": "self_contained_update_bundle",
        "version": 1,
        "created_at": created_at,
        "source_root": str(kern_root),
        "app_version": _project_version(root),
        "update_channel": "stable",
        "file_count": len(files),
        "excluded_roots": ["upgrade-backups", "restores"],
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, indent=2))
        for file_path in files:
            archive.write(file_path, arcname=file_path.relative_to(kern_root))

    salt = secrets.token_bytes(16)
    key = _derive_key(password, salt)
    payload = {
        "format": "self_contained_update_bundle",
        "version": 1,
        "created_at": created_at,
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "ciphertext": Fernet(key).encrypt(buffer.getvalue()).decode("ascii"),
        "manifest": manifest,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(output_path)
    return {
        "bundle_path": str(output_path),
        "manifest": manifest,
    }


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a self-contained encrypted KERN update bundle from .kern")
    parser.add_argument("--root", default=".", help="Project/install root containing .kern and pyproject.toml")
    parser.add_argument("--output", required=True, help="Destination .kernbundle path")
    parser.add_argument("--password", help="Encryption password")
    parser.add_argument("--password-stdin", action="store_true", help="Read the encryption password from stdin")
    parser.add_argument("--password-env", help="Read the encryption password from the named environment variable")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    try:
        result = create_update_bundle(
            root=Path(args.root).expanduser().resolve(),
            output_path=Path(args.output).expanduser().resolve(),
            password=_resolve_password(args),
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"ERROR: {exc}")
        return 1

    if args.json:
        print(json.dumps({"ok": True, **result}, indent=2))
    else:
        print(f"Encrypted update bundle created at {result['bundle_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
