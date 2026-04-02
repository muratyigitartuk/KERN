from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "app" / "config.py"
DOC_PATHS = [
    ROOT / "docs" / "deployment-checklist.md",
    ROOT / "docs" / "security-governance.md",
    ROOT / "docs" / "validation-pack.md",
    ROOT / "docs" / "windows-deployment.md",
    ROOT / "STAGING_VALIDATION_PLAN.md",
]
HEALTH_SEMANTIC_PATHS = DOC_PATHS + [ROOT / "tests" / "test_e2e_scaffold.py"]
_KERN_ENV_RE = re.compile(r"KERN_[A-Z0-9_]+")
_ALLOWED_NON_SETTINGS_VARS = {
    "KERN_E2E_BASE_URL",
    "KERN_E2E_ENABLED",
}


def _known_kern_env_vars() -> set[str]:
    source = CONFIG_PATH.read_text(encoding="utf-8")
    return set(_KERN_ENV_RE.findall(source))


def test_release_docs_only_reference_known_kern_env_vars() -> None:
    known = _known_kern_env_vars()
    missing: dict[str, list[str]] = {}
    for path in DOC_PATHS:
        referenced = sorted(set(_KERN_ENV_RE.findall(path.read_text(encoding="utf-8"))))
        unknown = [name for name in referenced if name not in known and name not in _ALLOWED_NON_SETTINGS_VARS]
        if unknown:
            missing[str(path.relative_to(ROOT))] = unknown
    assert missing == {}


def test_public_health_docs_do_not_use_stale_healthy_status() -> None:
    offenders: list[str] = []
    for path in HEALTH_SEMANTIC_PATHS:
        text = path.read_text(encoding="utf-8").lower()
        if "status=healthy" in text or '== "healthy"' in text or "healthy`" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_staging_plan_references_validation_pack_artifacts() -> None:
    text = (ROOT / "STAGING_VALIDATION_PLAN.md").read_text(encoding="utf-8")
    assert "validation pack" in text.lower()
    assert "output/playwright" in text
