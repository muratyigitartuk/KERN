from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.compliance import ComplianceService
from app.database import connect
from app.intelligence import IntelligenceService
from app.memory import MemoryRepository
from app.platform import PlatformStore, connect_platform_db
from app.retrieval import RetrievalService
from app.routes import register_routes
from app.types import DocumentChunk, DocumentRecord


def _build_runtime(tmp_path: Path):
    system_db = tmp_path / "kern-system.db"
    profile_root = tmp_path / "profiles"
    backup_root = tmp_path / "backups"
    platform = PlatformStore(connect_platform_db(system_db))
    profile = platform.ensure_default_profile(
        profile_root=profile_root,
        backup_root=backup_root,
        legacy_db_path=tmp_path / "legacy.db",
        title="Primary workspace",
        slug="default",
    )
    memory = MemoryRepository(connect(Path(profile.db_path)), profile_slug=profile.slug)
    runtime = SimpleNamespace(
        platform=platform,
        active_profile=profile,
        memory=memory,
        retrieval_service=RetrievalService(memory, platform=platform, profile_slug=profile.slug),
    )
    return runtime


def _client_for_runtime(runtime) -> TestClient:
    app = FastAPI()
    app.state.platform = runtime.platform
    register_routes(app, lambda: runtime)
    return TestClient(app)


def _create_active_user(runtime, *, email: str = "owner@example.com", role: str = "org_owner") -> str:
    org = runtime.platform.ensure_default_organization()
    user = runtime.platform.create_user(
        email=email,
        display_name="Owner",
        organization_id=org.id,
        status="active",
    )
    runtime.platform.upsert_workspace_membership(user_id=user.id, workspace_slug=runtime.active_profile.slug, role=role)
    return SimpleNamespace(
        organization_id=org.id,
        user_id=user.id,
        workspace_slug=runtime.active_profile.slug,
        roles=[role],
    )


