"""Playwright shell smoke scaffolding.

These tests intentionally stay lightweight. They verify Playwright wiring
and the browser-visible shell contract only; deeper staging validation lives
in the validation pack.

Run with:  python -m pytest tests/test_e2e_scaffold.py --headed
"""
from __future__ import annotations

import os

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

import pytest

try:
    from playwright.sync_api import sync_playwright, Page
except ImportError:
    pytest.skip("playwright not installed — skipping E2E tests", allow_module_level=True)

BASE_URL = os.environ.get("KERN_E2E_BASE_URL", "http://127.0.0.1:8000")
E2E_ENABLED = os.environ.get("KERN_E2E_ENABLED", "0") == "1"

pytestmark = pytest.mark.skipif(not E2E_ENABLED, reason="Set KERN_E2E_ENABLED=1 to run E2E tests")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    yield p
    p.close()
    ctx.close()


# ── Dashboard loads ──────────────────────────────────────────────────


def test_dashboard_loads(page: Page):
    page.goto(f"{BASE_URL}/dashboard")
    assert page.title()
    page.wait_for_selector("#chat-messages", timeout=5000)


def test_health_endpoint(page: Page):
    resp = page.request.get(f"{BASE_URL}/health/live")
    assert resp.ok
    body = resp.json()
    assert body.get("status") == "live"
    assert body.get("severity") in {"ok", "warning", "degraded"}


# ── PWA manifest accessible ─────────────────────────────────────────


def test_manifest_accessible(page: Page):
    resp = page.request.get(f"{BASE_URL}/static/manifest.webmanifest")
    assert resp.ok
    body = resp.json()
    assert body["name"] == "KERN AI Workspace"


# ── Service worker registers ────────────────────────────────────────


def test_service_worker_registers(page: Page):
    page.goto(f"{BASE_URL}/dashboard")
    # Wait for SW registration
    page.wait_for_function(
        "() => navigator.serviceWorker.ready",
        timeout=10000,
    )


# ── Utility tabs are accessible ─────────────────────────────────────


def test_utility_tabs_present(page: Page):
    page.goto(f"{BASE_URL}/dashboard")
    tabs = page.locator('[role="tab"]')
    assert tabs.count() >= 4


# ── Chat input visible ──────────────────────────────────────────────


def test_chat_input_visible(page: Page):
    page.goto(f"{BASE_URL}/dashboard")
    chat_input = page.locator("#chat-input")
    assert chat_input.is_visible()
