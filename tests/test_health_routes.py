from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.database import connect
from app.platform import PlatformStore, connect_platform_db
from app.routes import _http_policy_gate, register_routes
from app.types import CapabilityDescriptor


class _NetworkStatus:
    def __init__(self, status: str = "ok", endpoints: list[str] | None = None) -> None:
        self.status = status
        self.endpoints = endpoints or []


def _build_runtime(tmp_path: Path, *, severity: str = "ok"):
    system_db = tmp_path / "kern-system.db"
    profile_root = tmp_path / "profiles"
    backup_root = tmp_path / "backups"
    platform = PlatformStore(connect_platform_db(system_db))
    profile = platform.ensure_default_profile(
        profile_root=profile_root,
        backup_root=backup_root,
        legacy_db_path=tmp_path / "legacy.db",
    )
    profile_connection = connect(Path(profile.db_path))
    network_state = "unmonitored" if severity == "warning" else "ok"
    runtime = SimpleNamespace(
        active_profile=profile,
        platform=platform,
        memory=SimpleNamespace(connection=profile_connection),
        orchestrator=SimpleNamespace(
            snapshot=SimpleNamespace(
                llm_available=False,
                model_info=SimpleNamespace(app_version="1.0.0-test"),
                background_components={},
                runtime_degraded_reasons=["audit chain mismatch"] if severity == "degraded" else [],
                policy_mode="corporate",
                product_posture="production",
                retention_policies={},
                retention_status={},
                last_monitor_tick_at=None,
            )
        ),
        network_monitor=SimpleNamespace(status=_NetworkStatus(status=network_state)),
        audit_chain_ok=severity != "degraded",
        audit_chain_reason=None if severity != "degraded" else "audit chain mismatch",
        last_audit_verification_at=None,
        scheduler_service=None,
        _pending_proactive_alerts=[],
        _using_locked_scaffold=severity == "warning",
    )
    return runtime, profile_connection, platform.connection


def _build_client(tmp_path: Path, *, severity: str = "ok") -> tuple[TestClient, object]:
    runtime, profile_connection, system_connection = _build_runtime(tmp_path, severity=severity)
    app = FastAPI()
    register_routes(app, lambda: runtime)
    client = TestClient(app)

    def _cleanup() -> None:
        client.close()
        profile_connection.close()
        system_connection.close()

    return client, _cleanup


def test_health_routes_report_ok_state(tmp_path: Path) -> None:
    client, cleanup = _build_client(tmp_path, severity="ok")
    try:
        health = client.get("/health")
        live = client.get("/health/live")
        ready = client.get("/health/ready")
    finally:
        cleanup()

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert live.status_code == 200
    assert live.json() == {"status": "live", "severity": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "severity": "ok"}


def test_health_routes_report_warning_state(tmp_path: Path) -> None:
    client, cleanup = _build_client(tmp_path, severity="warning")
    try:
        health = client.get("/health")
        live = client.get("/health/live")
        ready = client.get("/health/ready")
    finally:
        cleanup()

    assert health.status_code == 200
    assert health.json()["status"] == "warning"
    assert health.json()["using_locked_scaffold"] is True
    assert live.status_code == 200
    assert live.json() == {"status": "live", "severity": "warning"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "severity": "warning"}


def test_health_routes_report_degraded_state(tmp_path: Path) -> None:
    client, cleanup = _build_client(tmp_path, severity="degraded")
    try:
        health = client.get("/health")
        live = client.get("/health/live")
        ready = client.get("/health/ready")
    finally:
        cleanup()

    assert health.status_code == 503
    assert health.json()["status"] == "degraded"
    assert health.json()["runtime_degraded_reasons"] == ["audit chain mismatch"]
    assert live.status_code == 200
    assert live.json() == {"status": "live", "severity": "degraded"}
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "severity": "degraded"}


def test_http_policy_gate_denies_corporate_confirmation_and_records_audit() -> None:
    captured: list[tuple[str, str, str, str, dict | None]] = []
    runtime = SimpleNamespace(
        policy=SimpleNamespace(
            mode="corporate",
            decide_step=lambda step, descriptor=None: SimpleNamespace(
                verdict="confirm",
                message="Confirmation required for protected export.",
                policy_reason="always_confirm",
            ),
        ),
        platform=SimpleNamespace(
            record_audit=lambda category, action, status, message, **kwargs: captured.append(
                (category, action, status, message, kwargs.get("details"))
            )
        ),
        active_profile=SimpleNamespace(slug="default"),
        orchestrator=SimpleNamespace(capabilities=SimpleNamespace(get_descriptor=lambda name: None)),
    )

    with pytest.raises(HTTPException) as exc_info:
        _http_policy_gate(
            runtime,
            "export_logs",
            descriptor=CapabilityDescriptor(
                name="export_logs",
                title="Export runtime logs",
                summary="Exports runtime logs from the active profile.",
                domain="security",
                risk_level="high",
                confirmation_rule="always",
            ),
        )

    assert exc_info.value.status_code == 409
    assert captured == [
        ("policy", "http_export_logs", "warning", "Confirmation required for protected export.", {"verdict": "confirm", "reason": "always_confirm"})
    ]


