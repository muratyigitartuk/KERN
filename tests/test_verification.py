"""Tests for VerificationService — all tool verification paths."""
from __future__ import annotations

import os

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.types import ExecutionReceipt, ToolRequest, ToolResult
from app.verification import VerificationService


@pytest.fixture
def svc():
    return VerificationService()


def _make_request(tool_name: str, **kwargs) -> ToolRequest:
    return ToolRequest(
        tool_name=tool_name,
        arguments=kwargs,
        user_utterance="test",
        reason="test",
    )


def _make_result(status: str = "observed", **data) -> ToolResult:
    return ToolResult(
        status=status,
        display_text="ok",
        data=data,
        evidence=[],
        side_effects=[],
    )


# ── write_file verification ──────────────────────────────────────────


def test_verify_write_file_exists(svc, tmp_path):
    f = tmp_path / "output.txt"
    f.write_text("hello")
    req = _make_request("write_file", path=str(f))
    res = _make_result()
    receipt = svc.verify(req, res)
    assert receipt.status == "observed"
    assert receipt.verification_source == "filesystem"
    assert "bytes" in receipt.evidence[-1]


def test_verify_write_file_missing(svc, tmp_path):
    req = _make_request("write_file", path=str(tmp_path / "gone.txt"))
    res = _make_result()
    receipt = svc.verify(req, res)
    assert "not found" in receipt.evidence[-1]


def test_verify_write_file_no_path(svc):
    req = _make_request("write_file")
    res = _make_result()
    receipt = svc.verify(req, res)
    assert receipt.status == "observed"  # no change when no path


# ── create_reminder verification ─────────────────────────────────────


def test_verify_reminder_found(svc, tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "kern.db")
    conn.execute("INSERT INTO local_reminders (id, title, due_at, created_at) VALUES (1, 'Test', '2026-04-01', '2026-03-25')")
    conn.commit()

    req = _make_request("create_reminder", title="Test")
    res = _make_result(reminder_id=1)
    receipt = svc.verify(req, res, connection=conn)
    assert receipt.status == "observed"
    assert receipt.verification_source == "database"


def test_verify_reminder_by_title(svc, tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "kern.db")
    conn.execute("INSERT INTO local_reminders (id, title, due_at, created_at) VALUES (1, 'Follow up', '2026-04-01', '2026-03-25')")
    conn.commit()

    req = _make_request("create_reminder", title="Follow up")
    res = _make_result()  # no reminder_id in result
    receipt = svc.verify(req, res, connection=conn)
    assert receipt.status == "observed"


def test_verify_reminder_not_found(svc, tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "kern.db")
    req = _make_request("create_reminder", title="Missing")
    res = _make_result(id=999)
    receipt = svc.verify(req, res, connection=conn)
    assert "not found" in receipt.evidence[-1]


def test_verify_reminder_no_connection(svc):
    req = _make_request("create_reminder", title="Test")
    res = _make_result(reminder_id=1)
    receipt = svc.verify(req, res)
    assert receipt.status == "observed"  # no verification without connection


# ── create_schedule verification ─────────────────────────────────────


def test_verify_schedule_found(svc, tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "kern.db")
    conn.execute(
        "INSERT INTO scheduled_tasks (id, profile_slug, title, cron_expression, action_type, enabled, created_at, updated_at) "
        "VALUES ('t-1', 'test', 'Daily check', '0 9 * * *', 'reminder', 1, '2026-03-25', '2026-03-25')"
    )
    conn.commit()

    req = _make_request("create_schedule")
    res = _make_result(task_id="t-1")
    receipt = svc.verify(req, res, connection=conn)
    assert receipt.status == "observed"
    assert "Daily check" in receipt.evidence[-1]


def test_verify_schedule_not_found(svc, tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "kern.db")
    req = _make_request("create_schedule")
    res = _make_result(task_id="nonexistent")
    receipt = svc.verify(req, res, connection=conn)
    assert "not found" in receipt.evidence[-1]


# ── compose_email verification ───────────────────────────────────────


def test_verify_email_found(svc, tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "kern.db")
    conn.execute(
        "INSERT INTO email_drafts (id, profile_slug, subject, body, status, created_at) "
        "VALUES ('d-1', 'test', 'Hello', 'Body', 'draft', '2026-03-25')"
    )
    conn.commit()

    req = _make_request("compose_email")
    res = _make_result(draft_id="d-1")
    receipt = svc.verify(req, res, connection=conn)
    assert receipt.status == "observed"
    assert receipt.verification_source == "database"


def test_verify_email_not_found(svc, tmp_path):
    from app.database import connect

    conn = connect(tmp_path / "kern.db")
    req = _make_request("compose_email")
    res = _make_result(draft_id="missing")
    receipt = svc.verify(req, res, connection=conn)
    assert "not found" in receipt.evidence[-1]


# ── failed status skips verification ─────────────────────────────────


def test_failed_status_not_verified(svc, tmp_path):
    f = tmp_path / "exists.txt"
    f.write_text("data")
    req = _make_request("write_file", path=str(f))
    res = _make_result(status="failed")
    receipt = svc.verify(req, res)
    assert receipt.status == "failed"
    assert receipt.verification_source == "none"


# ── unknown tool passes through ──────────────────────────────────────


def test_unknown_tool_passes_through(svc):
    req = _make_request("unknown_tool")
    res = _make_result()
    receipt = svc.verify(req, res)
    assert receipt.status == "observed"
