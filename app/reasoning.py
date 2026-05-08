from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from app.document_intelligence import DocumentIntelligenceService
from app.freeform_intelligence import FreeformIntelligenceService
from app.intelligence import IntelligenceService
from app.memory import MemoryRepository
from app.platform import PlatformStore
from app.retrieval import RetrievalService
from app.types import (
    ClaimEvidenceRef,
    ClaimRecord,
    DecisionRecord,
    DocumentAnswerPacket,
    EvidenceBundle,
    FreeformIntentRecord,
    FocusHint,
    GenerationContract,
    InteractionOutcomeRecord,
    MissingInputRecord,
    NegativeEvidenceRecord,
    ObligationRecord,
    PersonContextPacket,
    PreparationPacket,
    ProfileSummary,
    RankingExplanation,
    RankingFeatureVector,
    ReadinessEdge,
    ReadinessNode,
    RecommendationActionType,
    RecommendationRecord,
    ShadowRankingRecord,
    SuggestedDraftRecord,
    TaskIntentRecord,
    ThreadContextPacket,
    WorkflowDomainEvent,
    WorkflowEvent,
    WorkflowRecord,
    WorldStateSnapshot,
)


class ReasoningService:
    TOKEN_PATTERN = re.compile(r"[0-9A-Za-zÃ„Ã–ÃœÃ¤Ã¶Ã¼ÃŸ_-]+")
    _ADVISORY_MARKERS = (
        "what should",
        "what now",
        "what next",
        "next step",
        "next action",
        "what needs",
        "what is pending",
        "what matters",
        "needs attention",
        "attention",
        "follow up",
        "follow-up",
        "reply",
        "draft",
        "write",
        "send",
        "message",
        "message",
        "review",
        "export",
        "erasure",
        "finalize",
        "schedule",
        "compliance",
        "deadline",
        "overdue",
    )
    _ACTION_KEYWORDS: dict[str, tuple[str, ...]] = {
        "draft_reply": ("reply", "draft", "follow up", "follow-up", "message", "respond", "message"),
        "suggested_draft": ("reply", "draft", "follow up", "follow-up", "message", "respond", "message"),
        "follow_up_candidate": ("follow up", "follow-up", "reply", "customer", "client", "message"),
        "missing_context": ("missing", "need", "context", "attachment", "information", "input"),
        "blocked_item": ("blocked", "hold", "missing", "waiting", "approval"),
        "ready_to_finalize": ("finalize", "invoice", "offer", "document", "regulated", "gobd"),
        "evidence_pack": ("evidence", "proof", "context", "document", "history"),
        "review_candidate": ("review", "approve", "candidate", "pattern", "training", "memory"),
        "run_export": ("export", "evidence", "bundle", "download", "manifest"),
        "confirm_erasure": ("erasure", "delete", "privacy", "gdpr", "hold"),
        "finalize_document": ("finalize", "invoice", "offer", "document", "regulated", "gobd"),
        "schedule_task": ("schedule", "reminder", "task", "failure", "retry"),
        "escalate": ("blocked", "hold", "escalate", "risk", "compliance"),
    }
    _WORKFLOW_KEYWORDS: dict[str, tuple[str, ...]] = {
        "review_approval_queue": ("approval", "candidate", "review", "training", "memory"),
        "compliance_export_erasure": ("compliance", "export", "erasure", "hold", "privacy"),
        "regulated_document_lifecycle": ("invoice", "offer", "regulated", "document", "finalize"),
        "scheduling_follow_through": ("schedule", "reminder", "task", "retry", "failed"),
        "local_follow_up": ("reply", "message", "draft", "follow up", "client"),
    }

    def __init__(
        self,
        platform: PlatformStore,
        memory: MemoryRepository,
        profile: ProfileSummary,
        *,
        scheduler_service=None,
    ) -> None:
        self.platform = platform
        self.memory = memory
        self.profile = profile
        self.scheduler_service = scheduler_service
        self.retrieval = RetrievalService(memory)
        self.intelligence = IntelligenceService(platform, memory, profile)
        self.document_intelligence = DocumentIntelligenceService(
            memory,
            profile,
            retrieval=self.retrieval,
            intelligence=self.intelligence,
        )
        self.freeform_intelligence = FreeformIntelligenceService(
            memory,
            profile,
            retrieval=self.retrieval,
            intelligence=self.intelligence,
            document_intelligence=self.document_intelligence,
            preparation_packet_getter=self.get_preparation_packet_for_transcript,
            recommendation_lister=self.list_recommendations,
        )

    def world_state(
        self,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> WorldStateSnapshot:
        workspace = workspace_slug or self.profile.slug
        now = datetime.now(timezone.utc)
        memory_items = self.memory.list_structured_memory_items(
            organization_id=organization_id,
            workspace_slug=workspace,
            user_id=actor_user_id,
            limit=300,
        )
        feedback_signals = self.memory.list_feedback_signals(workspace_slug=workspace, user_id=None, limit=200)
        training_examples = self.memory.list_training_examples(workspace_slug=workspace, limit=200)
        documents = [
            item
            for item in self.memory.list_document_records(limit=200)
            if item.organization_id in {None, organization_id}
        ]
        regulated_documents = self.memory.list_regulated_documents(limit=200)
        regulated_candidates = self._regulated_document_candidates(documents, regulated_documents)
        erasure_requests = [
            item
            for item in self.platform.list_erasure_requests(organization_id or "")
            if item.workspace_slug in {None, workspace}
        ] if organization_id else []
        data_exports = [
            item
            for item in self.platform.list_data_exports(organization_id or "")
            if item.workspace_slug in {None, workspace}
        ] if organization_id else []
        active_holds = [
            item
            for item in self.platform.list_legal_holds(organization_id or "", active_only=True)
            if item.workspace_slug in {None, workspace}
        ] if organization_id else []
        scheduler_tasks = self.scheduler_service.list_tasks() if self.scheduler_service else []
        promotion_candidates = [
            item
            for item in memory_items
            if item.get("promotion_state") in {"candidate", "none"} and item.get("memory_kind") != "episodic_turn"
        ]
        contradiction_keys = self._contradiction_keys(memory_items)

        workflows: list[WorkflowRecord] = []
        workflow_events: list[WorkflowEvent] = []
        obligations: list[ObligationRecord] = []
        decisions = self._decision_records(
            organization_id=organization_id,
            workspace_slug=workspace,
            feedback_signals=feedback_signals,
            training_examples=training_examples,
        )

        review_payload = self._review_queue_payload(
            promotion_candidates=promotion_candidates,
            training_examples=training_examples,
            contradiction_keys=contradiction_keys,
        )
        if review_payload["candidate_count"] > 0:
            workflow, event, queue_obligations = self._review_workflow(
                organization_id=organization_id,
                workspace_slug=workspace,
                actor_user_id=actor_user_id,
                now=now,
                review_payload=review_payload,
            )
            workflows.append(workflow)
            workflow_events.append(event)
            obligations.extend(queue_obligations)

        if erasure_requests or data_exports or active_holds:
            workflow, event, compliance_obligations = self._compliance_workflow(
                organization_id=organization_id,
                workspace_slug=workspace,
                actor_user_id=actor_user_id,
                now=now,
                erasure_requests=erasure_requests,
                data_exports=data_exports,
                active_holds=active_holds,
            )
            workflows.append(workflow)
            workflow_events.append(event)
            obligations.extend(compliance_obligations)

        if regulated_candidates or regulated_documents:
            workflow, event, document_obligations = self._regulated_document_workflow(
                organization_id=organization_id,
                workspace_slug=workspace,
                actor_user_id=actor_user_id,
                now=now,
                regulated_candidates=regulated_candidates,
                regulated_documents=regulated_documents,
            )
            workflows.append(workflow)
            workflow_events.append(event)
            obligations.extend(document_obligations)

        if scheduler_tasks:
            workflow, event, schedule_obligations = self._scheduler_workflow(
                organization_id=organization_id,
                workspace_slug=workspace,
                actor_user_id=actor_user_id,
                now=now,
                scheduler_tasks=scheduler_tasks,
            )
            workflows.append(workflow)
            workflow_events.append(event)
            obligations.extend(schedule_obligations)

        self.memory.replace_reasoning_snapshot(
            workspace_slug=workspace,
            workflows=workflows,
            workflow_events=workflow_events,
            obligations=obligations,
            decisions=decisions,
        )
        hydrated_workflows: list[WorkflowRecord] = []
        for workflow in workflows:
            domain_events = self.memory.list_workflow_domain_events(workflow_id=workflow.id, limit=12)
            hydrated_workflows.append(
                workflow.model_copy(
                    update={
                        "metadata": {
                            **workflow.metadata,
                            "event_refs": [item.id for item in domain_events],
                            "domain_event_count": len(domain_events),
                        }
                    }
                )
            )
        ordered_obligations = sorted(
            obligations,
            key=lambda item: (-item.priority, self._normalize_datetime(item.due_at) or datetime.max.replace(tzinfo=timezone.utc)),
        )
        return WorldStateSnapshot(
            organization_id=organization_id,
            workspace_slug=workspace,
            actor_user_id=actor_user_id,
            generated_at=now,
            workflow_count=len(hydrated_workflows),
            obligation_count=len(obligations),
            risk_count=sum(1 for workflow in hydrated_workflows if workflow.status == "blocked"),
            approval_queue_count=review_payload["candidate_count"],
            workflows=hydrated_workflows,
            obligations=ordered_obligations,
            summary={
                "active_holds": len(active_holds),
                "regulated_candidates": len(regulated_candidates),
                "promotion_candidates": len(promotion_candidates),
                "training_candidates": len([item for item in training_examples if item.status == "candidate"]),
                "domain_events": sum(int(item.metadata.get("domain_event_count", 0) or 0) for item in hydrated_workflows),
            },
        )

    def list_workflows(self, *, organization_id: str | None, workspace_slug: str | None, actor_user_id: str | None) -> list[WorkflowRecord]:
        return self.world_state(
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        ).workflows

    def get_workflow(self, workflow_id: str, *, organization_id: str | None, workspace_slug: str | None, actor_user_id: str | None) -> WorkflowRecord | None:
        self.world_state(organization_id=organization_id, workspace_slug=workspace_slug, actor_user_id=actor_user_id)
        return self.memory.get_workflow_record(workflow_id)

    def list_obligations(self, *, organization_id: str | None, workspace_slug: str | None, actor_user_id: str | None) -> list[ObligationRecord]:
        self.world_state(organization_id=organization_id, workspace_slug=workspace_slug, actor_user_id=actor_user_id)
        return self.memory.list_obligation_records(workspace_slug=workspace_slug or self.profile.slug, actor_user_id=actor_user_id)

    def list_decisions(self, *, organization_id: str | None, workspace_slug: str | None, actor_user_id: str | None) -> list[DecisionRecord]:
        self.world_state(organization_id=organization_id, workspace_slug=workspace_slug, actor_user_id=actor_user_id)
        return self.memory.list_decision_records(workspace_slug=workspace_slug or self.profile.slug, actor_user_id=actor_user_id)

    def list_recommendations(
        self,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> list[RecommendationRecord]:
        snapshot = self.world_state(
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        return self._recommendations_for_snapshot(snapshot, actor_user_id=actor_user_id)

    def get_recommendation(
        self,
        recommendation_id: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> RecommendationRecord | None:
        for recommendation in self.list_recommendations(
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        ):
            if recommendation.id == recommendation_id:
                return recommendation
        return None

    def get_evidence_bundle(
        self,
        bundle_id: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> EvidenceBundle | None:
        for recommendation in self.list_recommendations(
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        ):
            if recommendation.evidence_bundle.id == bundle_id:
                return recommendation.evidence_bundle
        return None

    def classify_task_intent_for_transcript(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        selected_document_ids: list[str] | None = None,
        ) -> TaskIntentRecord:
        return self.document_intelligence.classify_task_intent(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            selected_document_ids=selected_document_ids,
        )

    def classify_freeform_intent_for_transcript(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        selected_document_ids: list[str] | None = None,
    ) -> FreeformIntentRecord:
        return self.freeform_intelligence.classify_intent(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            selected_document_ids=selected_document_ids,
        )

    def route_freeform_for_transcript(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        selected_document_ids: list[str] | None = None,
    ) -> dict[str, object]:
        return self.freeform_intelligence.route_freeform(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            selected_document_ids=selected_document_ids,
        )

    def get_document_answer_packet(
        self,
        packet_id: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> DocumentAnswerPacket | None:
        packet = self.document_intelligence.get_document_answer_packet(packet_id)
        if packet is None:
            return None
        if packet.organization_id not in {None, organization_id}:
            return None
        if packet.workspace_slug not in {None, workspace_slug or self.profile.slug}:
            return None
        if packet.actor_user_id not in {None, actor_user_id}:
            return None
        return packet

    def get_document_answer_packet_for_transcript(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        selected_document_ids: list[str] | None = None,
    ) -> DocumentAnswerPacket | None:
        return self.document_intelligence.build_document_answer_packet(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            selected_document_ids=selected_document_ids,
        )

    def get_thread_context_packet(
        self,
        packet_id: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> ThreadContextPacket | None:
        packet = self.freeform_intelligence.get_thread_context_packet(packet_id)
        if packet is None:
            return None
        if packet.organization_id not in {None, organization_id}:
            return None
        if packet.workspace_slug not in {None, workspace_slug or self.profile.slug}:
            return None
        if packet.actor_user_id not in {None, actor_user_id}:
            return None
        return packet

    def get_thread_context_packet_for_transcript(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> ThreadContextPacket:
        return self.freeform_intelligence.build_thread_context_packet(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )

    def get_person_context_packet(
        self,
        packet_id: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> PersonContextPacket | None:
        packet = self.freeform_intelligence.get_person_context_packet(packet_id)
        if packet is None:
            return None
        if packet.organization_id not in {None, organization_id}:
            return None
        if packet.workspace_slug not in {None, workspace_slug or self.profile.slug}:
            return None
        if packet.actor_user_id not in {None, actor_user_id}:
            return None
        return packet

    def get_person_context_packet_for_transcript(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> PersonContextPacket:
        return self.freeform_intelligence.build_person_context_packet(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )

    def record_interaction_outcome(
        self,
        *,
        packet_type: str,
        packet_id: str,
        outcome_type: str,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> InteractionOutcomeRecord:
        record = InteractionOutcomeRecord(
            id=self._stable_id("interaction-outcome", packet_type, packet_id, outcome_type, datetime.now(timezone.utc).isoformat()),
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug or self.profile.slug,
            actor_user_id=actor_user_id,
            packet_type=packet_type,  # type: ignore[arg-type]
            packet_id=packet_id,
            outcome_type=outcome_type,  # type: ignore[arg-type]
            metadata=metadata or {},
        )
        return self.memory.record_interaction_outcome(record)

    def list_focus_hints(
        self,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> list[FocusHint]:
        snapshot = self.world_state(
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        recommendations = self._recommendations_for_snapshot(snapshot, actor_user_id=actor_user_id)
        return [self._focus_hint_for_recommendation(item) for item in recommendations[:6]]

    def get_preparation_packet(
        self,
        recommendation_id: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> PreparationPacket | None:
        recommendation = self.get_recommendation(
            recommendation_id,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        if recommendation is None:
            return None
        return self._preparation_packet_for_recommendation(recommendation)

    def get_preparation_packet_for_transcript(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> PreparationPacket | None:
        recommendation = self.recommend_for_transcript(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        if recommendation is None:
            return None
        return self._preparation_packet_for_recommendation(recommendation)

    def recommend_for_transcript(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> RecommendationRecord | None:
        normalized = transcript.strip().lower()
        if not normalized:
            return None
        snapshot = self.world_state(
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        recommendations = [
            self._recommendation_for_workflow(workflow, snapshot=snapshot, actor_user_id=actor_user_id)
            for workflow in snapshot.workflows
        ]
        ranked_recommendations = [item for item in recommendations if item is not None]
        if not ranked_recommendations:
            return None

        advisory_request = self._is_advisory_request(normalized)
        tokens = self._tokenize(normalized)
        memory_hits = self.intelligence.retrieve_memory_context(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug or self.profile.slug,
            user_id=actor_user_id,
            limit=4,
        )
        document_hits = [
            hit
            for hit in self.retrieval.retrieve(transcript, scope="profile", limit=6)
            if hit.source_type in {"document", "archive"}
        ]

        best_score = 0.0
        best_recommendation: RecommendationRecord | None = None
        for recommendation in ranked_recommendations:
            score, reasons, matched_memory, matched_documents = self._score_transcript_match(
                recommendation,
                transcript_tokens=tokens,
                advisory_request=advisory_request,
                memory_hits=memory_hits,
                document_hits=document_hits,
            )
            threshold = 0.9 if advisory_request else 1.15
            if score < threshold:
                continue
            enriched = self._enrich_recommendation(
                recommendation,
                score=score,
                transcript=transcript,
                match_reasons=reasons,
                matched_memory=matched_memory,
                matched_documents=matched_documents,
            )
            if score > best_score:
                best_score = score
                best_recommendation = enriched
        return best_recommendation

    def _recommendations_for_snapshot(
        self,
        snapshot: WorldStateSnapshot,
        *,
        actor_user_id: str | None,
    ) -> list[RecommendationRecord]:
        recommendations = [
            self._recommendation_for_workflow(workflow, snapshot=snapshot, actor_user_id=actor_user_id)
            for workflow in snapshot.workflows
        ]
        filtered = [item for item in recommendations if item is not None]
        filtered.sort(key=lambda item: item.ranking_explanation.score, reverse=True)
        for item in filtered[:12]:
            self._record_shadow_ranking(item)
        return filtered

    def _is_advisory_request(self, normalized_transcript: str) -> bool:
        return any(marker in normalized_transcript for marker in self._ADVISORY_MARKERS)

    def _tokenize(self, text: str) -> list[str]:
        return [match.group(0).lower() for match in self.TOKEN_PATTERN.finditer(text)]

    def _score_transcript_match(
        self,
        recommendation: RecommendationRecord,
        *,
        transcript_tokens: list[str],
        advisory_request: bool,
        memory_hits: list[dict[str, object]],
        document_hits: list,
    ) -> tuple[float, list[str], list[dict[str, object]], list]:
        base_score = float(recommendation.ranking_explanation.score or 0.0)
        action_keywords = self._ACTION_KEYWORDS.get(recommendation.recommendation_type, ())
        workflow_keywords = self._WORKFLOW_KEYWORDS.get(str(recommendation.workflow_type or ""), ())
        haystack = " ".join(
            [
                recommendation.title.lower(),
                recommendation.reason.lower(),
                str(recommendation.recommendation_type).replace("_", " "),
                str(recommendation.workflow_type or "").replace("_", " "),
                " ".join(str(reason).lower() for reason in recommendation.ranking_explanation.reasons),
            ]
        )
        overlap = sum(1 for token in transcript_tokens if token and token in haystack)
        action_match = sum(1 for keyword in action_keywords if keyword in haystack and any(token in keyword for token in transcript_tokens))
        workflow_match = sum(1 for keyword in workflow_keywords if any(token in keyword for token in transcript_tokens))

        matched_memory = [
            item
            for item in memory_hits
            if self._recommendation_matches_evidence(recommendation, title=str(item.get("key") or item.get("title") or ""), body=str(item.get("value") or ""))
        ]
        matched_documents = [
            hit
            for hit in document_hits
            if self._recommendation_matches_evidence(
                recommendation,
                title=str(hit.metadata.get("title") or hit.source_id),
                body=str(hit.text or ""),
            )
        ]

        score = (
            base_score
            + min(0.9, overlap * 0.22)
            + min(0.45, action_match * 0.2)
            + min(0.35, workflow_match * 0.16)
            + min(0.45, len(matched_memory) * 0.12)
            + min(0.4, len(matched_documents) * 0.1)
            + (0.22 if advisory_request else 0.0)
        )

        reasons = []
        if advisory_request:
            reasons.append("The transcript asks for workflow guidance rather than free-form generation.")
        if overlap:
            reasons.append("Transcript terms matched the recommended workflow and action vocabulary.")
        if matched_memory:
            reasons.append("Scoped memory hits reinforced the recommendation with local facts and patterns.")
        if matched_documents:
            reasons.append("Document retrieval provided workspace evidence for the recommendation.")
        return round(score, 4), reasons, matched_memory[:3], matched_documents[:3]

    def _recommendation_matches_evidence(self, recommendation: RecommendationRecord, *, title: str, body: str) -> bool:
        normalized = f"{title} {body}".lower()
        for keyword in self._ACTION_KEYWORDS.get(recommendation.recommendation_type, ()):
            if keyword in normalized:
                return True
        for keyword in self._WORKFLOW_KEYWORDS.get(str(recommendation.workflow_type or ""), ()):
            if keyword in normalized:
                return True
        return False

    def _enrich_recommendation(
        self,
        recommendation: RecommendationRecord,
        *,
        score: float,
        transcript: str,
        match_reasons: list[str],
        matched_memory: list[dict[str, object]],
        matched_documents: list,
    ) -> RecommendationRecord:
        enriched_items = list(recommendation.evidence_bundle.items)
        enriched_refs = list(recommendation.evidence_bundle.source_refs)
        for item in matched_memory:
            source_id = str(item.get("id") or item.get("key") or "")
            enriched_refs.append(source_id)
            enriched_items.append(
                {
                    "title": str(item.get("key") or item.get("title") or "memory"),
                    "status": "memory",
                    "reason": str(item.get("value") or ""),
                    "priority": min(5, 2 + int(item.get("approved_count", 0) or 0)),
                    "metadata": {
                        "kind": "memory",
                        "scope": item.get("provenance", {}).get("scope"),
                        "policy_safe": item.get("provenance", {}).get("policy_safe", True),
                        "score": item.get("score"),
                    },
                }
            )
        for hit in matched_documents:
            chunk_ref = str(hit.metadata.get("chunk_id") or hit.source_id)
            enriched_refs.append(chunk_ref)
            enriched_items.append(
                {
                    "title": str(hit.metadata.get("title") or hit.source_id),
                    "status": "retrieved_evidence",
                    "reason": str(hit.text[:240]),
                    "priority": 3,
                    "metadata": {
                        "kind": "document",
                        "source_type": hit.source_type,
                        "score": hit.score,
                        "classification": hit.metadata.get("classification"),
                        "policy_safe": True,
                    },
                }
            )
        ranking_reasons = list(dict.fromkeys([*recommendation.ranking_explanation.reasons, *match_reasons]))
        enriched_bundle = recommendation.evidence_bundle.model_copy(deep=True)
        enriched_bundle.items = enriched_items[:12]
        enriched_bundle.source_refs = list(dict.fromkeys(ref for ref in enriched_refs if ref))
        enriched_bundle.why_selected = list(dict.fromkeys([*recommendation.evidence_bundle.why_selected, *match_reasons]))
        enriched_bundle.summary = f"{recommendation.title} matched '{transcript.strip()}' through local workflow reasoning."
        enriched_bundle.action_relevance = min(1.0, max(recommendation.evidence_bundle.action_relevance, score / 2.0))
        enriched_ranking = recommendation.ranking_explanation.model_copy(deep=True)
        enriched_ranking.score = score
        enriched_ranking.reasons = ranking_reasons
        enriched_bundle.ranking_explanation = enriched_ranking
        enriched = recommendation.model_copy(deep=True)
        enriched.evidence_bundle = enriched_bundle
        enriched.ranking_explanation = enriched_ranking
        return enriched

    def build_draft_from_packet(self, packet: PreparationPacket) -> SuggestedDraftRecord | None:
        if not packet.generation_contract.allow_draft:
            return None
        if packet.suggested_draft is not None:
            return packet.suggested_draft
        if packet.preparation_type not in {"suggested_draft", "follow_up_candidate"}:
            return None
        return SuggestedDraftRecord(
            id=self._stable_id("draft", packet.id),
            title=f"Draft from {packet.title}",
            subject=f"Follow-up: {packet.title}",
            body=self._build_draft_body(packet),
            tone="clear and helpful",
            based_on_refs=list(packet.evidence_pack.source_refs),
            provenance={"source": "reasoning_service", "packet_id": packet.id},
        )

    def _preparation_packet_for_recommendation(self, recommendation: RecommendationRecord) -> PreparationPacket:
        suggested_draft = self._suggested_draft_for_recommendation(recommendation)
        focus_hint = self._focus_hint_for_recommendation(recommendation)
        return PreparationPacket(
            id=self._stable_id("prep", recommendation.id),
            profile_slug=self.profile.slug,
            organization_id=recommendation.organization_id,
            workspace_slug=recommendation.workspace_slug,
            actor_user_id=recommendation.actor_user_id,
            recommendation_id=recommendation.id,
            workflow_id=recommendation.workflow_id,
            workflow_type=recommendation.workflow_type,
            preparation_type=recommendation.recommendation_type,
            title=recommendation.title,
            summary=recommendation.reason,
            readiness_status=recommendation.readiness_status,
            why_ready=list(recommendation.why_ready),
            why_blocked=list(recommendation.why_blocked),
            missing_inputs=list(recommendation.missing_inputs),
            readiness_nodes=list(recommendation.readiness_nodes),
            readiness_edges=list(recommendation.readiness_edges),
            evidence_pack=recommendation.evidence_bundle,
            preparation_scope=recommendation.preparation_scope,
            worker_review_required=recommendation.worker_review_required,
            generation_contract=recommendation.generation_contract,
            event_refs=list(recommendation.event_refs),
            suggested_draft=suggested_draft,
            focus_hint=focus_hint,
        )

    def _focus_hint_for_recommendation(self, recommendation: RecommendationRecord) -> FocusHint:
        why_now = list(dict.fromkeys([*recommendation.why_ready, *recommendation.ranking_explanation.reasons]))[:4]
        return FocusHint(
            id=self._stable_id("focus", recommendation.id),
            title=recommendation.title,
            summary=recommendation.reason,
            recommendation_id=recommendation.id,
            workflow_id=recommendation.workflow_id,
            score=recommendation.ranking_explanation.score,
            readiness_status=recommendation.readiness_status,
            why_now=why_now,
            risk_level=recommendation.risk_level,
        )

    def _suggested_draft_for_recommendation(self, recommendation: RecommendationRecord) -> SuggestedDraftRecord | None:
        if recommendation.recommendation_type not in {"suggested_draft", "follow_up_candidate"}:
            return None
        if not recommendation.generation_contract.allow_draft:
            return None
        body = self._build_draft_body(self._preparation_packet_for_stub(recommendation))
        subject = "Prepared follow-up"
        for item in recommendation.evidence_bundle.items:
            metadata = item.get("metadata") or {}
            subject = str(metadata.get("subject") or item.get("title") or subject)
            if subject:
                break
        return SuggestedDraftRecord(
            id=self._stable_id("draft", recommendation.id),
            title=f"Prepared draft for {recommendation.title}",
            subject=subject,
            body=body,
            tone="clear and grounded",
            based_on_refs=list(recommendation.evidence_bundle.source_refs),
            provenance={"source": "reasoning_service", "recommendation_id": recommendation.id},
        )

    def _preparation_packet_for_stub(self, recommendation: RecommendationRecord) -> PreparationPacket:
        return PreparationPacket(
            id=self._stable_id("prep", recommendation.id),
            profile_slug=self.profile.slug,
            organization_id=recommendation.organization_id,
            workspace_slug=recommendation.workspace_slug,
            actor_user_id=recommendation.actor_user_id,
            recommendation_id=recommendation.id,
            workflow_id=recommendation.workflow_id,
            workflow_type=recommendation.workflow_type,
            preparation_type=recommendation.recommendation_type,
            title=recommendation.title,
            summary=recommendation.reason,
            readiness_status=recommendation.readiness_status,
            why_ready=list(recommendation.why_ready),
            why_blocked=list(recommendation.why_blocked),
            missing_inputs=list(recommendation.missing_inputs),
            readiness_nodes=list(recommendation.readiness_nodes),
            readiness_edges=list(recommendation.readiness_edges),
            evidence_pack=recommendation.evidence_bundle,
            preparation_scope=recommendation.preparation_scope,
            worker_review_required=recommendation.worker_review_required,
            generation_contract=recommendation.generation_contract,
            event_refs=list(recommendation.event_refs),
        )

    def _build_draft_body(self, packet: PreparationPacket) -> str:
        lines = ["Hello,", ""]
        if packet.summary:
            lines.append(packet.summary)
            lines.append("")
        if packet.why_ready:
            lines.append("Context prepared:")
            lines.extend(f"- {reason}" for reason in packet.why_ready[:3])
            lines.append("")
        if packet.missing_inputs:
            lines.append("Still needed before sending:")
            lines.extend(f"- {item.label}: {item.reason}" for item in packet.missing_inputs[:3])
            lines.append("")
        evidence_titles = [str(item.get("title") or "").strip() for item in packet.evidence_pack.items[:3] if item.get("title")]
        if evidence_titles:
            lines.append("Evidence used:")
            lines.extend(f"- {title}" for title in evidence_titles)
            lines.append("")
        lines.append("Best regards,")
        return "\n".join(lines).strip()

    def _regulated_document_candidates(self, documents, regulated_documents) -> list[dict[str, Any]]:
        regulated_document_ids = {item.document_id for item in regulated_documents if item.document_id}
        regulated_business_ids = {item.business_document_id for item in regulated_documents if item.business_document_id}
        candidates: list[dict[str, Any]] = []
        for document in documents:
            if document.data_class != "regulated_business":
                continue
            if document.id in regulated_document_ids or document.id in regulated_business_ids:
                continue
            candidates.append(
                {
                    "id": document.id,
                    "title": document.title,
                    "category": document.category,
                    "classification": document.classification,
                    "retention_state": document.retention_state or "standard",
                    "imported_at": document.imported_at.isoformat(),
                    "file_path": document.file_path,
                }
            )
        return candidates

    def _contradiction_keys(self, memory_items: list[dict[str, Any]]) -> set[str]:
        values_by_key: dict[str, set[str]] = {}
        for item in memory_items:
            if str(item.get("memory_kind")) != "fact":
                continue
            key = str(item.get("key") or "").strip().lower()
            value = str(item.get("value") or "").strip().lower()
            if not key or not value:
                continue
            values_by_key.setdefault(key, set()).add(value)
        return {key for key, values in values_by_key.items() if len(values) > 1}

    def _review_queue_payload(
        self,
        *,
        promotion_candidates: list[dict[str, Any]],
        training_examples,
        contradiction_keys: set[str],
    ) -> dict[str, Any]:
        candidate_items: list[dict[str, Any]] = []
        conflict_count = 0
        for item in promotion_candidates:
            key = str(item.get("key") or "").strip().lower()
            has_conflict = key in contradiction_keys
            if has_conflict:
                conflict_count += 1
            candidate_items.append(
                {
                    "id": item.get("id"),
                    "title": item.get("key") or item.get("value") or item.get("id"),
                    "scope": "user_private" if item.get("user_id") else "workspace" if item.get("workspace_slug") else "organization_shared",
                    "policy_safe": item.get("data_class") != "personal",
                    "kind": "memory",
                    "approved_count": item.get("approved_count", 0),
                    "has_conflict": has_conflict,
                }
            )
        for example in training_examples:
            if example.status != "candidate":
                continue
            candidate_items.append(
                {
                    "id": example.id,
                    "title": f"{example.source_type}:{example.source_id}",
                    "scope": "workspace",
                    "policy_safe": str(example.metadata.get("data_class") or "operational") != "personal",
                    "kind": "training_example",
                    "approved_count": 0,
                    "has_conflict": False,
                }
            )
        return {
            "candidate_count": len(candidate_items),
            "conflict_count": conflict_count,
            "items": candidate_items[:12],
        }

    def _review_workflow(self, *, organization_id, workspace_slug, actor_user_id, now, review_payload):
        workflow_id = self._stable_id("workflow", workspace_slug, "review")
        blocking_reasons = ["Conflicting facts require confirmation before promotion."] if review_payload["conflict_count"] else []
        workflow = WorkflowRecord(
            id=workflow_id,
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            workflow_type="review_approval_queue",
            subject_refs={"candidate_count": review_payload["candidate_count"]},
            status="blocked" if blocking_reasons else "awaiting_review",
            last_event="promotion_candidates_pending",
            next_expected_step="prepare_review_packet",
            blocking_reasons=blocking_reasons,
            evidence_refs=[str(item["id"]) for item in review_payload["items"]],
            confidence=0.94,
            metadata=review_payload,
            created_at=now,
            updated_at=now,
        )
        event = WorkflowEvent(
            id=self._stable_id("event", workflow_id, "pending", review_payload["candidate_count"], review_payload["conflict_count"]),
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            workflow_id=workflow_id,
            event_type="queue_detected",
            detail=f"{review_payload['candidate_count']} candidate items require review.",
            metadata={"candidate_count": review_payload["candidate_count"]},
            created_at=now,
        )
        obligations = [
            ObligationRecord(
                id=self._stable_id("obligation", workflow_id, str(item["id"])),
                profile_slug=self.profile.slug,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                workflow_id=workflow_id,
                title=f"Review candidate: {item['title']}",
                status="blocked" if item.get("has_conflict") else "open",
                reason="Manual approval remains the default for promotions and training examples.",
                priority=5 if item.get("has_conflict") else 4,
                blocking_reasons=["Conflicting fact values need confirmation."] if item.get("has_conflict") else [],
                evidence_refs=[str(item["id"])],
                metadata=item,
                created_at=now,
                updated_at=now,
            )
            for item in review_payload["items"][:8]
        ]
        return workflow, event, obligations

    def _compliance_workflow(self, *, organization_id, workspace_slug, actor_user_id, now, erasure_requests, data_exports, active_holds):
        workflow_id = self._stable_id("workflow", workspace_slug, "compliance")
        blocked = any(item.status == "blocked" for item in erasure_requests)
        pending_erasures = [item for item in erasure_requests if item.status in {"requested", "approved", "blocked"}]
        pending_exports = [item for item in data_exports if item.status in {"requested", "approved"}]
        blocking_reasons = ["A legal hold is blocking at least one erasure path."] if blocked or active_holds else []
        workflow = WorkflowRecord(
            id=workflow_id,
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            workflow_type="compliance_export_erasure",
            subject_refs={
                "erasure_count": len(pending_erasures),
                "export_count": len(pending_exports),
                "legal_hold_count": len(active_holds),
            },
            status="blocked" if blocking_reasons else "open",
            last_event="compliance_queue_updated",
            next_expected_step="prepare_blocker_brief" if blocking_reasons else "prepare_evidence_pack",
            blocking_reasons=blocking_reasons,
            due_at=min((item.updated_at for item in pending_erasures + pending_exports), default=None),
            evidence_refs=[item.id for item in pending_erasures[:4]] + [item.id for item in pending_exports[:4]],
            confidence=0.91,
            metadata={
                "erasure_requests": [item.model_dump(mode="json") for item in pending_erasures[:8]],
                "data_exports": [item.model_dump(mode="json") for item in pending_exports[:8]],
                "legal_holds": [item.model_dump(mode="json") for item in active_holds[:8]],
            },
            created_at=now,
            updated_at=now,
        )
        event = WorkflowEvent(
            id=self._stable_id("event", workflow_id, "queue", len(pending_erasures), len(pending_exports), len(active_holds), blocked),
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            workflow_id=workflow_id,
            event_type="compliance_queue_detected",
            detail=f"{len(pending_erasures)} erasure requests and {len(pending_exports)} export jobs require review.",
            metadata={"blocked": blocked},
            created_at=now,
        )
        obligations: list[ObligationRecord] = []
        for request in pending_erasures[:6]:
            obligations.append(
                ObligationRecord(
                    id=self._stable_id("obligation", workflow_id, request.id),
                    profile_slug=self.profile.slug,
                    organization_id=organization_id,
                    workspace_slug=workspace_slug,
                    actor_user_id=actor_user_id,
                    workflow_id=workflow_id,
                    title=f"Compliance review: erasure for {request.target_user_id}",
                    status="blocked" if request.status == "blocked" else "open",
                    reason=request.reason or "Pending erasure workflow needs review.",
                    priority=5,
                    due_at=request.updated_at,
                    blocking_reasons=blocking_reasons if request.status == "blocked" else [],
                    evidence_refs=[request.id],
                    metadata=request.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
            )
        for export in pending_exports[:4]:
            obligations.append(
                ObligationRecord(
                    id=self._stable_id("obligation", workflow_id, export.id),
                    profile_slug=self.profile.slug,
                    organization_id=organization_id,
                    workspace_slug=workspace_slug,
                    actor_user_id=actor_user_id,
                    workflow_id=workflow_id,
                    title=f"Evidence export: {export.workspace_slug or export.target_user_id or export.id}",
                    reason="Compliance export artifact should be generated or inspected.",
                    priority=4,
                    due_at=export.updated_at,
                    evidence_refs=[export.id],
                    metadata=export.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
            )
        return workflow, event, obligations

    def _regulated_document_workflow(self, *, organization_id, workspace_slug, actor_user_id, now, regulated_candidates, regulated_documents):
        workflow_id = self._stable_id("workflow", workspace_slug, "regulated")
        workflow = WorkflowRecord(
            id=workflow_id,
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            workflow_type="regulated_document_lifecycle",
            subject_refs={
                "candidate_count": len(regulated_candidates),
                "finalized_count": len([item for item in regulated_documents if item.immutability_state == "finalized"]),
            },
            status="awaiting_review" if regulated_candidates else "monitoring",
            last_event="regulated_document_state_checked",
            next_expected_step="prepare_finalization_packet" if regulated_candidates else "inspect_version_lineage",
            evidence_refs=[item["id"] for item in regulated_candidates[:6]],
            confidence=0.88,
            metadata={
                "candidates": regulated_candidates[:12],
                "finalized": [item.model_dump(mode="json") for item in regulated_documents[:8]],
            },
            created_at=now,
            updated_at=now,
        )
        event = WorkflowEvent(
            id=self._stable_id(
                "event",
                workflow_id,
                "documents",
                len(regulated_candidates),
                len([item for item in regulated_documents if item.immutability_state == "finalized"]),
            ),
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            workflow_id=workflow_id,
            event_type="regulated_documents_scanned",
            detail=f"{len(regulated_candidates)} regulated-business documents still need finalization review.",
            metadata={"candidate_count": len(regulated_candidates)},
            created_at=now,
        )
        obligations = [
            ObligationRecord(
                id=self._stable_id("obligation", workflow_id, item["id"]),
                profile_slug=self.profile.slug,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                workflow_id=workflow_id,
                title=f"Finalize regulated document: {item['title']}",
                reason="Regulated-business documents should move into immutable versioned state after review.",
                priority=4,
                due_at=datetime.fromisoformat(item["imported_at"]),
                evidence_refs=[item["id"]],
                metadata=item,
                created_at=now,
                updated_at=now,
            )
            for item in regulated_candidates[:6]
        ]
        return workflow, event, obligations

    def _scheduler_workflow(self, *, organization_id, workspace_slug, actor_user_id, now, scheduler_tasks):
        workflow_id = self._stable_id("workflow", workspace_slug, "scheduler")
        attention_tasks = [item for item in scheduler_tasks if str(item.get("run_status") or "idle") in {"failed", "running"} or item.get("failure_count", 0)]
        workflow = WorkflowRecord(
            id=workflow_id,
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            workflow_type="scheduling_follow_through",
            subject_refs={"task_count": len(scheduler_tasks), "attention_count": len(attention_tasks)},
            status="open" if attention_tasks else "monitoring",
            last_event="scheduler_state_checked",
            next_expected_step="prepare_follow_up_packet" if attention_tasks else "monitor_schedule",
            evidence_refs=[str(item.get("id")) for item in scheduler_tasks[:8]],
            confidence=0.76,
            metadata={"tasks": scheduler_tasks[:12]},
            created_at=now,
            updated_at=now,
        )
        event = WorkflowEvent(
            id=self._stable_id(
                "event",
                workflow_id,
                "scheduler",
                len(scheduler_tasks),
                len(attention_tasks),
                "|".join(str(item.get("id")) for item in attention_tasks[:4]),
            ),
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            workflow_id=workflow_id,
            event_type="scheduler_tasks_checked",
            detail=f"{len(scheduler_tasks)} scheduled tasks scanned for due or failed runs.",
            metadata={"attention_count": len(attention_tasks)},
            created_at=now,
        )
        obligations = [
            ObligationRecord(
                id=self._stable_id("obligation", workflow_id, str(item.get("id"))),
                profile_slug=self.profile.slug,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                workflow_id=workflow_id,
                title=f"Check scheduled task: {item.get('title') or item.get('id')}",
                status="open",
                reason=f"Task state is {item.get('run_status') or 'idle'} with {item.get('failure_count', 0)} failures recorded.",
                priority=3 if item.get("failure_count", 0) else 2,
                due_at=datetime.fromisoformat(item["next_run_at"]) if item.get("next_run_at") else None,
                evidence_refs=[str(item.get("id"))],
                metadata=item,
                created_at=now,
                updated_at=now,
            )
            for item in (attention_tasks[:6] if attention_tasks else scheduler_tasks[:4])
        ]
        return workflow, event, obligations

    def _decision_records(self, *, organization_id, workspace_slug, feedback_signals, training_examples) -> list[DecisionRecord]:
        decisions: list[DecisionRecord] = []
        for signal in feedback_signals[:40]:
            decisions.append(
                DecisionRecord(
                    id=self._stable_id("decision", signal.id),
                    profile_slug=self.profile.slug,
                    organization_id=organization_id,
                    workspace_slug=workspace_slug,
                    actor_user_id=signal.user_id,
                    decision_kind="feedback_signal",
                    decision_value=signal.signal_type,
                    source_type=signal.source_type,
                    source_id=signal.source_id,
                    reasoning_source="system_decision",
                    rationale=f"User signaled {signal.signal_type.replace('_', ' ')} on {signal.source_type}.",
                    metadata=signal.metadata,
                    created_at=signal.created_at,
                )
            )
        for example in training_examples[:40]:
            if example.status == "candidate":
                continue
            decisions.append(
                DecisionRecord(
                    id=self._stable_id("decision", example.id, example.status),
                    profile_slug=self.profile.slug,
                    organization_id=organization_id,
                    workspace_slug=workspace_slug,
                    actor_user_id=example.user_id,
                    decision_kind="training_example_review",
                    decision_value=example.status,
                    source_type=example.source_type,
                    source_id=example.source_id,
                    reasoning_source="system_decision",
                    rationale=f"Training example moved to {example.status}.",
                    metadata=example.metadata,
                    created_at=example.updated_at,
                )
            )
        decisions.sort(key=lambda item: item.created_at, reverse=True)
        return decisions[:80]

    def _recommendation_for_workflow(
        self,
        workflow: WorkflowRecord,
        *,
        snapshot: WorldStateSnapshot,
        actor_user_id: str | None,
    ) -> RecommendationRecord | None:
        action_type: RecommendationActionType
        title: str
        reason: str
        required_inputs: list[str] = []
        risk_level = "low"
        if workflow.workflow_type == "review_approval_queue":
            action_type = "review_candidate"
            title = "Prepared review packet for memory and training candidates"
            reason = f"{workflow.subject_refs.get('candidate_count', 0)} candidate items are ready for worker review before they shape shared behavior."
            risk_level = "medium" if workflow.blocking_reasons else "low"
        elif workflow.workflow_type == "compliance_export_erasure":
            if workflow.blocking_reasons:
                action_type = "blocked_item"
                title = "Prepared blocker brief for compliance work"
                reason = "A legal hold or retention rule is blocking at least one requested compliance step."
                risk_level = "high"
            elif int(workflow.subject_refs.get("erasure_count", 0) or 0) > 0:
                action_type = "missing_context"
                title = "Prepared compliance packet with missing legal checks"
                reason = "Erasure requests are queued, but worker review is still required before anything irreversible happens."
                risk_level = "high"
            else:
                action_type = "evidence_pack"
                title = "Prepared evidence pack for pending exports"
                reason = "Compliance exports are ready for generation or inspection with local evidence attached."
                risk_level = "medium"
        elif workflow.workflow_type == "regulated_document_lifecycle":
            action_type = "ready_to_finalize"
            title = "Prepared finalization packet for regulated documents"
            reason = f"{workflow.subject_refs.get('candidate_count', 0)} regulated-business documents are ready for worker finalization review."
            required_inputs = ["document approval"]
            risk_level = "medium"
        elif workflow.workflow_type == "scheduling_follow_through":
            action_type = "follow_up_candidate"
            title = "Prepared follow-up packet for scheduled work"
            reason = "The scheduler has due or previously failed work that should be checked with local context before more automation runs."
            risk_level = "medium"
        elif workflow.workflow_type == "local_follow_up":
            action_type = "suggested_draft"
            title = "Prepared draft from existing workspace context"
            reason = f"{workflow.subject_refs.get('draft_count', 0)} workspace drafts already contain enough context for a worker-ready follow-up."
        else:
            return None

        retrieval_context = self._retrieve_workflow_context(
            workflow,
            snapshot=snapshot,
            actor_user_id=actor_user_id,
        )
        evidence_bundle = self._evidence_bundle_for_workflow(
            workflow,
            action_type=action_type,
            snapshot=snapshot,
            actor_user_id=actor_user_id,
            retrieval_context=retrieval_context,
        )
        recommendation_id = self._stable_id("recommendation", workflow.id, action_type)
        evidence_bundle.recommendation_id = recommendation_id
        readiness_nodes, readiness_edges, missing_inputs = self._build_readiness_graph(
            workflow=workflow,
            action_type=action_type,
            evidence_bundle=evidence_bundle,
            required_inputs=required_inputs,
        )
        readiness_status = self._readiness_status_from_graph(
            readiness_nodes=readiness_nodes,
            required_inputs=required_inputs,
        )
        why_blocked = self._why_blocked_from_graph(
            workflow=workflow,
            readiness_nodes=readiness_nodes,
            missing_inputs=missing_inputs,
        )
        why_ready = self._why_ready_for_recommendation(
            workflow=workflow,
            action_type=action_type,
            evidence_bundle=evidence_bundle,
            readiness_status=readiness_status,
        )
        generation_contract = self._generation_contract_for_recommendation(
            workflow=workflow,
            action_type=action_type,
            readiness_status=readiness_status,
        )
        return RecommendationRecord(
            id=recommendation_id,
            profile_slug=self.profile.slug,
            organization_id=workflow.organization_id,
            workspace_slug=workflow.workspace_slug,
            actor_user_id=workflow.actor_user_id,
            workflow_id=workflow.id,
            workflow_type=workflow.workflow_type,
            recommendation_type=action_type,
            title=title,
            reason=reason,
            required_inputs=required_inputs,
            blocking_conditions=workflow.blocking_reasons,
            evidence_bundle=evidence_bundle,
            risk_level=risk_level,
            reversible=action_type not in {"ready_to_finalize"},
            recommended_now=readiness_status == "ready_now",
            reasoning_source="system_decision",
            ranking_explanation=evidence_bundle.ranking_explanation,
            readiness_status=readiness_status,
            why_ready=why_ready,
            why_blocked=why_blocked,
            missing_inputs=missing_inputs,
            readiness_nodes=readiness_nodes,
            readiness_edges=readiness_edges,
            preparation_scope=evidence_bundle.scope,
            worker_review_required=True,
            generation_contract=generation_contract,
            event_refs=list(evidence_bundle.event_refs),
            created_at=datetime.now(timezone.utc),
        )

    def _readiness_status_for_recommendation(
        self,
        *,
        workflow: WorkflowRecord,
        action_type: RecommendationActionType,
        required_inputs: list[str],
    ) -> str:
        if workflow.blocking_reasons:
            return "blocked"
        if required_inputs:
            return "needs_review"
        if action_type in {"missing_context", "blocked_item"}:
            return "waiting_on_input"
        return "ready_now"

    def _why_ready_for_recommendation(
        self,
        *,
        workflow: WorkflowRecord,
        action_type: RecommendationActionType,
        evidence_bundle: EvidenceBundle,
        readiness_status: str,
    ) -> list[str]:
        reasons = list(evidence_bundle.why_selected[:3])
        if workflow.workflow_type == "local_follow_up":
            reasons.insert(0, "An existing draft and prior workspace context are already available.")
        elif workflow.workflow_type == "regulated_document_lifecycle":
            reasons.insert(0, "The document has enough retained metadata to prepare a finalization review.")
        elif workflow.workflow_type == "review_approval_queue":
            reasons.insert(0, "Candidate memory and training items were gathered into one review packet.")
        elif workflow.workflow_type == "compliance_export_erasure":
            reasons.insert(0, "Compliance records and holds were assembled into one evidence-backed packet.")
        elif workflow.workflow_type == "scheduling_follow_through":
            reasons.insert(0, "Due and failed scheduled work was gathered into a follow-up packet.")
        if readiness_status == "needs_review":
            reasons.append("Worker review remains required before any sensitive state change.")
        return list(dict.fromkeys(reason for reason in reasons if reason))[:4]

    def _missing_inputs_for_recommendation(
        self,
        *,
        workflow: WorkflowRecord,
        action_type: RecommendationActionType,
        required_inputs: list[str],
    ) -> list[MissingInputRecord]:
        items: list[MissingInputRecord] = []
        for index, reason in enumerate(workflow.blocking_reasons, start=1):
            items.append(
                MissingInputRecord(
                    id=self._stable_id("missing", workflow.id, action_type, "block", index),
                    label="Resolve blocker",
                    reason=reason,
                    required_for=action_type,
                    severity="blocking",
                )
            )
        for index, requirement in enumerate(required_inputs, start=1):
            items.append(
                MissingInputRecord(
                    id=self._stable_id("missing", workflow.id, action_type, "input", index),
                    label=requirement.replace("_", " ").capitalize(),
                    reason=f"{requirement.replace('_', ' ')} is still required before worker review can finish.",
                    required_for=action_type,
                    severity="warning",
                )
            )
        if workflow.workflow_type == "local_follow_up" and int(workflow.subject_refs.get("draft_count", 0) or 0) <= 0:
            items.append(
                MissingInputRecord(
                    id=self._stable_id("missing", workflow.id, action_type, "draft"),
                    label="Draft context",
                    reason="No existing draft or follow-up packet was found for this thread.",
                    required_for=action_type,
                    severity="warning",
                )
            )
        if workflow.workflow_type == "compliance_export_erasure" and int(workflow.subject_refs.get("erasure_count", 0) or 0) > 0:
            items.append(
                MissingInputRecord(
                    id=self._stable_id("missing", workflow.id, action_type, "legal-check"),
                    label="Legal review",
                    reason="A worker still needs to confirm the erasure path against active retention and hold state.",
                    required_for=action_type,
                    severity="warning",
                )
            )
        if workflow.workflow_type == "scheduling_follow_through" and int(workflow.subject_refs.get("attention_count", 0) or 0) > 0:
            items.append(
                MissingInputRecord(
                    id=self._stable_id("missing", workflow.id, action_type, "owner-check"),
                    label="Owner confirmation",
                    reason="A worker should confirm whether the scheduled failure needs follow-up or can be retried safely.",
                    required_for=action_type,
                    severity="info",
                )
            )
        return items[:5]

    def _evidence_bundle_for_workflow(
        self,
        workflow: WorkflowRecord,
        *,
        action_type: RecommendationActionType,
        snapshot: WorldStateSnapshot,
        actor_user_id: str | None,
        retrieval_context: dict[str, Any] | None = None,
    ) -> EvidenceBundle:
        retrieval_context = retrieval_context or self._retrieve_workflow_context(
            workflow,
            snapshot=snapshot,
            actor_user_id=actor_user_id,
        )
        related_obligations = list(retrieval_context.get("obligations", []))[:6]
        policy_safe = all(not str(item.metadata.get("data_class") or "") == "personal" for item in related_obligations)
        scope = "user_private" if any(item.actor_user_id and item.actor_user_id == actor_user_id for item in related_obligations) else "workspace"
        claims = self._claim_records_for_workflow(
            workflow,
            action_type=action_type,
            retrieval_context=retrieval_context,
        )
        feature_vector = RankingFeatureVector(
            workflow_match=1.0,
            urgency_score=self._urgency_score(workflow.due_at, related_obligations),
            recency_score=0.8 if workflow.updated_at else 0.5,
            approval_score=min(1.0, float(workflow.metadata.get("candidate_count", 0) or 0) / 4.0) if workflow.workflow_type == "review_approval_queue" else min(1.0, float(len(retrieval_context.get("decisions", []))) / 4.0),
            entity_match_score=0.85 if retrieval_context.get("same_contact_history") else 0.45 if workflow.subject_refs else 0.2,
            document_class_score=0.9 if workflow.workflow_type == "regulated_document_lifecycle" else 0.65 if retrieval_context.get("document_hits") else 0.4,
            actor_match_score=1.0 if workflow.actor_user_id and workflow.actor_user_id == actor_user_id else 0.5,
            policy_safe=policy_safe,
        )
        score = round(
            (feature_vector.workflow_match * 0.24)
            + (feature_vector.urgency_score * 0.24)
            + (feature_vector.recency_score * 0.08)
            + (feature_vector.approval_score * 0.12)
            + (feature_vector.entity_match_score * 0.1)
            + (feature_vector.document_class_score * 0.08)
            + (feature_vector.actor_match_score * 0.08)
            + (0.06 if feature_vector.policy_safe else 0.0),
            4,
        )
        reasons = [
            f"Workflow type {workflow.workflow_type.replace('_', ' ')} is active in the selected workspace.",
            "Due or blocked obligations were ranked ahead of passive memory.",
        ]
        if workflow.blocking_reasons:
            reasons.append("Blocking reasons are exposed directly so the recommendation can be trusted without LLM inference.")
        if workflow.workflow_type == "review_approval_queue":
            reasons.append("Approved and rejected user behavior is being reused as a deterministic ranking signal.")
        if retrieval_context.get("same_contact_history"):
            reasons.append("Same-contact history was retrieved before broader archive evidence.")
        if retrieval_context.get("negative_evidence"):
            reasons.append("Expected evidence gaps were recorded explicitly instead of being guessed around.")
        ranking = RankingExplanation(score=score, features=feature_vector, reasons=reasons)
        items = list(retrieval_context.get("ordered_items", []))[:14]
        source_refs = list(
            dict.fromkeys([*workflow.evidence_refs, *[str(item.get("ref_id") or "") for item in items if item.get("ref_id")]])
        )
        required_claims = [item for item in claims if item.required]
        grounded_claims = [item for item in required_claims if item.status == "supported"]
        return EvidenceBundle(
            id=self._stable_id("evidence", workflow.id, action_type),
            workflow_id=workflow.id,
            summary=workflow.last_event or workflow.next_expected_step,
            why_selected=reasons,
            scope=scope,
            confidence=workflow.confidence,
            policy_safe=policy_safe,
            source_refs=[ref for ref in source_refs if ref],
            action_relevance=min(1.0, score),
            items=items,
            claims=claims,
            negative_evidence=list(retrieval_context.get("negative_evidence", [])),
            coverage_score=round(len(grounded_claims) / len(required_claims), 4) if required_claims else 1.0,
            freshness=self._freshness_seconds(
                workflow.updated_at,
                retrieval_context.get("domain_events", []),
                related_obligations,
            ),
            event_refs=[event.id for event in retrieval_context.get("domain_events", [])],
            ranking_explanation=ranking,
        )

    def _retrieve_workflow_context(
        self,
        workflow: WorkflowRecord,
        *,
        snapshot: WorldStateSnapshot,
        actor_user_id: str | None,
    ) -> dict[str, Any]:
        workspace_slug = workflow.workspace_slug or self.profile.slug
        query = self._workflow_query(workflow)
        domain_events = self.memory.list_workflow_domain_events(workflow_id=workflow.id, limit=12)
        obligations = [item for item in snapshot.obligations if item.workflow_id == workflow.id]
        decisions = [
            item
            for item in self.memory.list_decision_records(workspace_slug=workspace_slug, actor_user_id=actor_user_id, limit=80)
            if item.source_id in workflow.evidence_refs or item.source_type in {"draft", "memory", "training_example"}
        ][:8]
        memory_hits = self.intelligence.retrieve_memory_context(
            query,
            organization_id=workflow.organization_id,
            workspace_slug=workspace_slug,
            user_id=actor_user_id,
            limit=10,
        )
        user_private_memory = [item for item in memory_hits if item.get("provenance", {}).get("scope") == "user_private"][:4]
        workspace_memory = [item for item in memory_hits if item.get("provenance", {}).get("scope") != "user_private"][:4]
        retrieval_hits = self.retrieval.retrieve(query, scope="profile", limit=8)
        document_hits = [item for item in retrieval_hits if item.source_type == "document"][:4]
        archive_hits = [item for item in retrieval_hits if item.source_type == "archive"][:3]
        correspondence_items = list((workflow.metadata or {}).get("drafts", []))[:6]
        same_contact_history = bool(correspondence_items or archive_hits)
        negative_evidence: list[NegativeEvidenceRecord] = []
        if workflow.workflow_type == "local_follow_up" and not correspondence_items:
            negative_evidence.append(
                NegativeEvidenceRecord(
                    id=self._stable_id("neg", workflow.id, "draft-history"),
                    expected_signal="existing draft or thread history",
                    searched_sources=["workflow_projection", "recent_drafts", "archive"],
                    detail="The system looked for an existing follow-up draft or thread history and did not find one.",
                )
            )
        if workflow.workflow_type == "regulated_document_lifecycle" and not workflow.evidence_refs:
            negative_evidence.append(
                NegativeEvidenceRecord(
                    id=self._stable_id("neg", workflow.id, "regulated-source"),
                    expected_signal="regulated document source",
                    searched_sources=["workflow_projection", "document_records"],
                    detail="No retained document reference was found for this regulated-document packet.",
                )
            )
        ordered_items: list[dict[str, Any]] = []
        for event in domain_events:
            ordered_items.append(
                {
                    "ref_id": event.id,
                    "title": event.event_type.replace("_", " "),
                    "status": "workflow_event",
                    "reason": event.detail,
                    "priority": 5,
                    "metadata": {"stage": "workflow_projection", "workflow_type": event.workflow_type},
                }
            )
        for obligation in obligations[:6]:
            ordered_items.append(
                {
                    "ref_id": obligation.id,
                    "title": obligation.title,
                    "status": obligation.status,
                    "reason": obligation.reason,
                    "due_at": obligation.due_at.isoformat() if obligation.due_at else None,
                    "priority": obligation.priority,
                    "metadata": {"stage": "obligations", **obligation.metadata},
                }
            )
        for decision in decisions[:4]:
            ordered_items.append(
                {
                    "ref_id": decision.id,
                    "title": decision.decision_kind,
                    "status": decision.decision_value,
                    "reason": decision.rationale,
                    "priority": 4,
                    "metadata": {"stage": "decisions", **decision.metadata},
                }
            )
        for item in user_private_memory:
            ordered_items.append(
                {
                    "ref_id": str(item.get("id") or item.get("key") or ""),
                    "title": str(item.get("key") or item.get("title") or "memory"),
                    "status": "user_private_memory",
                    "reason": str(item.get("value") or ""),
                    "priority": 4,
                    "metadata": {"stage": "user_private_memory", **(item.get("provenance") or {})},
                }
            )
        for item in workspace_memory:
            ordered_items.append(
                {
                    "ref_id": str(item.get("id") or item.get("key") or ""),
                    "title": str(item.get("key") or item.get("title") or "memory"),
                    "status": "workspace_memory",
                    "reason": str(item.get("value") or ""),
                    "priority": 3,
                    "metadata": {"stage": "workspace_memory", **(item.get("provenance") or {})},
                }
            )
        for draft in correspondence_items[:4]:
            ordered_items.append(
                {
                    "ref_id": str(draft.get("id") or self._stable_id("draft", workflow.id, draft.get("subject"))),
                    "title": str(draft.get("subject") or "Draft"),
                    "status": str(draft.get("status") or "draft"),
                    "reason": "Recent draft or contact history is available for this correspondence packet.",
                    "priority": 4,
                    "metadata": {"stage": "recent_drafts", **draft},
                }
            )
        for hit in document_hits:
            ordered_items.append(
                {
                    "ref_id": str(hit.metadata.get("chunk_id") or hit.source_id),
                    "title": str(hit.metadata.get("title") or hit.source_id),
                    "status": "document_retrieval",
                    "reason": str(hit.text[:220]),
                    "priority": 3,
                    "metadata": {"stage": "document_retrieval", "score": hit.score, **hit.metadata},
                }
            )
        for hit in archive_hits:
            ordered_items.append(
                {
                    "ref_id": str(hit.metadata.get("chunk_id") or hit.source_id),
                    "title": str(hit.metadata.get("title") or hit.source_id),
                    "status": "archive_retrieval",
                    "reason": str(hit.text[:220]),
                    "priority": 2,
                    "metadata": {"stage": "archive_expansion", "score": hit.score, **hit.metadata},
                }
            )
        return {
            "domain_events": domain_events,
            "obligations": obligations,
            "decisions": decisions,
            "user_private_memory": user_private_memory,
            "workspace_memory": workspace_memory,
            "correspondence_items": correspondence_items,
            "document_hits": document_hits,
            "archive_hits": archive_hits,
            "same_contact_history": same_contact_history,
            "negative_evidence": negative_evidence,
            "ordered_items": ordered_items,
        }

    def _claim_records_for_workflow(
        self,
        workflow: WorkflowRecord,
        *,
        action_type: RecommendationActionType,
        retrieval_context: dict[str, Any],
    ) -> list[ClaimRecord]:
        claims: list[ClaimRecord] = []
        domain_events = retrieval_context.get("domain_events", [])
        obligations = retrieval_context.get("obligations", [])
        claims.append(
            ClaimRecord(
                id=self._stable_id("claim", workflow.id, "workflow-active"),
                label=f"{workflow.workflow_type.replace('_', ' ')} workflow is active",
                status="supported" if domain_events or obligations or workflow.evidence_refs else "inferred",
                evidence_refs=[
                    ClaimEvidenceRef(
                        ref_id=item.id,
                        source_type="workflow_event",
                        title=item.event_type,
                        excerpt=item.detail,
                        freshness_seconds=self._age_seconds(item.created_at),
                    )
                    for item in domain_events[:2]
                ],
                rationale="The current workflow projection and event log show active state for this packet.",
                derived_from=["workflow_projection"],
            )
        )
        if workflow.workflow_type == "local_follow_up":
            drafts = retrieval_context.get("correspondence_items", [])
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", workflow.id, "draft-context"),
                    label="Existing draft or thread context exists",
                    status="supported" if drafts else "missing",
                    evidence_refs=[
                        ClaimEvidenceRef(
                            ref_id=str(item.get("id") or ""),
                            source_type="draft",
                            title=str(item.get("subject") or "Draft"),
                        )
                        for item in drafts[:2]
                        if item.get("id")
                    ],
                    rationale="Follow-up drafting should start from an existing draft or thread packet when possible.",
                    derived_from=["recent_drafts", "archive_expansion"],
                )
            )
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", workflow.id, "contact-history"),
                    label="Same-contact history supports the follow-up",
                    status="supported" if retrieval_context.get("same_contact_history") else "missing",
                    evidence_refs=[
                        ClaimEvidenceRef(
                            ref_id=str(item.get("ref_id") or ""),
                            source_type=str((item.get("metadata") or {}).get("stage") or "evidence"),
                            title=str(item.get("title") or ""),
                            excerpt=str(item.get("reason") or "")[:140],
                        )
                        for item in retrieval_context.get("ordered_items", [])[:4]
                    ],
                    rationale="Follow-up packets should be grounded in same-contact history before broader search.",
                    derived_from=["recent_drafts", "document_retrieval", "archive_expansion"],
                )
            )
        elif workflow.workflow_type == "regulated_document_lifecycle":
            candidates = int(workflow.subject_refs.get("candidate_count", 0) or 0)
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", workflow.id, "regulated-source"),
                    label="A regulated-business document candidate exists",
                    status="supported" if candidates > 0 else "missing",
                    evidence_refs=[
                        ClaimEvidenceRef(ref_id=ref, source_type="regulated_document", title="Candidate document")
                        for ref in workflow.evidence_refs[:2]
                    ],
                    rationale="Finalization packets need a retained document candidate to point at.",
                    derived_from=["workflow_projection"],
                )
            )
        elif workflow.workflow_type == "compliance_export_erasure":
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", workflow.id, "legal-state"),
                    label="Legal hold and retention state are clear",
                    status="conflicted" if workflow.blocking_reasons else "supported",
                    evidence_refs=[
                        ClaimEvidenceRef(ref_id=ref, source_type="compliance_record", title="Compliance item")
                        for ref in workflow.evidence_refs[:2]
                    ],
                    rationale="Compliance packets must not treat blocked legal state as ready.",
                    derived_from=["compliance_projection"],
                )
            )
        elif workflow.workflow_type == "review_approval_queue":
            conflict = any("conflict" in reason.lower() for reason in workflow.blocking_reasons)
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", workflow.id, "promotion-safety"),
                    label="Promotion inputs are conflict-free enough for review",
                    status="conflicted" if conflict else "supported",
                    evidence_refs=[
                        ClaimEvidenceRef(ref_id=ref, source_type="review_candidate", title="Review candidate")
                        for ref in workflow.evidence_refs[:3]
                    ],
                    rationale="Conflicting facts must stay visible instead of being silently collapsed.",
                    derived_from=["review_queue"],
                )
            )
        elif workflow.workflow_type == "scheduling_follow_through":
            attention_count = int(workflow.subject_refs.get("attention_count", 0) or 0)
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", workflow.id, "scheduled-work"),
                    label="Scheduled work still requires follow-through",
                    status="supported" if attention_count > 0 else "missing",
                    evidence_refs=[
                        ClaimEvidenceRef(ref_id=ref, source_type="scheduler_task", title="Scheduled task")
                        for ref in workflow.evidence_refs[:3]
                    ],
                    rationale="The packet should show concrete scheduled work that still needs a worker check.",
                    derived_from=["scheduler_projection"],
                )
            )
        return claims

    def _build_readiness_graph(
        self,
        *,
        workflow: WorkflowRecord,
        action_type: RecommendationActionType,
        evidence_bundle: EvidenceBundle,
        required_inputs: list[str],
    ) -> tuple[list[ReadinessNode], list[ReadinessEdge], list[MissingInputRecord]]:
        nodes: list[ReadinessNode] = []
        edges: list[ReadinessEdge] = []
        missing_inputs: list[MissingInputRecord] = []
        action_node_id = self._stable_id("readiness", workflow.id, action_type, "action")
        nodes.append(
            ReadinessNode(
                id=action_node_id,
                label=action_type.replace("_", " "),
                node_type="action",
                state="candidate",
                reason="Final worker-facing packet state is derived from the required claims and inputs below.",
                required=True,
                source_refs=list(evidence_bundle.source_refs),
            )
        )
        for claim in evidence_bundle.claims:
            state_map = {"supported": "verified", "inferred": "candidate", "missing": "missing", "conflicted": "blocked"}
            node_state = state_map.get(claim.status, "candidate")
            node_id = self._stable_id("readiness", workflow.id, claim.id)
            nodes.append(
                ReadinessNode(
                    id=node_id,
                    label=claim.label,
                    node_type="claim",
                    state=node_state,
                    reason=claim.rationale,
                    claim_id=claim.id,
                    required=claim.required,
                    source_refs=[item.ref_id for item in claim.evidence_refs],
                )
            )
            edges.append(ReadinessEdge(from_node_id=node_id, to_node_id=action_node_id, relationship="supports"))
            if claim.status == "missing":
                missing_inputs.append(
                    MissingInputRecord(
                        id=self._stable_id("missing", workflow.id, claim.id),
                        label=claim.label,
                        reason="Required evidence is still missing for this packet.",
                        required_for=action_type,
                        severity="warning",
                    )
                )
            elif claim.status == "conflicted":
                missing_inputs.append(
                    MissingInputRecord(
                        id=self._stable_id("missing", workflow.id, claim.id, "conflict"),
                        label=claim.label,
                        reason="Conflicting evidence must be resolved by a worker before this packet is trusted.",
                        required_for=action_type,
                        severity="blocking",
                    )
                )
        for index, requirement in enumerate(required_inputs, start=1):
            node_id = self._stable_id("readiness", workflow.id, action_type, "required", index)
            nodes.append(
                ReadinessNode(
                    id=node_id,
                    label=requirement.replace("_", " ").capitalize(),
                    node_type="required_input",
                    state="missing",
                    reason=f"{requirement.replace('_', ' ')} is still required before the packet can be executed safely.",
                    required=True,
                )
            )
            edges.append(ReadinessEdge(from_node_id=node_id, to_node_id=action_node_id, relationship="requires"))
            missing_inputs.append(
                MissingInputRecord(
                    id=self._stable_id("missing", workflow.id, action_type, "input", index),
                    label=requirement.replace("_", " ").capitalize(),
                    reason=f"{requirement.replace('_', ' ')} is still required before worker review can finish.",
                    required_for=action_type,
                    severity="warning",
                )
            )
        for index, reason in enumerate(workflow.blocking_reasons, start=1):
            node_id = self._stable_id("readiness", workflow.id, action_type, "block", index)
            nodes.append(
                ReadinessNode(
                    id=node_id,
                    label="Blocker",
                    node_type="blocker",
                    state="blocked",
                    reason=reason,
                    required=True,
                )
            )
            edges.append(ReadinessEdge(from_node_id=node_id, to_node_id=action_node_id, relationship="blocks"))
            missing_inputs.append(
                MissingInputRecord(
                    id=self._stable_id("missing", workflow.id, action_type, "block", index),
                    label="Resolve blocker",
                    reason=reason,
                    required_for=action_type,
                    severity="blocking",
                )
            )
        return nodes, edges, missing_inputs[:6]

    def _readiness_status_from_graph(
        self,
        *,
        readiness_nodes: list[ReadinessNode],
        required_inputs: list[str],
    ) -> str:
        if any(item.state == "blocked" and item.required for item in readiness_nodes):
            return "blocked"
        if any(item.state == "missing" and item.required for item in readiness_nodes):
            return "waiting_on_input"
        if any(
            item.state == "candidate"
            and item.required
            and item.node_type != "action"
            for item in readiness_nodes
        ):
            return "needs_review"
        if required_inputs:
            return "needs_review"
        return "ready_now"

    def _why_blocked_from_graph(
        self,
        *,
        workflow: WorkflowRecord,
        readiness_nodes: list[ReadinessNode],
        missing_inputs: list[MissingInputRecord],
    ) -> list[str]:
        reasons = [item.reason for item in readiness_nodes if item.state == "blocked" and item.reason]
        reasons.extend(item.reason for item in missing_inputs if item.severity == "blocking")
        reasons.extend(workflow.blocking_reasons)
        return list(dict.fromkeys(reason for reason in reasons if reason))[:6]

    def _generation_contract_for_recommendation(
        self,
        *,
        workflow: WorkflowRecord,
        action_type: RecommendationActionType,
        readiness_status: str,
    ) -> GenerationContract:
        if readiness_status in {"blocked", "waiting_on_input"}:
            return GenerationContract(
                mode="clarify" if action_type in {"missing_context", "blocked_item", "suggested_draft", "follow_up_candidate"} else "explain_only",
                allow_draft=False,
                allow_summarize=True,
                allow_clarify=True,
                allow_explain_only=True,
                note="Packet is not ready. Only clarification or blocker explanation is allowed.",
            )
        if action_type in {"suggested_draft", "follow_up_candidate"}:
            if readiness_status != "ready_now":
                return GenerationContract(
                    mode="summarize" if readiness_status == "needs_review" else "clarify",
                    allow_draft=False,
                    allow_summarize=True,
                    allow_clarify=True,
                    allow_explain_only=True,
                    note="Packet still needs grounded review before wording should be generated.",
                )
            return GenerationContract(
                mode="draft",
                allow_draft=True,
                allow_summarize=True,
                allow_clarify=True,
                allow_explain_only=True,
                note="Draft wording may be generated from the deterministic scaffold and evidence pack.",
            )
        return GenerationContract(
            mode="summarize" if workflow.workflow_type in {"review_approval_queue", "compliance_export_erasure"} else "explain_only",
            allow_draft=False,
            allow_summarize=True,
            allow_clarify=True,
            allow_explain_only=True,
            note="This packet is evidence-first. The language layer may summarize or explain it, but not invent execution state.",
        )

    def _workflow_query(self, workflow: WorkflowRecord) -> str:
        parts = [
            str(workflow.workflow_type).replace("_", " "),
            workflow.last_event,
            workflow.next_expected_step,
            " ".join(str(key) for key in workflow.subject_refs.keys()),
            " ".join(str(value) for value in workflow.subject_refs.values()),
        ]
        for item in (workflow.metadata or {}).get("drafts", [])[:3]:
            parts.append(str(item.get("subject") or ""))
            parts.append(" ".join(str(target) for target in item.get("to", []) or []))
        return " ".join(part for part in parts if part).strip()

    def _freshness_seconds(
        self,
        updated_at: datetime | None,
        domain_events: list[WorkflowDomainEvent],
        obligations: list[ObligationRecord],
    ) -> float | None:
        timestamps = [item.created_at for item in domain_events]
        timestamps.extend(item.updated_at for item in obligations if item.updated_at)
        if updated_at:
            timestamps.append(updated_at)
        if not timestamps:
            return None
        latest = max(self._normalize_datetime(item) or datetime.min.replace(tzinfo=timezone.utc) for item in timestamps)
        return round(self._age_seconds(latest) or 0.0, 3)

    def _age_seconds(self, value: datetime | None) -> float | None:
        normalized = self._normalize_datetime(value)
        if normalized is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - normalized).total_seconds())

    def _record_shadow_ranking(self, recommendation: RecommendationRecord) -> None:
        shadow_score = round(
            (recommendation.ranking_explanation.features.workflow_match * 0.2)
            + (recommendation.ranking_explanation.features.urgency_score * 0.18)
            + (recommendation.evidence_bundle.coverage_score * 0.22)
            + (0.18 if not recommendation.evidence_bundle.negative_evidence else 0.0)
            + (recommendation.ranking_explanation.features.entity_match_score * 0.12)
            + (recommendation.ranking_explanation.features.approval_score * 0.1),
            4,
        )
        self.memory.record_shadow_ranking(
            ShadowRankingRecord(
                id=self._stable_id("shadow", recommendation.id, "coverage_v1"),
                profile_slug=self.profile.slug,
                organization_id=recommendation.organization_id,
                workspace_slug=recommendation.workspace_slug,
                actor_user_id=recommendation.actor_user_id,
                workflow_id=recommendation.workflow_id,
                recommendation_id=recommendation.id,
                policy_name="coverage_v1",
                score=shadow_score,
                features={
                    "coverage_score": recommendation.evidence_bundle.coverage_score,
                    "negative_evidence_count": len(recommendation.evidence_bundle.negative_evidence),
                    "workflow_match": recommendation.ranking_explanation.features.workflow_match,
                    "urgency_score": recommendation.ranking_explanation.features.urgency_score,
                    "entity_match_score": recommendation.ranking_explanation.features.entity_match_score,
                },
                outcome={"ready_now": recommendation.readiness_status == "ready_now"},
            )
        )

    def _urgency_score(self, due_at: datetime | None, obligations: list[ObligationRecord]) -> float:
        now = datetime.now(timezone.utc)
        if any(item.status == "blocked" for item in obligations):
            return 1.0
        normalized_due_at = self._normalize_datetime(due_at)
        if normalized_due_at is None:
            return 0.45 if obligations else 0.2
        remaining = (normalized_due_at - now).total_seconds()
        if remaining <= 0:
            return 1.0
        if remaining <= 86400:
            return 0.85
        if remaining <= 86400 * 3:
            return 0.7
        return 0.5

    def _stable_id(self, *parts: object) -> str:
        raw = "|".join(str(part or "") for part in parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _normalize_datetime(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
