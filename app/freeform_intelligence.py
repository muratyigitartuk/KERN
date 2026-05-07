from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any, Callable

from app.document_intelligence import DocumentIntelligenceService
from app.intelligence import IntelligenceService
from app.memory import MemoryRepository
from app.retrieval import RetrievalService
from app.types import (
    ClaimEvidenceRef,
    ClaimRecord,
    ContextLinkRecord,
    EvidenceBundle,
    FreeformIntentRecord,
    GenerationContract,
    MissingInputRecord,
    NegativeEvidenceRecord,
    PersonContextPacket,
    PreparationPacket,
    ProfileSummary,
    RankingExplanation,
    RankingFeatureVector,
    ReadinessEdge,
    ReadinessNode,
    ThreadContextPacket,
)


class FreeformIntelligenceService:
    TOKEN_PATTERN = re.compile(r"[0-9A-Za-zÃ„Ã–ÃœÃ¤Ã¶Ã¼ÃŸ]+")
    TOKEN_PATTERN = re.compile(r"[0-9A-Za-zÃ„Ã–ÃœÃ¤Ã¶Ã¼ÃŸ@._+-]+")
    TOKEN_PATTERN = re.compile(r"[0-9A-Za-zÃ„Ã–ÃœÃ¤Ã¶Ã¼ÃŸ]+")
    QUERY_TOKEN_PATTERN = re.compile(r"[0-9A-Za-zÃ„Ã–ÃœÃ¤Ã¶Ã¼ÃŸ]+")
    PREPARED_WORK_MARKERS = (
        "what should",
        "what next",
        "follow up",
        "follow-up",
        "prepared work",
        "review",
        "finalize",
        "export",
        "erasure",
    )
    THREAD_MARKERS = (
        "last time",
        "previous thread",
        "thread",
        "email chain",
        "message chain",
        "what did we tell",
        "what did i tell",
        "previous email",
        "earlier email",
    )
    PERSON_MARKERS = (
        "client",
        "customer",
        "supplier",
        "vendor",
        "contact",
        "person",
        "customer thread",
        "supplier thread",
    )
    WORKSPACE_MARKERS = (
        "workspace",
        "what do we have",
        "what's in here",
        "what is in here",
        "local files",
        "uploaded docs",
    )

    def __init__(
        self,
        memory: MemoryRepository,
        profile: ProfileSummary,
        *,
        retrieval: RetrievalService,
        intelligence: IntelligenceService,
        document_intelligence: DocumentIntelligenceService,
        preparation_packet_getter: Callable[..., PreparationPacket | None],
        recommendation_lister: Callable[..., list],
    ) -> None:
        self.memory = memory
        self.profile = profile
        self.retrieval = retrieval
        self.intelligence = intelligence
        self.document_intelligence = document_intelligence
        self.preparation_packet_getter = preparation_packet_getter
        self.recommendation_lister = recommendation_lister

    def classify_intent(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        selected_document_ids: list[str] | None = None,
    ) -> FreeformIntentRecord:
        lowered = transcript.strip().lower()
        query_terms = self._query_terms(transcript)
        document_intent = self.document_intelligence.classify_task_intent(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            selected_document_ids=selected_document_ids,
        )
        matched_contacts: list[dict[str, Any]] = []
        thread_candidates: list[dict[str, Any]] = []

        scores: dict[str, float] = defaultdict(float)
        reasons: dict[str, list[str]] = defaultdict(list)
        linked_refs: list[str] = []
        doc_families = {
            "document_qa",
            "document_citation",
            "document_summary",
            "document_key_sections",
            "document_compare",
            "clarification_needed",
        }
        if document_intent.task_family in doc_families:
            scores[document_intent.task_family] = max(scores[document_intent.task_family], document_intent.confidence)
            reasons[document_intent.task_family].extend(document_intent.reasons)
            linked_refs.extend(document_intent.selected_document_ids)

        prepared_hits = sum(1 for marker in self.PREPARED_WORK_MARKERS if marker in lowered)
        if prepared_hits:
            scores["prepared_work"] = 0.36 + min(0.45, prepared_hits * 0.16)
            reasons["prepared_work"].append("The request matches worker-facing follow-up or preparation language.")

        thread_hits = sum(1 for marker in self.THREAD_MARKERS if marker in lowered)
        if thread_candidates:
            scores["thread_qa"] = max(
                scores["thread_qa"],
                0.34 + min(0.48, len(thread_candidates) * 0.14) + min(0.18, thread_hits * 0.08),
            )
            reasons["thread_qa"].append("Local thread context matched the request.")
            linked_refs.extend(candidate["refs"][:2] for candidate in thread_candidates[:2])  # type: ignore[arg-type]
        elif thread_hits:
            scores["thread_qa"] = 0.42
            reasons["thread_qa"].append("The request asks about prior thread context but KERN needs a clearer match.")

        person_hits = sum(1 for marker in self.PERSON_MARKERS if marker in lowered)
        if matched_contacts:
            scores["person_context"] = max(
                scores["person_context"],
                0.34 + min(0.5, len(matched_contacts) * 0.16) + min(0.15, person_hits * 0.08),
            )
            reasons["person_context"].append("The request matched a known contact/customer pattern.")
            linked_refs.extend(item["refs"][:2] for item in matched_contacts[:2])  # type: ignore[arg-type]
        elif person_hits:
            scores["person_context"] = 0.4
            reasons["person_context"].append("The request asks about a customer/contact but no single identity is clear yet.")

        workspace_hits = sum(1 for marker in self.WORKSPACE_MARKERS if marker in lowered)
        if workspace_hits:
            scores["workspace_context"] = 0.3 + min(0.35, workspace_hits * 0.12)
            reasons["workspace_context"].append("The request asks about current local workspace context.")

        if (
            any(family.startswith("document_") for family in scores)
            and ("thread_qa" in scores or "person_context" in scores or "workspace_context" in scores)
        ):
            scores["cross_context_question"] = min(0.9, max(scores.values(), default=0.0) - 0.02)
            reasons["cross_context_question"].append("The request spans document evidence and broader local context together.")

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        if not ranked:
            return FreeformIntentRecord(
                id=self._stable_id("freeform", transcript, workspace_slug or self.profile.slug),
                transcript=transcript,
                task_family="general_chat_fallback",
                confidence=0.0,
                reasons=["No strong local deterministic route was found."],
            )

        top_family, top_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        ambiguity_count = 0
        clarification_reason = ""
        clarification_required = False
        if top_family == "cross_context_question":
            clarification_required = True
            ambiguity_count = max(1, len([item for item in ranked[:3] if item[1] >= top_score - 0.08]))
            clarification_reason = "The request spans multiple local context types and needs a clearer target first."
        elif top_family == "thread_qa" and len(thread_candidates) > 1 and top_score - second_score < 0.12:
            clarification_required = True
            ambiguity_count = len(thread_candidates[:3])
            clarification_reason = "Multiple plausible local threads match this request."
        elif top_family == "person_context" and len(matched_contacts) > 1 and top_score - second_score < 0.12:
            clarification_required = True
            ambiguity_count = len(matched_contacts[:3])
            clarification_reason = "Multiple plausible contacts match this request."
        elif top_family == "person_context" and len(matched_contacts) > 1:
            alias_counts: dict[str, int] = defaultdict(int)
            for item in matched_contacts[:4]:
                alias = str(item["label"]).split("@", 1)[0].strip().lower()
                if alias:
                    alias_counts[alias] += 1
            if any(alias_counts.get(term, 0) > 1 for term in query_terms):
                clarification_required = True
                ambiguity_count = len(matched_contacts[:3])
                clarification_reason = "Multiple plausible contacts share that local name."
        accept_threshold = {
            "document_qa": 0.47,
            "document_citation": 0.47,
            "document_summary": 0.47,
            "document_key_sections": 0.47,
            "document_compare": 0.47,
            "prepared_work": 0.5,
            "thread_qa": 0.58,
            "person_context": 0.54,
            "workspace_context": 0.52,
            "cross_context_question": 0.74,
        }.get(top_family, 0.72)
        clarify_threshold = {
            "document_qa": 0.35,
            "document_citation": 0.35,
            "document_summary": 0.35,
            "document_key_sections": 0.35,
            "document_compare": 0.35,
            "prepared_work": 0.36,
            "thread_qa": 0.4,
            "person_context": 0.38,
            "workspace_context": 0.36,
            "cross_context_question": 0.48,
        }.get(top_family, 0.46)
        if top_score < clarify_threshold:
            top_family = "general_chat_fallback"
        elif top_score < accept_threshold:
            clarification_required = True
            ambiguity_count = max(ambiguity_count, 1)
            clarification_reason = clarification_reason or "KERN has a possible local route, but the target is not strong enough to trust yet."
        if document_intent.task_family == "clarification_needed" and document_intent.clarification_required:
            top_family = "clarification_needed"
            clarification_required = True
            ambiguity_count = max(ambiguity_count, document_intent.ambiguity_count or 1)
            clarification_reason = document_intent.clarification_reason or document_intent.clarification_prompt
        if clarification_required and top_family != "general_chat_fallback":
            top_family = "clarification_needed"
        return FreeformIntentRecord(
            id=self._stable_id("freeform", transcript, workspace_slug or self.profile.slug),
            transcript=transcript,
            task_family=top_family,  # type: ignore[arg-type]
            confidence=round(top_score, 4),
            reasons=list(dict.fromkeys(reasons.get(top_family, []) or document_intent.reasons or ["KERN found a local route."]))[:6],
            selected_document_ids=list(document_intent.selected_document_ids),
            linked_entity_refs=self._flatten_refs(linked_refs)[:12],
            ambiguity_count=ambiguity_count,
            clarification_required=clarification_required,
            clarification_prompt=clarification_reason,
            clarification_reason=clarification_reason,
        )

    def route_freeform(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        selected_document_ids: list[str] | None = None,
    ) -> dict[str, object]:
        intent = self.classify_intent(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            selected_document_ids=selected_document_ids,
        )
        packet_type = None
        packet = None
        clarification_source = self._clarification_source_family(transcript, intent) if intent.task_family == "clarification_needed" else ""
        if intent.task_family in {
            "document_qa",
            "document_citation",
            "document_summary",
            "document_key_sections",
            "document_compare",
        } or (intent.task_family == "clarification_needed" and clarification_source == "document"):
            packet = self.document_intelligence.build_document_answer_packet(
                transcript,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                selected_document_ids=selected_document_ids,
            )
            if packet is not None:
                packet_type = "document_answer_packet"
        elif intent.task_family == "prepared_work":
            packet = self.preparation_packet_getter(
                transcript,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
            )
            if packet is not None:
                packet_type = "preparation_packet"
        elif intent.task_family == "thread_qa" or (intent.task_family == "clarification_needed" and clarification_source == "thread"):
            packet = self.build_thread_context_packet(
                transcript,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                intent=intent,
            )
            packet_type = "thread_context_packet"
        elif intent.task_family == "person_context" or (intent.task_family == "clarification_needed" and clarification_source == "person"):
            packet = self.build_person_context_packet(
                transcript,
                organization_id=organization_id,
                workspace_slug=workspace_slug,
                actor_user_id=actor_user_id,
                intent=intent,
            )
            packet_type = "person_context_packet"
        return {"task_intent": intent, "packet_type": packet_type, "packet": packet}

    def build_thread_context_packet(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        intent: FreeformIntentRecord | None = None,
    ) -> ThreadContextPacket:
        intent = intent or self.classify_intent(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        matched_contacts: list[dict[str, Any]] = []
        thread_candidates: list[dict[str, Any]] = []
        recommendations = self.recommendation_lister(
            organization_id=organization_id,
            workspace_slug=workspace_slug or self.profile.slug,
            actor_user_id=actor_user_id,
        )
        selected_candidate = thread_candidates[0] if thread_candidates else None
        ambiguity_count = max(intent.ambiguity_count, len(thread_candidates[:3]) if len(thread_candidates) > 1 else 0)
        clarification_reason = intent.clarification_reason
        if len(thread_candidates) > 1 and (thread_candidates[0]["score"] - thread_candidates[1]["score"]) < 0.12:
            selected_candidate = None
            clarification_reason = clarification_reason or "Multiple plausible threads match this request."
        resolved_refs = list(selected_candidate["refs"]) if selected_candidate else []
        related_rec_titles = self._related_recommendations(transcript, recommendations, contact_refs=resolved_refs)
        memory_support = self.intelligence.retrieve_memory_context(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug or self.profile.slug,
            user_id=actor_user_id,
            limit=4,
        )
        evidence_items: list[dict[str, object]] = []
        if selected_candidate:
            for entry in selected_candidate["entries"][:4]:
                evidence_items.append(
                    {
                        "ref_id": entry["ref_id"],
                        "title": entry["title"],
                        "status": entry["kind"],
                        "reason": entry["summary"],
                        "priority": 3,
                        "metadata": {"stage": "thread_context", "contact": entry.get("contact"), "subject": entry.get("subject")},
                    }
                )
        for item in memory_support[:3]:
            provenance = item.get("provenance") or {}
            if not provenance.get("policy_safe", True):
                continue
            evidence_items.append(
                {
                    "ref_id": str(item.get("id") or item.get("key") or ""),
                    "title": str(item.get("key") or "memory"),
                    "status": "memory_support",
                    "reason": str(item.get("value") or ""),
                    "priority": 2,
                    "metadata": {"stage": "memory_support", **provenance},
                }
            )
        if related_rec_titles:
            evidence_items.append(
                {
                    "ref_id": self._stable_id("thread-related", transcript),
                    "title": "Related prepared work",
                    "status": "workflow_context",
                    "reason": " | ".join(related_rec_titles[:3]),
                    "priority": 2,
                    "metadata": {"stage": "recommendation_context"},
                }
            )
        claims = [
            ClaimRecord(
                id=self._stable_id("thread-claim", transcript, "resolved"),
                label="Thread target is resolved",
                status="supported" if selected_candidate else "missing",
                evidence_refs=[
                    ClaimEvidenceRef(ref_id=ref, source_type="local_thread_context", title=selected_candidate["title"])
                    for ref in resolved_refs[:3]
                ],
                rationale="Thread questions need one clear local context target.",
                derived_from=["structured_memory_items", "context_links"],
            ),
            ClaimRecord(
                id=self._stable_id("thread-claim", transcript, "history"),
                label="Same-contact history is available",
                status="supported" if len(resolved_refs) >= 2 or matched_contacts else "missing",
                evidence_refs=[
                    ClaimEvidenceRef(ref_id=ref, source_type="contact_link", title=ref)
                    for ref in self._flatten_refs([item["refs"][:1] for item in matched_contacts[:2]])
                ],
                rationale="Thread answers are stronger when KERN can connect the thread to recurring contact history.",
                derived_from=["context_links", "structured_memory_items"],
            ),
        ]
        negative_evidence: list[NegativeEvidenceRecord] = []
        missing_inputs: list[MissingInputRecord] = []
        if not selected_candidate:
            negative_evidence.append(
                NegativeEvidenceRecord(
                    id=self._stable_id("thread-neg", transcript, "target"),
                    expected_signal="resolved thread target",
                    searched_sources=["structured_memory_items", "context_links"],
                    detail=clarification_reason or "KERN could not resolve one clear prior thread from local context.",
                )
            )
            missing_inputs.append(
                MissingInputRecord(
                    id=self._stable_id("thread-missing", transcript, "target"),
                    label="Choose the local thread or context target",
                    reason=clarification_reason or "KERN needs a clearer thread target before answering confidently.",
                    required_for="thread_qa",
                    severity="warning",
                )
            )
        support_breadth = min(1.0, round((len(resolved_refs) * 0.18) + (len(memory_support) * 0.08) + (len(related_rec_titles) * 0.06), 4))
        evidence = EvidenceBundle(
            id=self._stable_id("thread-evidence", transcript, workspace_slug or self.profile.slug),
            summary="Thread context evidence pack.",
            why_selected=list(dict.fromkeys(intent.reasons or ["KERN matched the request to prior thread activity."]))[:5],
            scope="workspace",
            confidence=min(0.95, round((intent.confidence * 0.55) + (support_breadth * 0.35), 4)),
            policy_safe=True,
            source_refs=resolved_refs[:8],
            action_relevance=min(1.0, 0.35 + support_breadth * 0.5),
            items=evidence_items,
            claims=claims,
            negative_evidence=negative_evidence,
            coverage_score=support_breadth,
            freshness=None,
            ranking_explanation=RankingExplanation(
                score=support_breadth,
                reasons=list(dict.fromkeys(intent.reasons + ([f"Matched thread target: {selected_candidate['title']}"] if selected_candidate else [])))[:5],
                features=RankingFeatureVector(entity_match_score=min(1.0, len(matched_contacts) * 0.3), recency_score=min(1.0, len(resolved_refs) * 0.2)),
            ),
        )
        readiness_nodes = [
            ReadinessNode(
                id=self._stable_id("thread-node", transcript, "target"),
                label="Thread target resolved",
                node_type="thread_target",
                state="verified" if selected_candidate else "missing",
                reason=clarification_reason or "A clear prior local thread is required.",
                required=True,
                source_refs=resolved_refs[:4],
            ),
            ReadinessNode(
                id=self._stable_id("thread-node", transcript, "history"),
                label="Thread history has enough support",
                node_type="evidence",
                state="verified" if support_breadth >= 0.35 and selected_candidate else "missing",
                reason="KERN needs enough grounded history before summarizing a thread confidently.",
                required=True,
                source_refs=resolved_refs[:4],
            ),
        ]
        readiness_edges = [ReadinessEdge(from_node_id=readiness_nodes[0].id, to_node_id=readiness_nodes[1].id, relationship="supports")]
        readiness_status = "ready_now" if all(node.state == "verified" for node in readiness_nodes) else "waiting_on_input"
        generation_contract = (
            GenerationContract(mode="answer", allow_answer=True, allow_summarize=True, allow_clarify=True, allow_explain_only=True, note="Thread answers must stay within linked local thread evidence.")
            if readiness_status == "ready_now"
            else GenerationContract(mode="clarify", allow_answer=False, allow_summarize=False, allow_clarify=True, allow_explain_only=True, note="Thread target is not strong enough yet. KERN should clarify or explain the blocker.")
        )
        packet = ThreadContextPacket(
            id=self._stable_id("thread-packet", transcript, workspace_slug or self.profile.slug),
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug or self.profile.slug,
            actor_user_id=actor_user_id,
            task_intent=intent,
            query_text=transcript,
            title=selected_candidate["title"] if selected_candidate else "Thread context",
            summary="Grounded thread context from local structured memory and context links.",
            thread_refs=resolved_refs,
            linked_entity_refs=self._flatten_refs([resolved_refs, *[item["refs"][:2] for item in matched_contacts[:2]]])[:12],
            resolution_confidence=round(intent.confidence if selected_candidate else min(0.69, intent.confidence), 4),
            ambiguity_count=ambiguity_count,
            support_breadth=support_breadth,
            clarification_reason=clarification_reason,
            readiness_status=readiness_status,
            why_ready=["KERN resolved one thread target and found supporting local history."] if readiness_status == "ready_now" else [],
            why_blocked=[] if readiness_status == "ready_now" else [clarification_reason or "Thread history is still too ambiguous."],
            blocker_details=[] if readiness_status == "ready_now" else [clarification_reason or "Thread history is still too ambiguous."],
            missing_inputs=missing_inputs,
            readiness_nodes=readiness_nodes,
            readiness_edges=readiness_edges,
            evidence_pack=evidence,
            generation_contract=generation_contract,
            worker_review_required=readiness_status != "ready_now",
            deterministic_answer=self._thread_answer(selected_candidate, related_rec_titles=related_rec_titles, matched_contacts=matched_contacts),
        )
        self.memory.store_thread_context_packet(packet)
        self._record_thread_links(packet, selected_candidate=selected_candidate, matched_contacts=matched_contacts)
        return packet

    def build_person_context_packet(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        intent: FreeformIntentRecord | None = None,
    ) -> PersonContextPacket:
        intent = intent or self.classify_intent(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        matched_contacts: list[dict[str, Any]] = []
        recommendations = self.recommendation_lister(
            organization_id=organization_id,
            workspace_slug=workspace_slug or self.profile.slug,
            actor_user_id=actor_user_id,
        )
        selected_contact = matched_contacts[0] if matched_contacts else None
        ambiguity_count = max(intent.ambiguity_count, len(matched_contacts[:3]) if len(matched_contacts) > 1 else 0)
        clarification_reason = intent.clarification_reason
        if len(matched_contacts) > 1 and (matched_contacts[0]["score"] - matched_contacts[1]["score"]) < 0.12:
            selected_contact = None
            clarification_reason = clarification_reason or "Multiple plausible contacts match this request."
        contact_refs = list(selected_contact["refs"]) if selected_contact else []
        related_rec_titles = self._related_recommendations(transcript, recommendations, contact_refs=contact_refs)
        memory_support = self.intelligence.retrieve_memory_context(
            transcript if not selected_contact else f"{transcript} {selected_contact['label']}",
            organization_id=organization_id,
            workspace_slug=workspace_slug or self.profile.slug,
            user_id=actor_user_id,
            limit=4,
        )
        evidence_items: list[dict[str, object]] = []
        if selected_contact:
            for interaction in selected_contact["interactions"][:4]:
                evidence_items.append(
                    {
                        "ref_id": interaction["ref_id"],
                        "title": interaction["title"],
                        "status": interaction["kind"],
                        "reason": interaction["summary"],
                        "priority": 3,
                        "metadata": {"stage": "person_context", "contact": selected_contact["label"]},
                    }
                )
        for item in memory_support[:3]:
            provenance = item.get("provenance") or {}
            if not provenance.get("policy_safe", True):
                continue
            evidence_items.append(
                {
                    "ref_id": str(item.get("id") or item.get("key") or ""),
                    "title": str(item.get("key") or "memory"),
                    "status": "memory_support",
                    "reason": str(item.get("value") or ""),
                    "priority": 2,
                    "metadata": {"stage": "memory_support", **provenance},
                }
            )
        claims = [
            ClaimRecord(
                id=self._stable_id("person-claim", transcript, "resolved"),
                label="Contact or customer identity is resolved",
                status="supported" if selected_contact else "missing",
                evidence_refs=[
                    ClaimEvidenceRef(ref_id=ref, source_type="contact_link", title=selected_contact["label"])
                    for ref in contact_refs[:3]
                ] if selected_contact else [],
                rationale="People-aware answers need one grounded identity target.",
                derived_from=["structured_memory_items", "context_links"],
            ),
            ClaimRecord(
                id=self._stable_id("person-claim", transcript, "history"),
                label="Recent interactions support the answer",
                status="supported" if selected_contact and len(selected_contact["interactions"]) >= 1 else "missing",
                evidence_refs=[
                    ClaimEvidenceRef(ref_id=item["ref_id"], source_type=item["kind"], title=item["title"], excerpt=item["summary"][:140])
                    for item in (selected_contact["interactions"][:3] if selected_contact else [])
                ],
                rationale="KERN should not make a person-context answer feel strong without recent grounded interactions.",
                derived_from=["structured_memory_items", "context_links"],
            ),
        ]
        negative_evidence: list[NegativeEvidenceRecord] = []
        missing_inputs: list[MissingInputRecord] = []
        if not selected_contact:
            negative_evidence.append(
                NegativeEvidenceRecord(
                    id=self._stable_id("person-neg", transcript, "identity"),
                    expected_signal="resolved contact identity",
                    searched_sources=["structured_memory_items", "context_links"],
                    detail=clarification_reason or "KERN could not resolve one clear customer/contact from local context.",
                )
            )
            missing_inputs.append(
                MissingInputRecord(
                    id=self._stable_id("person-missing", transcript, "identity"),
                    label="Choose the contact or customer",
                    reason=clarification_reason or "KERN needs a clearer identity target before answering confidently.",
                    required_for="person_context",
                    severity="warning",
                )
            )
        support_breadth = min(1.0, round((len(contact_refs) * 0.16) + (len(memory_support) * 0.08) + (len(related_rec_titles) * 0.05), 4))
        evidence = EvidenceBundle(
            id=self._stable_id("person-evidence", transcript, workspace_slug or self.profile.slug),
            summary="Person/context evidence pack.",
            why_selected=list(dict.fromkeys(intent.reasons or ["KERN matched the request to local contact history."]))[:5],
            scope="workspace",
            confidence=min(0.95, round((intent.confidence * 0.55) + (support_breadth * 0.32), 4)),
            policy_safe=True,
            source_refs=contact_refs[:8],
            action_relevance=min(1.0, 0.3 + support_breadth * 0.5),
            items=evidence_items,
            claims=claims,
            negative_evidence=negative_evidence,
            coverage_score=support_breadth,
            freshness=None,
            ranking_explanation=RankingExplanation(
                score=support_breadth,
                reasons=list(dict.fromkeys(intent.reasons + ([f"Matched contact: {selected_contact['label']}"] if selected_contact else [])))[:5],
                features=RankingFeatureVector(entity_match_score=min(1.0, len(contact_refs) * 0.18), recency_score=min(1.0, len(contact_refs) * 0.12)),
            ),
        )
        readiness_nodes = [
            ReadinessNode(
                id=self._stable_id("person-node", transcript, "identity"),
                label="Contact identity resolved",
                node_type="person_target",
                state="verified" if selected_contact else "missing",
                reason=clarification_reason or "A specific contact is required.",
                required=True,
                source_refs=contact_refs[:4],
            ),
            ReadinessNode(
                id=self._stable_id("person-node", transcript, "history"),
                label="Grounded interaction history exists",
                node_type="evidence",
                state="verified" if selected_contact and support_breadth >= 0.3 else "missing",
                reason="KERN needs enough grounded contact history before answering confidently.",
                required=True,
                source_refs=contact_refs[:4],
            ),
        ]
        readiness_edges = [ReadinessEdge(from_node_id=readiness_nodes[0].id, to_node_id=readiness_nodes[1].id, relationship="supports")]
        readiness_status = "ready_now" if all(node.state == "verified" for node in readiness_nodes) else "waiting_on_input"
        generation_contract = (
            GenerationContract(mode="answer", allow_answer=True, allow_summarize=True, allow_clarify=True, allow_explain_only=True, note="Person-context answers must stay within grounded local interactions and policy-safe memory.")
            if readiness_status == "ready_now"
            else GenerationContract(mode="clarify", allow_answer=False, allow_summarize=False, allow_clarify=True, allow_explain_only=True, note="The contact identity is still too weak or ambiguous for a grounded answer.")
        )
        packet = PersonContextPacket(
            id=self._stable_id("person-packet", transcript, workspace_slug or self.profile.slug),
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug or self.profile.slug,
            actor_user_id=actor_user_id,
            task_intent=intent,
            query_text=transcript,
            title=selected_contact["label"] if selected_contact else "Person context",
            summary="Grounded person/customer context from local interactions and policy-safe memory.",
            person_ref=selected_contact["label"] if selected_contact else None,
            linked_entity_refs=contact_refs[:12],
            resolution_confidence=round(intent.confidence if selected_contact else min(0.69, intent.confidence), 4),
            ambiguity_count=ambiguity_count,
            support_breadth=support_breadth,
            clarification_reason=clarification_reason,
            readiness_status=readiness_status,
            why_ready=["KERN resolved one contact identity and found grounded recent interactions."] if readiness_status == "ready_now" else [],
            why_blocked=[] if readiness_status == "ready_now" else [clarification_reason or "The contact target is still ambiguous."],
            blocker_details=[] if readiness_status == "ready_now" else [clarification_reason or "The contact target is still ambiguous."],
            missing_inputs=missing_inputs,
            readiness_nodes=readiness_nodes,
            readiness_edges=readiness_edges,
            evidence_pack=evidence,
            generation_contract=generation_contract,
            worker_review_required=readiness_status != "ready_now",
            deterministic_answer=self._person_answer(selected_contact, related_rec_titles=related_rec_titles),
        )
        self.memory.store_person_context_packet(packet)
        if selected_contact:
            self._record_person_links(packet, selected_contact=selected_contact)
        return packet

    def get_thread_context_packet(self, packet_id: str) -> ThreadContextPacket | None:
        return self.memory.get_thread_context_packet(packet_id)

    def get_person_context_packet(self, packet_id: str) -> PersonContextPacket | None:
        return self.memory.get_person_context_packet(packet_id)

    def _clarification_source_family(self, transcript: str, intent: FreeformIntentRecord) -> str:
        lowered = transcript.lower()
        if intent.selected_document_ids or any(
            marker in lowered for marker in DocumentIntelligenceService.DOCUMENT_MARKERS
        ):
            return "document"
        if any(marker in lowered for marker in self.THREAD_MARKERS):
            return "thread"
        if any(marker in lowered for marker in self.PERSON_MARKERS) or "what matters for" in lowered:
            return "person"
        return "generic"

    def _query_terms(self, text: str) -> list[str]:
        return [
            term.lower()
            for term in re.findall(r"[^\W\d_]{2,}|[0-9]+(?:[.,][0-9]+)*", text, flags=re.UNICODE)
        ]

    def _related_recommendations(
        self,
        transcript: str,
        recommendations: list[Any],
        *,
        contact_refs: list[str] | None = None,
    ) -> list[str]:
        terms = set(self._query_terms(transcript))
        refs = set(contact_refs or [])
        ranked: list[tuple[float, str]] = []
        for recommendation in recommendations:
            score = 0.0
            haystack_terms = set(self._query_terms(f"{recommendation.title} {recommendation.reason}"))
            score += len(terms.intersection(haystack_terms)) * 0.14
            if refs and refs.intersection(set(recommendation.evidence_bundle.source_refs or [])):
                score += 0.24
            if score > 0:
                ranked.append((score, recommendation.title))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [title for _, title in ranked[:4]]

    def _record_thread_links(
        self,
        packet: ThreadContextPacket,
        *,
        selected_candidate: dict[str, Any] | None,
        matched_contacts: list[dict[str, Any]],
    ) -> None:
        if not selected_candidate:
            return
        thread_refs = list(dict.fromkeys(selected_candidate["refs"]))[:6]
        for ref in thread_refs[1:]:
            self.memory.record_context_link(
                ContextLinkRecord(
                    id=self._stable_id("thread-link", packet.id, thread_refs[0], ref),
                    profile_slug=self.profile.slug,
                    organization_id=packet.organization_id,
                    workspace_slug=packet.workspace_slug,
                    actor_user_id=packet.actor_user_id,
                    link_type="same_thread",
                    source_ref=thread_refs[0],
                    target_ref=ref,
                    strength=0.88,
                    reasons=["Same local thread packet grouped these refs together."],
                    metadata={"packet_id": packet.id},
                )
            )
        for contact in matched_contacts[:3]:
            for ref in contact["refs"][:2]:
                self.memory.record_context_link(
                    ContextLinkRecord(
                        id=self._stable_id("contact-thread-link", packet.id, ref, thread_refs[0]),
                        profile_slug=self.profile.slug,
                        organization_id=packet.organization_id,
                        workspace_slug=packet.workspace_slug,
                        actor_user_id=packet.actor_user_id,
                        link_type="same_contact",
                        source_ref=ref,
                        target_ref=thread_refs[0],
                        strength=0.72,
                        reasons=["Contact history and thread packet matched the same local interaction cluster."],
                        metadata={"packet_id": packet.id},
                    )
                )

    def _record_person_links(self, packet: PersonContextPacket, *, selected_contact: dict[str, Any]) -> None:
        refs = list(dict.fromkeys(selected_contact["refs"]))[:6]
        if not refs:
            return
        for ref in refs[1:]:
            self.memory.record_context_link(
                ContextLinkRecord(
                    id=self._stable_id("person-link", packet.id, refs[0], ref),
                    profile_slug=self.profile.slug,
                    organization_id=packet.organization_id,
                    workspace_slug=packet.workspace_slug,
                    actor_user_id=packet.actor_user_id,
                    link_type="same_contact",
                    source_ref=refs[0],
                    target_ref=ref,
                    strength=0.86,
                    reasons=["KERN grouped these interactions under the same resolved contact."],
                    metadata={"packet_id": packet.id, "person_ref": packet.person_ref},
                )
            )

    def _interaction_link_boost(
        self,
        refs: list[str],
        *,
        packet_types: set[str] | None = None,
    ) -> float:
        if not refs:
            return 0.0
        ref_set = set(refs)
        boost = 0.0
        for outcome in self.memory.list_interaction_outcomes(
            workspace_slug=self.profile.slug,
            limit=40,
        ):
            if packet_types and outcome.packet_type not in packet_types:
                continue
            linked_refs = set(str(item) for item in (outcome.metadata.get("linked_entity_refs") or []))
            if not linked_refs.intersection(ref_set):
                continue
            if outcome.outcome_type in {"same_thread_packet_accepted", "same_contact_packet_accepted", "packet_used"}:
                boost += 0.05
            elif outcome.outcome_type == "llm_rewrite_used":
                boost += 0.03
        return min(0.15, boost)

    def _thread_answer(
        self,
        selected_candidate: dict[str, Any] | None,
        *,
        related_rec_titles: list[str],
        matched_contacts: list[dict[str, Any]],
    ) -> str:
        if not selected_candidate:
            return "KERN needs a clearer thread or email chain before it can answer this confidently."
        parts = [f"The strongest local thread match is '{selected_candidate['title']}'."]
        if matched_contacts:
            parts.append(f"It overlaps with contact history for {matched_contacts[0]['label']}.")
        if related_rec_titles:
            parts.append("Related prepared work: " + "; ".join(related_rec_titles[:2]) + ".")
        return " ".join(parts)

    def _person_answer(self, selected_contact: dict[str, Any] | None, *, related_rec_titles: list[str]) -> str:
        if not selected_contact:
            return "KERN needs a clearer contact or customer target before it can answer this confidently."
        parts = [f"The strongest local contact match is {selected_contact['label']}."]
        if selected_contact["interactions"]:
            parts.append(f"KERN found {len(selected_contact['interactions'][:4])} recent local interactions tied to that contact.")
        if related_rec_titles:
            parts.append("Related work right now: " + "; ".join(related_rec_titles[:2]) + ".")
        return " ".join(parts)

    def _flatten_refs(self, refs: list[Any]) -> list[str]:
        flattened: list[str] = []
        for item in refs:
            if isinstance(item, str):
                if item:
                    flattened.append(item)
                continue
            if isinstance(item, (list, tuple, set)):
                flattened.extend(self._flatten_refs(list(item)))
        return list(dict.fromkeys(flattened))

    def _stable_id(self, prefix: str, *parts: object) -> str:
        digest = hashlib.sha1("::".join(str(part) for part in parts if part is not None).encode("utf-8")).hexdigest()[:12]
        return f"{prefix}-{digest}"
