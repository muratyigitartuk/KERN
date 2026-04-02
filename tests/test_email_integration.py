"""Email integration tests: IMAP fetch, SMTP send, availability, reminders."""
from __future__ import annotations

import os

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

import smtplib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from app.email_service import EmailService
from app.types import EmailDraft


# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def email_svc(tmp_path):
    from app.database import connect
    from app.memory import MemoryRepository
    from app.local_data import LocalDataService

    conn = connect(tmp_path / "kern.db")
    profile = MagicMock()
    profile.slug = "test"
    platform = MagicMock()
    platform.is_profile_locked = MagicMock(return_value=False)
    # MagicMock blocks attr names starting with 'assert'; explicitly allow it
    platform.assert_profile_unlocked = MagicMock()
    # store_secret returns an object whose .id is a string (used as password_ref in DB)
    secret_mock = MagicMock()
    secret_mock.id = "secret-ref-1"
    platform.store_secret = MagicMock(return_value=secret_mock)
    data = MagicMock(spec=LocalDataService)
    calendar = MagicMock()
    documents = MagicMock()
    return EmailService(
        conn,
        platform,
        profile,
        data,
        calendar,
        documents,
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="secret",
    )


# ── availability ─────────────────────────────────────────────────────


def test_availability_locked(email_svc):
    email_svc.platform.is_profile_locked.return_value = True
    ok, msg = email_svc.availability()
    assert ok is False
    assert "Unlock" in msg


def test_availability_no_accounts(tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "email2.db")
    profile = MagicMock()
    profile.slug = "noaccounts"
    platform = MagicMock()
    platform.is_profile_locked.return_value = False
    svc = EmailService(
        conn, platform, profile, MagicMock(), MagicMock(), MagicMock()
    )
    ok, msg = svc.availability()
    assert ok is False
    assert "Configure" in msg


# ── send_email ───────────────────────────────────────────────────────


def test_send_email_no_smtp_account(email_svc):
    """send_email raises when no SMTP-enabled account exists."""
    draft = EmailDraft(to=["a@b.com"], subject="Hi", body="Body")
    # _resolve_account returns None when there's no SMTP-enabled account
    with patch.object(email_svc, "_resolve_account", return_value=None):
        with pytest.raises(RuntimeError, match="No SMTP"):
            email_svc.send_email(draft)


def test_send_email_smtp_failure_marks_draft_failed(email_svc):
    """SMTP failure marks draft as failed and records audit."""
    account = MagicMock()
    account.smtp_host = "smtp.example.com"
    account.email_address = "user@example.com"
    account.id = "acc-1"
    mock_mark = MagicMock()
    with patch.object(email_svc, "_resolve_account", return_value=account), \
         patch.object(email_svc.memory, "get_email_account_details", return_value={
             "username": "user@example.com",
             "password_ref": "ref1",
         }), \
         patch.object(email_svc, "_resolve_password", return_value="secret"), \
         patch.object(email_svc, "_retry_with_backoff", side_effect=smtplib.SMTPException("fail")), \
         patch.object(email_svc.memory, "mark_email_draft_status", mock_mark):
        draft = EmailDraft(to=["a@b.com"], subject="Test", body="Body")
        with pytest.raises(RuntimeError, match="Email send failed"):
            email_svc.send_email(draft, draft_id="d-1")
    mock_mark.assert_called_with("d-1", "failed")


# ── create_reminder_from_email ───────────────────────────────────────


def test_create_reminder_from_email(email_svc):
    msg = MagicMock()
    msg.subject = "Follow up"
    msg.id = "m-1"
    with patch.object(email_svc, "_resolve_message", return_value=msg):
        due = datetime.now() + timedelta(days=2)
        with patch.object(email_svc, "_extract_due_date", return_value=due):
            reminder_svc = MagicMock()
            reminder_svc.create_reminder = MagicMock()
            title, dt = email_svc.create_reminder_from_email(reminder_svc, message_id="m-1")
    assert "Follow up" in title
    reminder_svc.create_reminder.assert_called_once()


# ── suggest_reminders_from_email ─────────────────────────────────────


def test_suggest_reminders_empty_inbox(email_svc):
    with patch.object(email_svc, "list_indexed_messages", return_value=[]):
        result = email_svc.suggest_reminders_from_email()
    assert result == []


# ── ntfy URL construction ────────────────────────────────────────────


def test_ntfy_url_trailing_slash(tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "ntfy.db")
    profile = MagicMock()
    profile.slug = "test"
    svc = EmailService(
        conn,
        MagicMock(),
        profile,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        ntfy_base_url="https://ntfy.example.com/",
        ntfy_topic="kern",
    )
    assert svc.ntfy_base_url == "https://ntfy.example.com"
    assert svc.ntfy_topic == "kern"
