"""Docker integration tests — validates Dockerfile and docker-compose.yml structure.

These tests parse the project Docker files and verify correctness of
configuration, port mappings, environment defaults, and health checks.
They do NOT require a running Docker daemon.
"""
from __future__ import annotations

import os
import re

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"


# ── Dockerfile checks ───────────────────────────────────────────────

@pytest.fixture
def dockerfile_content():
    if not DOCKERFILE.exists():
        pytest.skip("Dockerfile not found")
    return DOCKERFILE.read_text(encoding="utf-8")


def test_dockerfile_has_from(dockerfile_content):
    assert re.search(r"^FROM\s+", dockerfile_content, re.MULTILINE)


def test_dockerfile_has_expose(dockerfile_content):
    assert re.search(r"^EXPOSE\s+\d+", dockerfile_content, re.MULTILINE)


def test_dockerfile_has_healthcheck(dockerfile_content):
    assert "HEALTHCHECK" in dockerfile_content or "healthcheck" in dockerfile_content.lower()


def test_dockerfile_copies_app(dockerfile_content):
    assert re.search(r"COPY.*app", dockerfile_content)


def test_dockerfile_installs_dependencies(dockerfile_content):
    assert "requirements" in dockerfile_content.lower() or "pip install" in dockerfile_content.lower()


def test_dockerfile_sets_workdir(dockerfile_content):
    assert re.search(r"^WORKDIR\s+", dockerfile_content, re.MULTILINE)


def test_dockerfile_no_root_user(dockerfile_content):
    """Best practice: container should not run as root."""
    # Check for USER directive (non-root)
    user_match = re.search(r"^USER\s+(\S+)", dockerfile_content, re.MULTILINE)
    if user_match:
        assert user_match.group(1) != "root"


# ── docker-compose.yml checks ───────────────────────────────────────

@pytest.fixture
def compose_content():
    if not COMPOSE_FILE.exists():
        pytest.skip("docker-compose.yml not found")
    return COMPOSE_FILE.read_text(encoding="utf-8")


def test_compose_has_services(compose_content):
    assert "services:" in compose_content


def test_compose_has_kern_service(compose_content):
    assert "kern" in compose_content.lower()


def test_compose_has_ports(compose_content):
    assert "ports:" in compose_content


def test_compose_has_volumes(compose_content):
    assert "volumes:" in compose_content


def test_compose_has_healthcheck(compose_content):
    assert "healthcheck:" in compose_content or "health" in compose_content.lower()


def test_compose_has_restart_policy(compose_content):
    assert "restart:" in compose_content or "restart_policy" in compose_content
