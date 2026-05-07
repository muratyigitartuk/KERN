from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.capabilities import build_capability_registry
from app.config import Settings
from app.database import connect
from app.scheduler import SchedulerService
from app.types import UICommand


REMOVED_COMMANDS = {
    "sync_mailbox",
    "save_email_draft",
    "send_email_draft",
    "apply_email_reminder_suggestion",
    "set_tts_speed",
    "set_tts_voice",
}

REMOVED_ENV = {
    "KERN_SPEAKING_ENABLED",
    "KERN_TTS_PREFERENCE",
    "KERN_PIPER_BINARY",
    "KERN_PIPER_MODEL",
    "KERN_TTS_VOICE",
    "KERN_TTS_SPEED",
    "KERN_IMAP_HOST",
    "KERN_SMTP_HOST",
    "KERN_EMAIL_USERNAME",
    "KERN_EMAIL_PASSWORD",
    "KERN_EMAIL_PASSWORD_REF",
    "KERN_EMAIL_ADDRESS",
    "KERN_NTFY_TOPIC",
    "KERN_NTFY_BASE_URL",
}

REMOVED_CAPABILITIES = {
    "read_email",
    "read_mailbox_summary",
    "sync_mailbox",
    "compose_email",
    "create_email_reminder",
    "schedule_meeting_and_invite",
    "send_ntfy_notification",
    "start_meeting_recording",
    "stop_meeting_recording",
}


class DummyTool:
    def availability(self):
        return True, None


def test_removed_capabilities_are_not_registered() -> None:
    registry = build_capability_registry({name: DummyTool() for name in REMOVED_CAPABILITIES})
    registered = {descriptor.name for descriptor in registry.available_descriptors()}
    assert registered.isdisjoint(REMOVED_CAPABILITIES)


@pytest.mark.parametrize("command_type", sorted(REMOVED_COMMANDS))
def test_removed_websocket_commands_are_rejected(command_type: str) -> None:
    with pytest.raises(ValidationError):
        UICommand(type=command_type)


def test_removed_public_env_fields_are_not_settings_attributes() -> None:
    settings = Settings()
    for env_name in REMOVED_ENV:
        attr = env_name.removeprefix("KERN_").lower()
        assert not hasattr(settings, attr)


def test_removed_package_dependencies_are_absent() -> None:
    deps = Path("pyproject.toml").read_text(encoding="utf-8").lower()
    assert "pyttsx3" not in deps
    assert "sounddevice" not in deps


def test_new_schema_does_not_create_email_tables(tmp_path: Path) -> None:
    connection = connect(tmp_path / "kern.db")
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "email_accounts" not in tables
    assert "email_drafts" not in tables
    assert "mailbox_messages" not in tables


def test_scheduler_rejects_removed_email_summary_action(tmp_path: Path) -> None:
    connection = connect(tmp_path / "kern.db")
    scheduler = SchedulerService(connection, "default")
    with pytest.raises(ValueError):
        scheduler.create_task("Mail summary", "0 9 * * *", "summarize_emails", {})
