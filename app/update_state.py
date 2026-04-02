from __future__ import annotations

import json
from pathlib import Path

from app.config import settings


def update_state_path() -> Path:
    return (settings.root_path / ".kern" / "update-state.json").resolve()


def load_update_state() -> dict[str, str]:
    path = update_state_path()
    if not path.exists():
        return {
            "last_attempt_at": "",
            "last_success_at": "",
            "last_backup_at": "",
            "last_restore_attempt_at": "",
            "last_status": "idle",
            "last_error": "",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "last_attempt_at": "",
            "last_success_at": "",
            "last_backup_at": "",
            "last_restore_attempt_at": "",
            "last_status": "idle",
            "last_error": "",
        }
    return {
        "last_attempt_at": str(payload.get("last_attempt_at") or ""),
        "last_success_at": str(payload.get("last_success_at") or ""),
        "last_backup_at": str(payload.get("last_backup_at") or ""),
        "last_restore_attempt_at": str(payload.get("last_restore_attempt_at") or ""),
        "last_status": str(payload.get("last_status") or "idle"),
        "last_error": str(payload.get("last_error") or ""),
    }