def test_compliance_service_generates_evidence_manifest_for_user_export(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    context = _create_active_user(runtime)
    service = ComplianceService(runtime.platform, runtime.memory, runtime.active_profile)
    runtime.memory.connection.execute(
        """
        INSERT INTO structured_memory_items (
            id, profile_slug, organization_id, workspace_slug, user_id, memory_kind, key, value, source,
            confidence, status, provenance_json, data_class, promotion_state, approved_count, rejected_count,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'fact', 'preferred_term', 'zahlbar in 14 tagen', 'user', 0.9, 'active', '{}', 'operational', 'none', 0, 0, datetime('now'), datetime('now'))
        """,
        (str(uuid4()), runtime.active_profile.slug, context.organization_id, runtime.active_profile.slug, context.user_id),
    )
    runtime.memory.connection.commit()

    export_record, payload = service.export_user_bundle(actor_user_id=context.user_id, target_user_id=context.user_id)

    assert export_record.manifest is not None
    assert export_record.artifact_path is not None
    assert Path(export_record.artifact_path).exists()
    assert "structured_memory" in payload
    assert export_record.manifest.included_datasets


def test_compliance_service_blocks_erasure_when_legal_hold_active(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    context = _create_active_user(runtime)
    target = runtime.platform.create_user(
        email="subject@example.com",
        display_name="Subject",
        organization_id=context.organization_id,
        status="active",
    )
    runtime.platform.create_legal_hold(
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        target_user_id=target.id,
        reason="Tax retention",
    )
    request = runtime.platform.create_erasure_request(
        organization_id=context.organization_id,
        target_user_id=target.id,
        requested_by_user_id=context.user_id,
        workspace_slug=runtime.active_profile.slug,
        reason="GDPR request",
    )
    service = ComplianceService(runtime.platform, runtime.memory, runtime.active_profile)

    result = service.execute_erasure(request.id, actor_user_id=context.user_id)

    assert result["status"] == "blocked"
    refreshed = runtime.platform.get_erasure_request(request.id)
    assert refreshed is not None
    assert refreshed.status == "blocked"


def test_compliance_service_executes_erasure_and_records_tombstones(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    context = _create_active_user(runtime)
    target = runtime.platform.create_user(
        email="subject@example.com",
        display_name="Subject",
        organization_id=context.organization_id,
        status="active",
    )
    runtime.platform.upsert_workspace_membership(user_id=target.id, workspace_slug=runtime.active_profile.slug, role="member")
    intelligence = IntelligenceService(runtime.platform, runtime.memory, runtime.active_profile)
    intelligence.record_training_example(
        actor_user_id=target.id,
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        source_type="draft",
        source_id="draft-1",
        input_text="input",
        output_text="output",
        status="approved",
        approved_for_training=True,
    )
    runtime.memory.connection.execute(
        """
        INSERT INTO structured_memory_items (
            id, profile_slug, organization_id, workspace_slug, user_id, memory_kind, key, value, source,
            confidence, status, provenance_json, data_class, promotion_state, approved_count, rejected_count,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'fact', 'customer_note', 'private preference', 'user', 0.9, 'active', '{}', 'personal', 'none', 0, 0, datetime('now'), datetime('now'))
        """,
        (str(uuid4()), runtime.active_profile.slug, context.organization_id, runtime.active_profile.slug, target.id),
    )
    runtime.memory.connection.commit()
    request = runtime.platform.create_erasure_request(
        organization_id=context.organization_id,
        target_user_id=target.id,
        requested_by_user_id=context.user_id,
        workspace_slug=runtime.active_profile.slug,
        reason="GDPR request",
    )
    service = ComplianceService(runtime.platform, runtime.memory, runtime.active_profile)

    result = service.execute_erasure(request.id, actor_user_id=context.user_id)

    assert result["status"] == "completed"
    refreshed_user = runtime.platform.get_user(target.id)
    assert refreshed_user is not None
    assert refreshed_user.status == "deleted"
    tombstones = runtime.platform.list_deletion_tombstones(context.organization_id, target_user_id=target.id)
    assert tombstones


def test_intelligence_feedback_updates_memory_ranking(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    context = _create_active_user(runtime)
    memory_item_id = str(uuid4())
    runtime.memory.connection.execute(
        """
        INSERT INTO structured_memory_items (
            id, profile_slug, organization_id, workspace_slug, user_id, memory_kind, key, value, source,
            confidence, status, provenance_json, data_class, promotion_state, approved_count, rejected_count,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'fact', 'payment_terms', 'zahlbar in 14 tagen', 'user', 0.7, 'active', '{}', 'operational', 'none', 0, 0, datetime('now'), datetime('now'))
        """,
        (memory_item_id, runtime.active_profile.slug, context.organization_id, runtime.active_profile.slug, context.user_id),
    )
    runtime.memory.connection.commit()
    service = IntelligenceService(runtime.platform, runtime.memory, runtime.active_profile)

    service.capture_feedback(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        signal_type="use_again",
        source_type="memory",
        source_id="payment_terms",
        memory_item_id=memory_item_id,
        approved_for_training=True,
    )
    hits = service.retrieve_memory_context(
        "zahlbar",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        user_id=context.user_id,
    )

    assert hits
    assert hits[0]["approved_count"] >= 1
    assert hits[0]["provenance"]["policy_safe"] is True


def test_routes_expose_data_inventory_and_generate_exports(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    context = _create_active_user(runtime)
    client = _client_for_runtime(runtime)
    intelligence = IntelligenceService(runtime.platform, runtime.memory, runtime.active_profile)
    intelligence.record_training_example(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        source_type="draft",
        source_id="draft-2",
        input_text="hello",
        output_text="world",
        status="approved",
        approved_for_training=True,
    )

    inventory = client.get("/compliance/data-inventory")
    export_response = client.post(f"/compliance/exports/user/{context.user_id}/generate")
    training = client.post("/intelligence/training-exports", json={"workspace_slug": runtime.active_profile.slug})

    assert inventory.status_code == 200
    assert "documents" in inventory.json()["inventory"]
    assert export_response.status_code == 200
    assert export_response.json()["export"]["manifest"] is not None
    assert training.status_code == 200
    assert training.json()["dataset"]["train_count"] >= 1


def test_routes_finalize_regulated_document_and_list_versions(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    context = _create_active_user(runtime)
    client = _client_for_runtime(runtime)
    record = DocumentRecord(
        id="doc-1",
        profile_slug=runtime.active_profile.slug,
        organization_id=context.organization_id,
        workspace_id=runtime.active_profile.workspace_id,
        actor_user_id=context.user_id,
        title="Invoice",
        source="manual",
        file_type="txt",
        file_path=str(tmp_path / "invoice.txt"),
        category="finance",
        classification="finance",
        data_class="regulated_business",
        retention_state="standard",
    )
    runtime.memory.upsert_document_record(
        record,
        chunks=[DocumentChunk(document_id=record.id, chunk_index=0, text="Invoice content", metadata={})],
        metadata={"classification": "finance", "data_class": "regulated_business"},
    )

    finalized = client.post("/compliance/regulated-documents/finalize", json={"document_id": "doc-1", "title": "Invoice"})
    regulated_id = finalized.json()["regulated_document"]["id"]
    versions = client.get(f"/compliance/regulated-documents/{regulated_id}/versions")

    assert finalized.status_code == 200
    assert finalized.json()["regulated_document"]["immutability_state"] == "finalized"
    assert versions.status_code == 200
    assert len(versions.json()["versions"]) == 1


def test_retrieval_hits_include_provenance_metadata(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.memory.remember_memory_item("payment_terms", "zahlbar in 14 tagen", source="user")

    hits = runtime.retrieval_service.retrieve("zahlbar", scope="profile", limit=5)

    assert hits
    assert hits[0].provenance
    assert hits[0].provenance["scope"] == "profile"
