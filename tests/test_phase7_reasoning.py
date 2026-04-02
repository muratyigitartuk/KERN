from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import connect
from app.identity import IdentityService
from app.intelligence import IntelligenceService
from app.memory import MemoryRepository
from app.platform import PlatformStore, connect_platform_db
from app.reasoning import ReasoningService
from app.routes import register_routes
from app.runtime import KernRuntime
from app.scheduler import SchedulerService
from app.types import AssistantTurn, DocumentChunk, DocumentRecord, EmailDraft, EmailMessage, SuggestedDraftRecord


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
    identity = IdentityService(platform)
    scheduler = SchedulerService(memory.connection, profile.slug)
    runtime = SimpleNamespace(
        platform=platform,
        active_profile=profile,
        identity_service=identity,
        memory=memory,
        scheduler_service=scheduler,
    )
    return runtime


def _client_for_runtime(runtime) -> TestClient:
    app = FastAPI()
    app.state.platform = runtime.platform
    app.state.identity_service = runtime.identity_service
    register_routes(app, lambda: runtime)
    return TestClient(app)


def _create_active_user(runtime, *, email: str = "owner@example.com", role: str = "org_owner") -> str:
    org = runtime.platform.ensure_default_organization()
    user = runtime.platform.create_user(
        email=email,
        display_name="Owner",
        organization_id=org.id,
        auth_source="bootstrap",
        status="active",
    )
    runtime.platform.upsert_workspace_membership(user_id=user.id, workspace_slug=runtime.active_profile.slug, role=role)
    session = runtime.platform.create_session(
        organization_id=org.id,
        user_id=user.id,
        workspace_slug=runtime.active_profile.slug,
        auth_method="oidc",
    )
    return session.id


