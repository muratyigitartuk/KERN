"""PowerShell script validation tests.

These tests verify that the project's PowerShell scripts are well-formed:
syntax parsing, mandatory parameters, and structural requirements.
They do NOT execute the scripts — just validate their content.
"""
from __future__ import annotations

import os
import re

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

PS1_FILES = sorted(SCRIPTS_DIR.glob("*.ps1")) if SCRIPTS_DIR.exists() else []


@pytest.fixture(params=PS1_FILES, ids=lambda p: p.name)
def ps1_content(request):
    return request.param.read_text(encoding="utf-8")


# ── Basic structure ──────────────────────────────────────────────────


def test_ps1_scripts_exist():
    assert len(PS1_FILES) > 0, "Expected at least one .ps1 script in scripts/"


def test_ps1_no_syntax_errors(ps1_content):
    """Check for common PowerShell syntax issues."""
    # Unmatched braces
    open_braces = ps1_content.count("{")
    close_braces = ps1_content.count("}")
    assert open_braces == close_braces, "Unmatched braces in script"


def test_ps1_has_error_handling(ps1_content):
    """Scripts should have at least basic error handling."""
    has_error_action = "ErrorAction" in ps1_content
    has_try_catch = "try" in ps1_content.lower() and "catch" in ps1_content.lower()
    has_exit = "$LASTEXITCODE" in ps1_content or "exit" in ps1_content.lower()
    assert has_error_action or has_try_catch or has_exit, \
        "Script should have error handling (ErrorAction, try/catch, or exit code check)"


def test_ps1_no_hardcoded_paths(ps1_content):
    """Scripts should not have hardcoded user-specific paths."""
    # Check for C:\Users\<username>\ patterns (but allow generic examples)
    hardcoded = re.findall(r"C:\\Users\\[A-Za-z0-9]+\\", ps1_content)
    # Filter out documentation/comments
    for match in hardcoded:
        # Only flag if it's not in a comment
        for line in ps1_content.splitlines():
            stripped = line.strip()
            if match in line and not stripped.startswith("#"):
                pytest.fail(f"Hardcoded user path found: {match}")


# ── install-kern-service.ps1 specific ────────────────────────────────


def test_install_script_exists():
    script = SCRIPTS_DIR / "install-kern-service.ps1"
    if not script.exists():
        pytest.skip("install-kern-service.ps1 not found")
    content = script.read_text(encoding="utf-8")
    assert "nssm" in content.lower() or "pywin32" in content.lower(), \
        "Install script should reference nssm or pywin32 service installer"


def test_install_script_has_service_name():
    script = SCRIPTS_DIR / "install-kern-service.ps1"
    if not script.exists():
        pytest.skip("install-kern-service.ps1 not found")
    content = script.read_text(encoding="utf-8")
    assert "KERN" in content or "ServiceName" in content


# ── update-kern.ps1 specific ────────────────────────────────────────


def test_update_script_exists():
    script = SCRIPTS_DIR / "update-kern.ps1"
    if not script.exists():
        pytest.skip("update-kern.ps1 not found")
    content = script.read_text(encoding="utf-8")
    assert "git" in content.lower() or "pull" in content.lower() or "pip" in content.lower(), \
        "Update script should reference git or pip operations"
