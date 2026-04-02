"""Load testing scaffold — validates locustfile structure and basic config.

The actual load tests require a running server and locust installed.
These unit tests just confirm the scaffold is importable and well-formed.
"""
from __future__ import annotations

import os

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCUSTFILE = PROJECT_ROOT / "tests" / "locustfile.py"


# ── Ensure locustfile exists ─────────────────────────────────────────


def test_locustfile_exists():
    assert LOCUSTFILE.exists(), "tests/locustfile.py should exist for load testing"


def test_locustfile_has_user_class():
    content = LOCUSTFILE.read_text(encoding="utf-8")
    assert "HttpUser" in content or "User" in content


def test_locustfile_has_tasks():
    content = LOCUSTFILE.read_text(encoding="utf-8")
    assert "@task" in content or "tasks" in content


def test_locustfile_has_health_check():
    content = LOCUSTFILE.read_text(encoding="utf-8")
    assert "/health" in content