def test_http_policy_gate_denies_personal_confirmation_and_records_audit() -> None:
    captured: list[tuple[str, str, str, str, dict | None]] = []
    runtime = SimpleNamespace(
        policy=SimpleNamespace(
            mode="personal",
            decide_step=lambda step, descriptor=None: SimpleNamespace(
                verdict="confirm",
                message="Confirmation required for protected export.",
                policy_reason="always_confirm",
            ),
        ),
        platform=SimpleNamespace(
            record_audit=lambda category, action, status, message, **kwargs: captured.append(
                (category, action, status, message, kwargs.get("details"))
            )
        ),
        active_profile=SimpleNamespace(slug="default"),
        orchestrator=SimpleNamespace(capabilities=SimpleNamespace(get_descriptor=lambda name: None)),
    )

    with pytest.raises(HTTPException) as exc_info:
        _http_policy_gate(
            runtime,
            "export_logs",
            descriptor=CapabilityDescriptor(
                name="export_logs",
                title="Export runtime logs",
                summary="Exports runtime logs from the active profile.",
                domain="security",
                risk_level="high",
                confirmation_rule="always",
            ),
        )

    assert exc_info.value.status_code == 409
    assert captured == [
        ("policy", "http_export_logs", "warning", "Confirmation required for protected export.", {"verdict": "confirm", "reason": "always_confirm"})
    ]


def test_http_policy_gate_allows_corporate_manual_upload_without_confirmation() -> None:
    runtime = SimpleNamespace(
        policy=SimpleNamespace(
            mode="corporate",
            decide_step=lambda step, descriptor=None: SimpleNamespace(
                verdict="allow",
                message="Safe to execute immediately.",
                policy_reason="allowed",
            ),
        ),
        platform=SimpleNamespace(record_audit=lambda *args, **kwargs: None),
        active_profile=SimpleNamespace(slug="default"),
        orchestrator=SimpleNamespace(capabilities=SimpleNamespace(get_descriptor=lambda name: None)),
    )

    _http_policy_gate(
        runtime,
        "upload_documents",
        descriptor=CapabilityDescriptor(
            name="upload_documents",
            title="Upload Documents",
            summary="Upload a small set of local documents into the active profile archive.",
            domain="documents",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
    )


def test_upload_route_rebuilds_retrieval_index_after_ingest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.routes.settings.rag_enabled", True)
    monkeypatch.setattr("app.routes.settings.vec_enabled", False)
    rebuild_calls: list[str] = []
    ingest_calls: list[list[str]] = []

    def _ingest_batch(file_paths, **kwargs):
        ingest_calls.append([str(path) for path in file_paths])
        return [SimpleNamespace(id="doc-1", title="acme_offer", category="offer")]

    runtime = SimpleNamespace(
        active_profile=SimpleNamespace(slug="default"),
        platform=SimpleNamespace(
            is_profile_locked=lambda slug: False,
            record_audit=lambda *args, **kwargs: None,
        ),
        policy=SimpleNamespace(
            mode="corporate",
            decide_step=lambda step, descriptor=None: SimpleNamespace(
                verdict="allow",
                message="ok",
                policy_reason="allowed",
            ),
        ),
        orchestrator=SimpleNamespace(
            capabilities=SimpleNamespace(get_descriptor=lambda name: None),
            snapshot=SimpleNamespace(
                llm_available=False,
                model_info=SimpleNamespace(app_version="1.0.0-test"),
                background_components={},
                runtime_degraded_reasons=[],
                policy_mode="corporate",
                product_posture="production",
                retention_policies={},
                retention_status={},
                last_monitor_tick_at=None,
            ),
        ),
        network_monitor=SimpleNamespace(status=_NetworkStatus(status="ok")),
        audit_chain_ok=True,
        audit_chain_reason=None,
        last_audit_verification_at=None,
        scheduler_service=None,
        _pending_proactive_alerts=[],
        _using_locked_scaffold=False,
        document_service=SimpleNamespace(ingest_batch=_ingest_batch),
        retrieval_service=SimpleNamespace(rebuild_index=lambda scope: rebuild_calls.append(scope)),
        local_data=SimpleNamespace(memory_scope=lambda: "profile_plus_archive"),
    )
    app = FastAPI()
    register_routes(app, lambda: runtime)
    client = TestClient(app)
    try:
        response = client.post(
            "/upload",
            files=[("files", ("acme_offer.txt", b"ACME GmbH Offer\nTarget amount: 4850 EUR", "text/plain"))],
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert ingest_calls
    assert rebuild_calls == ["profile_plus_archive"]
