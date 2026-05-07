from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "app" / "config.py"
DOC_PATHS = [
    ROOT / "README.md",
    ROOT / "docs" / "deployment-checklist.md",
    ROOT / "docs" / "security-governance.md",
    ROOT / "docs" / "validation-pack.md",
    ROOT / "docs" / "windows-deployment.md",
    ROOT / "docs" / "corporate-demo-script.md",
]
HEALTH_SEMANTIC_PATHS = DOC_PATHS
_KERN_ENV_RE = re.compile(r"KERN_[A-Z0-9_]+")
_ALLOWED_NON_SETTINGS_VARS: set[str] = set()


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


def test_corporate_demo_script_references_release_and_acceptance_artifacts() -> None:
    text = (ROOT / "docs" / "corporate-demo-script.md").read_text(encoding="utf-8").lower()
    assert "release gate" in text
    assert "enterprise acceptance" in text
    assert "support" in text


def test_final_enterprise_name_is_explicitly_reserved() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    final_name = "KERN Enterprise Workspace: a single-tenant, company-controlled document AI workspace for governed internal knowledge work."
    assert final_name in readme
    assert "reserved final enterprise name" in readme.lower()
    assert "Do not describe this release as fully enterprise-scale" in readme


def test_public_docs_do_not_overclaim_enterprise_scale() -> None:
    offenders: list[str] = []
    overclaims = (
        "fully enterprise-scale",
        "final enterprise product",
        "production enterprise workspace",
    )
    for path in DOC_PATHS:
        text = path.read_text(encoding="utf-8").lower()
        if path.name == "README.md":
            continue
        if any(claim in text for claim in overclaims):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