def test_reasoning_service_builds_workflow_state_without_llm(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    other_user = runtime.platform.create_user(
        email="other@example.com",
        display_name="Other",
        organization_id=context.organization_id,
        auth_source="oidc",
        status="active",
    )
    runtime.platform.upsert_workspace_membership(user_id=other_user.id, workspace_slug=runtime.active_profile.slug, role="member")
    runtime.memory.connection.execute(
        """
        INSERT INTO structured_memory_items (
            id, profile_slug, organization_id, workspace_slug, user_id, memory_kind, key, value, source,
            confidence, status, provenance_json, data_class, promotion_state, approved_count, rejected_count,
            created_at, updated_at
        ) VALUES
            (?, ?, ?, ?, ?, 'fact', 'payment_terms', 'zahlbar in 14 tagen', 'user', 0.8, 'active', '{}', 'operational', 'candidate', 1, 0, datetime('now'), datetime('now')),
            (?, ?, ?, ?, ?, 'fact', 'payment_terms', 'zahlbar in 30 tagen', 'user', 0.7, 'active', '{}', 'operational', 'candidate', 0, 0, datetime('now'), datetime('now')),
            (?, ?, ?, ?, ?, 'fact', 'other_private_pref', 'confidential', 'user', 0.9, 'active', '{}', 'personal', 'candidate', 2, 0, datetime('now'), datetime('now'))
        """,
        (
            str(uuid4()),
            runtime.active_profile.slug,
            context.organization_id,
            runtime.active_profile.slug,
            context.user_id,
            str(uuid4()),
            runtime.active_profile.slug,
            context.organization_id,
            runtime.active_profile.slug,
            context.user_id,
            str(uuid4()),
            runtime.active_profile.slug,
            context.organization_id,
            runtime.active_profile.slug,
            other_user.id,
        ),
    )
    runtime.memory.connection.commit()
    intelligence = IntelligenceService(runtime.platform, runtime.memory, runtime.active_profile)
    intelligence.record_training_example(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        source_type="draft",
        source_id="draft-1",
        input_text="hello",
        output_text="world",
        status="candidate",
        approved_for_training=False,
    )
    record = DocumentRecord(
        id="regulated-doc-1",
        profile_slug=runtime.active_profile.slug,
        organization_id=context.organization_id,
        workspace_id=runtime.active_profile.workspace_id,
        actor_user_id=context.user_id,
        title="Invoice April",
        source="manual",
        file_type="txt",
        file_path=str(tmp_path / "invoice.txt"),
        category="finance",
        classification="finance",
        data_class="regulated_business",
    )
    runtime.memory.upsert_document_record(
        record,
        chunks=[DocumentChunk(document_id=record.id, chunk_index=0, text="Invoice content", metadata={})],
        metadata={"classification": "finance", "data_class": "regulated_business"},
    )
    runtime.memory.save_email_draft(
        EmailDraft(to=["client@example.com"], subject="Follow-up", body="Draft body", status="draft"),
    )
    runtime.scheduler_service.create_task(
        title="Weekly reminder",
        cron_expression="0 9 * * 1",
        action_type="generate_report",
        action_payload={"kind": "summary"},
    )
    target = runtime.platform.create_user(
        email="subject@example.com",
        display_name="Subject",
        organization_id=context.organization_id,
        auth_source="oidc",
        status="active",
    )
    runtime.platform.create_legal_hold(
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        target_user_id=target.id,
        reason="Retention hold",
    )
    runtime.platform.create_erasure_request(
        organization_id=context.organization_id,
        target_user_id=target.id,
        requested_by_user_id=context.user_id,
        workspace_slug=runtime.active_profile.slug,
        reason="GDPR request",
    )

    service = ReasoningService(
        runtime.platform,
        runtime.memory,
        runtime.active_profile,
        scheduler_service=runtime.scheduler_service,
    )

    snapshot = service.world_state(
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    recommendations = service.list_recommendations(
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )

    workflow_types = {item.workflow_type for item in snapshot.workflows}
    assert "review_approval_queue" in workflow_types
    assert "compliance_export_erasure" in workflow_types
    assert "regulated_document_lifecycle" in workflow_types
    assert "correspondence_follow_up" in workflow_types
    assert "scheduling_follow_through" in workflow_types
    assert recommendations
    assert all(item.reasoning_source == "system_decision" for item in recommendations)
    assert any(item.recommendation_type == "review_candidate" for item in recommendations)
    assert any(item.recommendation_type == "suggested_draft" for item in recommendations)
    review_recommendation = next(item for item in recommendations if item.recommendation_type == "review_candidate")
    assert review_recommendation.blocking_conditions
    assert "Conflicting facts" in review_recommendation.blocking_conditions[0]
    assert review_recommendation.readiness_status == "blocked"
    assert review_recommendation.missing_inputs
    assert review_recommendation.readiness_nodes
    assert review_recommendation.readiness_edges
    assert review_recommendation.generation_contract.mode in {"clarify", "explain_only"}
    assert review_recommendation.evidence_bundle.claims
    assert review_recommendation.evidence_bundle.coverage_score <= 1.0
    assert review_recommendation.evidence_bundle.event_refs
    assert all(source_ref != other_user.id for source_ref in review_recommendation.evidence_bundle.source_refs)


def test_reasoning_routes_expose_structured_state_and_evidence(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    runtime.memory.connection.execute(
        """
        INSERT INTO structured_memory_items (
            id, profile_slug, organization_id, workspace_slug, user_id, memory_kind, key, value, source,
            confidence, status, provenance_json, data_class, promotion_state, approved_count, rejected_count,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'fact', 'reply_style', 'kurz und formal', 'user', 0.9, 'active', '{}', 'operational', 'candidate', 1, 0, datetime('now'), datetime('now'))
        """,
        (str(uuid4()), runtime.active_profile.slug, context.organization_id, runtime.active_profile.slug, context.user_id),
    )
    runtime.memory.connection.commit()
    runtime.memory.save_email_draft(
        EmailDraft(to=["client@example.com"], subject="Follow-up", body="Prepared", status="draft"),
    )
    IntelligenceService(runtime.platform, runtime.memory, runtime.active_profile).record_training_example(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        source_type="draft",
        source_id="draft-2",
        input_text="request",
        output_text="response",
        status="candidate",
        approved_for_training=False,
    )
    client = _client_for_runtime(runtime)
    client.cookies.set("kern_session", session_id)

    world_state = client.get("/intelligence/world-state")
    workbench = client.get("/intelligence/workbench")
    workflows = client.get("/intelligence/workflows")
    obligations = client.get("/intelligence/obligations")
    recommendations = client.get("/intelligence/recommendations")
    focus_hints = client.get("/intelligence/focus-hints")
    decisions = client.get("/intelligence/decisions")

    assert world_state.status_code == 200
    assert world_state.json()["world_state"]["workflow_count"] >= 1
    assert workbench.status_code == 200
    assert workbench.json()["workbench"]["recommendations"]
    assert workbench.json()["workbench"]["world_state"]["obligations"]
    assert workflows.status_code == 200
    assert workflows.json()["workflows"]
    assert obligations.status_code == 200
    assert recommendations.status_code == 200
    recommendation = recommendations.json()["recommendations"][0]
    assert recommendation["reasoning_source"] == "system_decision"
    assert recommendation["ranking_explanation"]["reasons"]
    assert recommendation["evidence_bundle"]["why_selected"]
    assert recommendation["evidence_bundle"]["claims"]
    assert "coverage_score" in recommendation["evidence_bundle"]
    assert "negative_evidence" in recommendation["evidence_bundle"]
    assert recommendation["readiness_status"]
    assert recommendation["readiness_nodes"]
    assert recommendation["generation_contract"]["mode"]
    assert decisions.status_code == 200
    assert focus_hints.status_code == 200
    assert focus_hints.json()["focus_hints"]

    recommendation_detail = client.get(f"/intelligence/recommendations/{recommendation['id']}")
    preparation_detail = client.get(f"/intelligence/preparation/{recommendation['id']}")
    preparation_from_query = client.get("/intelligence/preparation", params={"query": "What should I follow up on next?"})
    draft_detail = client.post(f"/intelligence/preparation/{recommendation['id']}/draft")
    evidence_detail = client.get(f"/intelligence/evidence/{recommendation['evidence_bundle']['id']}")
    workflow_detail = client.get(f"/intelligence/workflows/{recommendation['workflow_id']}")

    assert recommendation_detail.status_code == 200
    assert recommendation_detail.json()["recommendation"]["recommendation_type"] == recommendation["recommendation_type"]
    assert preparation_detail.status_code == 200
    assert preparation_detail.json()["preparation_packet"]["recommendation_id"] == recommendation["id"]
    assert preparation_detail.json()["preparation_packet"]["readiness_nodes"]
    assert "generation_contract" in preparation_detail.json()["preparation_packet"]
    assert preparation_from_query.status_code == 200
    assert preparation_from_query.json()["preparation_packet"]["evidence_pack"]["items"]
    assert preparation_from_query.json()["preparation_packet"]["evidence_pack"]["claims"]
    if recommendation["recommendation_type"] in {"suggested_draft", "follow_up_candidate"}:
        if recommendation["generation_contract"]["allow_draft"]:
            assert draft_detail.status_code == 200
            assert draft_detail.json()["suggested_draft"]["body"]
        else:
            assert draft_detail.status_code == 409
    else:
        assert draft_detail.status_code == 409
    assert evidence_detail.status_code == 200
    assert evidence_detail.json()["evidence_bundle"]["id"] == recommendation["evidence_bundle"]["id"]
    assert workflow_detail.json()["domain_events"]
    assert workflow_detail.status_code == 200


def test_document_query_routes_expose_grounded_packets(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    document = DocumentRecord(
        id=str(uuid4()),
        profile_slug=runtime.active_profile.slug,
        organization_id=context.organization_id,
        workspace_id=runtime.active_profile.workspace_id,
        actor_user_id=context.user_id,
        title="deep-research-report",
        source="upload",
        file_type="md",
        file_path=str(tmp_path / "deep-research-report.md"),
        category="research",
        classification="internal",
        data_class="operational",
        tags=["research"],
    )
    runtime.memory.upsert_document_record(
        document,
        chunks=[
            DocumentChunk(
                document_id=document.id,
                chunk_index=0,
                text="# Risks\nThe main risk is delayed payment and a contractual penalty if the deadline slips.",
            ),
            DocumentChunk(
                document_id=document.id,
                chunk_index=1,
                text="# Important sections\nThe report highlights implementation risks, timeline pressure, and follow-up requirements.",
            ),
        ],
        metadata={"classification": "internal", "data_class": "operational"},
    )
    client = _client_for_runtime(runtime)
    client.cookies.set("kern_session", session_id)

    packet_response = client.post("/intelligence/document-query", json={"query": "cite this PDF's important sections"})
    assert packet_response.status_code == 200
    packet = packet_response.json()["document_answer_packet"]
    assert packet["task_intent"]["task_family"] == "document_citation"
    assert packet["selected_document_ids"] == [document.id]
    assert packet["citations"]
    assert packet["evidence_pack"]["claims"]
    assert "coverage_score" in packet["evidence_pack"]
    assert packet["generation_contract"]["mode"] == "cite"

    detail_response = client.get(f"/intelligence/document-query/{packet['id']}")
    assert detail_response.status_code == 200
    assert detail_response.json()["document_answer_packet"]["id"] == packet["id"]


def test_document_query_marks_ambiguous_freeform_document_requests_as_waiting(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    for name in ("alpha-report", "beta-report"):
        record = DocumentRecord(
            id=str(uuid4()),
            profile_slug=runtime.active_profile.slug,
            organization_id=context.organization_id,
            workspace_id=runtime.active_profile.workspace_id,
            actor_user_id=context.user_id,
            title=name,
            source="upload",
            file_type="md",
            file_path=str(tmp_path / f"{name}.md"),
            category="research",
            classification="internal",
            data_class="operational",
            tags=["research"],
        )
        runtime.memory.upsert_document_record(
            record,
            chunks=[DocumentChunk(document_id=record.id, chunk_index=0, text=f"# {name}\nThis is a local report.")],
            metadata={"classification": "internal", "data_class": "operational"},
        )
    service = ReasoningService(runtime.platform, runtime.memory, runtime.active_profile, scheduler_service=runtime.scheduler_service)
    packet = service.get_document_answer_packet_for_transcript(
        "summarize this PDF",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    assert packet is not None
    assert packet.task_intent.task_family == "clarification_needed"
    assert packet.readiness_status == "waiting_on_input"
    assert packet.missing_inputs


def test_document_query_does_not_hijack_generic_summary_requests_from_recent_docs(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    record = DocumentRecord(
        id=str(uuid4()),
        profile_slug=runtime.active_profile.slug,
        organization_id=context.organization_id,
        workspace_id=runtime.active_profile.workspace_id,
        actor_user_id=context.user_id,
        title="research-notes",
        source="upload",
        file_type="md",
        file_path=str(tmp_path / "research-notes.md"),
        category="research",
        classification="internal",
        data_class="operational",
        tags=["research"],
    )
    runtime.memory.upsert_document_record(
        record,
        chunks=[DocumentChunk(document_id=record.id, chunk_index=0, text="# Notes\nThis is a local research note.")],
        metadata={"classification": "internal", "data_class": "operational"},
    )
    service = ReasoningService(runtime.platform, runtime.memory, runtime.active_profile, scheduler_service=runtime.scheduler_service)
    intent = service.classify_task_intent_for_transcript(
        "summarize the meeting status",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    assert intent.task_family == "general_chat_fallback"


def test_document_query_keeps_multiple_title_matches_in_clarification_state(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    for title in ("alpha contract report", "beta contract report"):
        record = DocumentRecord(
            id=str(uuid4()),
            profile_slug=runtime.active_profile.slug,
            organization_id=context.organization_id,
            workspace_id=runtime.active_profile.workspace_id,
            actor_user_id=context.user_id,
            title=title,
            source="upload",
            file_type="md",
            file_path=str(tmp_path / f"{title.replace(' ', '-')}.md"),
            category="research",
            classification="internal",
            data_class="operational",
            tags=["research"],
        )
        runtime.memory.upsert_document_record(
            record,
            chunks=[DocumentChunk(document_id=record.id, chunk_index=0, text=f"# {title}\nContract summary and obligations.")],
            metadata={"classification": "internal", "data_class": "operational"},
        )
    service = ReasoningService(runtime.platform, runtime.memory, runtime.active_profile, scheduler_service=runtime.scheduler_service)
    packet = service.get_document_answer_packet_for_transcript(
        "summarize the contract report",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    assert packet is not None
    assert packet.task_intent.task_family == "clarification_needed"
    assert packet.readiness_status == "waiting_on_input"
    assert not packet.selected_document_ids


def test_document_compare_requires_grounded_support_from_both_sides(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    documents: list[DocumentRecord] = []
    specs = [
        ("alpha proposal", "The payment terms are net 14 days and the delivery deadline is April 20."),
        ("beta proposal", "This document only covers branding concepts and color ideas."),
    ]
    for title, body in specs:
        record = DocumentRecord(
            id=str(uuid4()),
            profile_slug=runtime.active_profile.slug,
            organization_id=context.organization_id,
            workspace_id=runtime.active_profile.workspace_id,
            actor_user_id=context.user_id,
            title=title,
            source="upload",
            file_type="md",
            file_path=str(tmp_path / f"{title.replace(' ', '-')}.md"),
            category="research",
            classification="internal",
            data_class="operational",
            tags=["research"],
        )
        runtime.memory.upsert_document_record(
            record,
            chunks=[DocumentChunk(document_id=record.id, chunk_index=0, text=body)],
            metadata={"classification": "internal", "data_class": "operational"},
        )
        documents.append(record)
    service = ReasoningService(runtime.platform, runtime.memory, runtime.active_profile, scheduler_service=runtime.scheduler_service)
    packet = service.get_document_answer_packet_for_transcript(
        "compare alpha proposal and beta proposal payment terms",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    assert packet is not None
    assert packet.task_intent.task_family == "document_compare"
    assert packet.readiness_status == "waiting_on_input"
    assert any("grounded comparison support" in item.expected_signal for item in packet.evidence_pack.negative_evidence)
    assert any(claim.label == "Both comparison sides have grounded support" and claim.status == "missing" for claim in packet.evidence_pack.claims)


def test_freeform_route_uses_thread_context_before_generic_chat(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    runtime.memory.append_mailbox_message(
        EmailMessage(
            id="mail-1",
            subject="Quarterly follow-up",
            sender="client@example.com",
            recipients=["owner@example.com"],
            received_at=datetime.now(timezone.utc),
            folder="INBOX",
        ),
        body_text="Can you send the updated payment timeline?",
    )
    runtime.memory.append_mailbox_message(
        EmailMessage(
            id="mail-2",
            subject="Quarterly follow-up",
            sender="owner@example.com",
            recipients=["client@example.com"],
            received_at=datetime.now(timezone.utc),
            folder="SENT",
        ),
        body_text="We will send the updated timeline tomorrow.",
    )
    runtime.memory.save_email_draft(
        EmailDraft(to=["client@example.com"], subject="Quarterly follow-up", body="Draft reply", status="draft"),
    )
    service = ReasoningService(runtime.platform, runtime.memory, runtime.active_profile, scheduler_service=runtime.scheduler_service)
    routed = service.route_freeform_for_transcript(
        "what did we tell this client last time",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    assert routed["packet_type"] == "thread_context_packet"
    packet = routed["packet"]
    assert packet is not None
    assert packet.task_intent.task_family == "thread_qa"
    assert packet.readiness_status == "ready_now"
    assert packet.thread_refs


def test_thread_context_packet_excludes_policy_unsafe_memory(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    runtime.memory.append_mailbox_message(
        EmailMessage(
            id="mail-thread-safe",
            subject="Quarterly follow-up",
            sender="client@example.com",
            recipients=["owner@example.com"],
            received_at=datetime.now(timezone.utc),
            folder="INBOX",
        ),
        body_text="Thread update",
    )
    runtime.memory.connection.execute(
        """
        INSERT INTO structured_memory_items (
            id, profile_slug, organization_id, workspace_slug, user_id, memory_kind, key, value, source,
            confidence, status, provenance_json, data_class, promotion_state, approved_count, rejected_count,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, NULL, 'fact', 'thread_note', 'sensitive personal thread note', 'user', 0.9, 'active', '{}', 'personal', 'candidate', 1, 0, datetime('now'), datetime('now'))
        """,
        (str(uuid4()), runtime.active_profile.slug, context.organization_id, runtime.active_profile.slug),
    )
    runtime.memory.connection.commit()
    service = ReasoningService(runtime.platform, runtime.memory, runtime.active_profile, scheduler_service=runtime.scheduler_service)
    packet = service.get_thread_context_packet_for_transcript(
        "what did we tell this client last time",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    assert packet is not None
    assert all(item["reason"] != "sensitive personal thread note" for item in packet.evidence_pack.items)


def test_freeform_route_uses_person_context_for_customer_question(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    runtime.memory.append_mailbox_message(
        EmailMessage(
            id="mail-3",
            subject="Invoice question",
            sender="supplier@example.com",
            recipients=["owner@example.com"],
            received_at=datetime.now(timezone.utc),
            folder="INBOX",
        ),
        body_text="Please confirm the invoice date.",
    )
    runtime.memory.save_email_draft(
        EmailDraft(to=["supplier@example.com"], subject="Invoice question", body="Reply draft", status="draft"),
    )
    service = ReasoningService(runtime.platform, runtime.memory, runtime.active_profile, scheduler_service=runtime.scheduler_service)
    routed = service.route_freeform_for_transcript(
        "what matters for this supplier right now",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    assert routed["packet_type"] == "person_context_packet"
    packet = routed["packet"]
    assert packet is not None
    assert packet.task_intent.task_family == "person_context"
    assert packet.person_ref == "supplier@example.com"
    assert packet.evidence_pack.claims


def test_freeform_route_keeps_ambiguous_person_questions_in_clarification(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    for idx, address in enumerate(("alice@example.com", "alice@vendor.example"), start=1):
        runtime.memory.append_mailbox_message(
            EmailMessage(
                id=f"mail-a{idx}",
                subject="Account review",
                sender=address,
                recipients=["owner@example.com"],
                received_at=datetime.now(timezone.utc),
                folder="INBOX",
            ),
            body_text="Review request",
        )
    service = ReasoningService(runtime.platform, runtime.memory, runtime.active_profile, scheduler_service=runtime.scheduler_service)
    routed = service.route_freeform_for_transcript(
        "what matters for alice right now",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    assert routed["task_intent"].task_family == "clarification_needed"
    assert routed["packet_type"] == "person_context_packet"
    packet = routed["packet"]
    assert packet is not None
    assert packet.readiness_status == "waiting_on_input"
    assert packet.missing_inputs


def test_freeform_and_context_routes_expose_packets(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    runtime.memory.append_mailbox_message(
        EmailMessage(
            id="mail-ctx",
            subject="Supplier update",
            sender="supplier@example.com",
            recipients=["owner@example.com"],
            received_at=datetime.now(timezone.utc),
            folder="INBOX",
        ),
        body_text="Latest update",
    )
    client = _client_for_runtime(runtime)
    client.cookies.set("kern_session", session_id)
    freeform = client.post("/intelligence/freeform-route", json={"query": "what matters for this supplier right now"})
    thread_packet = client.post("/intelligence/thread-context", json={"query": "what did we tell this supplier last time"})
    person_packet = client.post("/intelligence/person-context", json={"query": "what matters for this supplier right now"})
    assert freeform.status_code == 200
    assert freeform.json()["freeform_route"]["task_intent"]["task_family"] in {"person_context", "clarification_needed"}
    assert thread_packet.status_code == 200
    assert thread_packet.json()["thread_context_packet"]["evidence_pack"]["claims"]
    assert person_packet.status_code == 200
    assert person_packet.json()["person_context_packet"]["linked_entity_refs"]


def test_reasoning_service_persists_event_history_across_state_changes(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    runtime.memory.connection.execute(
        """
        INSERT INTO structured_memory_items (
            id, profile_slug, organization_id, workspace_slug, user_id, memory_kind, key, value, source,
            confidence, status, provenance_json, data_class, promotion_state, approved_count, rejected_count,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'fact', 'reply_style', 'formal', 'user', 0.8, 'active', '{}', 'operational', 'candidate', 1, 0, datetime('now'), datetime('now'))
        """,
        (str(uuid4()), runtime.active_profile.slug, context.organization_id, runtime.active_profile.slug, context.user_id),
    )
    runtime.memory.connection.commit()
    service = ReasoningService(runtime.platform, runtime.memory, runtime.active_profile, scheduler_service=runtime.scheduler_service)
    first_snapshot = service.world_state(
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    review_workflow = next(item for item in first_snapshot.workflows if item.workflow_type == "review_approval_queue")
    first_events = runtime.memory.list_workflow_events(review_workflow.id)
    assert len(first_events) == 1

    IntelligenceService(runtime.platform, runtime.memory, runtime.active_profile).record_training_example(
        actor_user_id=context.user_id,
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        source_type="draft",
        source_id="draft-history",
        input_text="input",
        output_text="output",
        status="candidate",
        approved_for_training=False,
    )
    service.world_state(
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    second_events = runtime.memory.list_workflow_events(review_workflow.id)
    second_domain_events = runtime.memory.list_workflow_domain_events(workflow_id=review_workflow.id)
    assert len(second_events) >= 2
    assert second_domain_events


def test_mutating_routes_append_domain_events_for_core_workflows(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    memory_id = str(uuid4())
    runtime.memory.connection.execute(
        """
        INSERT INTO structured_memory_items (
            id, profile_slug, organization_id, workspace_slug, user_id, memory_kind, key, value, source,
            confidence, status, provenance_json, data_class, promotion_state, approved_count, rejected_count,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'fact', 'reply_style', 'formal', 'user', 0.8, 'active', '{}', 'operational', 'candidate', 1, 0, datetime('now'), datetime('now'))
        """,
        (memory_id, runtime.active_profile.slug, context.organization_id, runtime.active_profile.slug, context.user_id),
    )
    runtime.memory.connection.commit()
    client = _client_for_runtime(runtime)
    client.cookies.set("kern_session", session_id)

    promotion_review = client.post(
        f"/intelligence/promotion-candidates/{memory_id}/review",
        json={"decision": "approved"},
    )
    assert promotion_review.status_code == 200

    legal_hold = client.post(
        "/compliance/legal-holds",
        json={"workspace_slug": runtime.active_profile.slug, "reason": "Preserve records"},
    )
    assert legal_hold.status_code == 200

    target = runtime.platform.create_user(
        email="subject@example.com",
        display_name="Subject",
        organization_id=context.organization_id,
        auth_source="oidc",
        status="active",
    )
    erasure_request = client.post(
        "/compliance/erasure-requests",
        json={"target_user_id": target.id, "workspace_slug": runtime.active_profile.slug, "reason": "GDPR request"},
    )
    assert erasure_request.status_code == 200
    erasure = erasure_request.json()["erasure_request"]
    erasure_execute = client.post(f"/compliance/erasure-requests/{erasure['id']}/execute")
    assert erasure_execute.status_code == 200

    review_events = runtime.memory.list_workflow_domain_events(
        workspace_slug=runtime.active_profile.slug,
        workflow_type="review_approval_queue",
        limit=50,
    )
    compliance_events = runtime.memory.list_workflow_domain_events(
        workspace_slug=runtime.active_profile.slug,
        workflow_type="compliance_export_erasure",
        limit=50,
    )
    assert any(event.event_type == "promotion_candidate_reviewed" for event in review_events)
    assert any(event.event_type == "legal_hold_created" for event in compliance_events)
    assert any(event.event_type == "erasure_requested" for event in compliance_events)
    assert any(event.event_type.startswith("erasure_") for event in compliance_events)


def test_preparation_feedback_records_event_on_actual_workflow(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    runtime.memory.save_email_draft(
        EmailDraft(to=["client@example.com"], subject="Precise follow-up", body="Prepared", status="draft"),
    )
    client = _client_for_runtime(runtime)
    client.cookies.set("kern_session", session_id)

    recommendation = ReasoningService(
        runtime.platform,
        runtime.memory,
        runtime.active_profile,
        scheduler_service=runtime.scheduler_service,
    ).recommend_for_transcript(
        "What should I follow up on next?",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    assert recommendation is not None

    feedback = client.post(
        "/intelligence/feedback",
        json={
            "signal_type": "packet_accepted",
            "source_type": "preparation",
            "source_id": recommendation.id,
        },
    )
    assert feedback.status_code == 200

    events = runtime.memory.list_workflow_domain_events(workflow_id=recommendation.workflow_id, limit=50)
    assert any(event.event_type == "packet_accepted" for event in events)


def test_preparation_draft_route_can_use_llm_rewrite_mode(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    session_id = _create_active_user(runtime)
    context = runtime.platform.build_auth_context(session_id)
    runtime.memory.save_email_draft(
        EmailDraft(to=["client@example.com"], subject="Rewrite follow-up", body="Prepared", status="draft"),
    )

    async def _render_packet_with_llm(packet):
        scaffold = packet.suggested_draft or ReasoningService(
            runtime.platform,
            runtime.memory,
            runtime.active_profile,
            scheduler_service=runtime.scheduler_service,
        ).build_draft_from_packet(packet)
        return scaffold.model_copy(update={"body": "LLM rewritten wording.", "mode": "llm_rewrite"})

    runtime.orchestrator = SimpleNamespace(_render_packet_with_llm=_render_packet_with_llm)
    client = _client_for_runtime(runtime)
    client.cookies.set("kern_session", session_id)
    recommendation = ReasoningService(
        runtime.platform,
        runtime.memory,
        runtime.active_profile,
        scheduler_service=runtime.scheduler_service,
    ).recommend_for_transcript(
        "What should I follow up on next?",
        organization_id=context.organization_id,
        workspace_slug=runtime.active_profile.slug,
        actor_user_id=context.user_id,
    )
    assert recommendation is not None

    draft = client.post(
        f"/intelligence/preparation/{recommendation.id}/draft",
        json={"mode": "llm_rewrite"},
    )
    assert draft.status_code == 200
    assert draft.json()["suggested_draft"]["mode"] == "llm_rewrite"
    assert draft.json()["render_mode"] == "llm_rewrite"


def test_scheduler_custom_prompt_requires_deterministic_reasoning() -> None:
    calls: list[dict[str, object]] = []

    async def _stub_process_transcript(prompt: str, trigger: str = "manual_ui", **kwargs):
        calls.append({"prompt": prompt, "trigger": trigger, **kwargs})
        return AssistantTurn(
            trigger="scheduler",
            transcript=prompt,
            intent_type="query",
            response_text="deterministic",
            spoken=False,
            reasoning_source="system_decision",
            recommendation_id="rec-1",
            workflow_type="review_approval_queue",
        )

    runtime = KernRuntime.__new__(KernRuntime)
    runtime.ensure_production_access = lambda blocked_scope=None: True
    runtime.orchestrator = SimpleNamespace(
        snapshot=SimpleNamespace(action_in_progress=False),
        process_transcript=_stub_process_transcript,
    )

    result = asyncio.run(
        KernRuntime._execute_scheduled_task(
            runtime,
            {"action_type": "custom_prompt", "action_payload": {"prompt": "What needs attention?"}, "title": "Reasoning prompt"},
        )
    )

    assert result["reasoning_source"] == "system_decision"
    assert calls
    assert calls[0]["allow_llm_fallback"] is False
