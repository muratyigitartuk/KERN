from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.intelligence import IntelligenceService
from app.memory import MemoryRepository
from app.retrieval import RetrievalHit, RetrievalService
from app.types import (
    AnswerReadinessStatus,
    ClaimEvidenceRef,
    ClaimRecord,
    DocumentAnswerPacket,
    DocumentCitationRecord,
    DocumentQueryPlan,
    EvidenceBundle,
    GenerationContract,
    MissingInputRecord,
    NegativeEvidenceRecord,
    ProfileSummary,
    RankingExplanation,
    RankingFeatureVector,
    ReadinessEdge,
    ReadinessNode,
    TaskIntentRecord,
)


class DocumentIntelligenceService:
    TOKEN_PATTERN = re.compile(r"[0-9A-Za-zÄÖÜäöüß_-]+")
    DOCUMENT_MARKERS = (
        "pdf",
        "document",
        "documents",
        "file",
        "files",
        "uploaded",
        "attachment",
        "attachments",
        "dokument",
        "dokumente",
        "datei",
        "dateien",
        "anhang",
        "anhänge",
    )
    CITATION_MARKERS = ("cite", "citation", "citations", "source", "sources", "quote", "quotes", "zitiere", "quelle", "quellen")
    SUMMARY_MARKERS = ("summarize", "summary", "summarise", "zusammenfassen", "zusammenfassung", "main points")
    KEY_SECTION_MARKERS = (
        "important sections",
        "key sections",
        "important parts",
        "key parts",
        "risky parts",
        "risky sections",
        "main sections",
        "wichtige abschnitte",
        "wichtige teile",
        "riskante teile",
        "relevante abschnitte",
    )
    COMPARE_MARKERS = ("compare", "difference", "differences", "versus", "vs", "contrast", "vergleiche", "unterschied")
    AMBIGUOUS_REFERENCE_MARKERS = (
        "this pdf",
        "this file",
        "this document",
        "that pdf",
        "that file",
        "that document",
        "diese pdf",
        "diese datei",
        "dieses dokument",
        "diesen anhang",
    )
    RECENT_DOCUMENT_REFERENCE_MARKERS = (
        "attached",
        "attachment",
        "attached file",
        "attached document",
        "uploaded",
        "recent",
        "angehängt",
        "hochgeladen",
    )
    STOPWORDS = {
        "the", "this", "that", "from", "into", "with", "your", "their", "about", "what", "show", "tell",
        "me", "and", "for", "der", "die", "das", "den", "dem", "ein", "eine", "einer", "einem", "einen",
        "und", "aus", "von", "mit", "bitte", "mir", "diese", "dieses", "diesem", "dokument", "dokumente",
        "datei", "pdf",
    }
    RISK_TERMS = (
        "risk", "urgent", "deadline", "must", "required", "penalty", "warning", "overdue", "due", "riskant",
        "frist", "fällig", "faellig", "muss", "erforderlich", "strafe", "warnung", "überfällig",
    )

    def __init__(
        self,
        memory: MemoryRepository,
        profile: ProfileSummary,
        *,
        retrieval: RetrievalService | None = None,
        intelligence: IntelligenceService | None = None,
    ) -> None:
        self.memory = memory
        self.profile = profile
        self.retrieval = retrieval or RetrievalService(memory)
        self.intelligence = intelligence or IntelligenceService(None, memory, profile)  # type: ignore[arg-type]

    def classify_task_intent(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        selected_document_ids: list[str] | None = None,
    ) -> TaskIntentRecord:
        lowered = transcript.strip().lower()
        selected_ids = [str(item) for item in (selected_document_ids or []) if str(item).strip()]
        reasons: list[str] = []
        recent_documents = self.memory.list_document_records(limit=12, include_archived=True)
        title_matches = self._match_documents(lowered, recent_documents)
        explicit_document_marker = any(marker in lowered for marker in self.DOCUMENT_MARKERS)
        deictic_reference = any(marker in lowered for marker in self.RECENT_DOCUMENT_REFERENCE_MARKERS)
        recent_document_bias = bool(recent_documents) and deictic_reference
        has_document_signal = bool(selected_ids) or explicit_document_marker or bool(title_matches) or recent_document_bias
        if any(marker in lowered for marker in self.COMPARE_MARKERS):
            family = "document_compare" if has_document_signal or len(title_matches) >= 2 else "general_chat_fallback"
            reasons.append("The request uses comparison language around local document context.")
        elif any(marker in lowered for marker in self.CITATION_MARKERS):
            family = "document_citation" if has_document_signal else "general_chat_fallback"
            reasons.append("The request explicitly asks for citations or supporting sources.")
        elif any(marker in lowered for marker in self.KEY_SECTION_MARKERS):
            family = "document_key_sections" if has_document_signal else "general_chat_fallback"
            reasons.append("The request asks for important or risky document sections.")
        elif any(marker in lowered for marker in self.SUMMARY_MARKERS):
            family = "document_summary" if has_document_signal else "general_chat_fallback"
            reasons.append("The request asks for a local document summary.")
        elif has_document_signal:
            family = "document_qa"
            reasons.append("The request references a local file or document and asks for grounded help.")
        else:
            family = "general_chat_fallback"
        if family == "general_chat_fallback" and any(marker in lowered for marker in ("what should", "what next", "follow up", "draft", "review", "finalize", "export", "erasure")):
            family = "prepared_work"
            reasons.append("The request matches a worker-facing preparation pattern.")

        clarification_required = False
        clarification_prompt = ""
        resolved_ids = self._resolve_document_ids(
            lowered,
            selected_ids=selected_ids,
            recent_documents=recent_documents,
            title_matches=title_matches,
        )
        if family.startswith("document_"):
            if family == "document_compare" and len(resolved_ids) < 2:
                clarification_required = True
                clarification_prompt = "I need two clear document targets before I can compare them."
                reasons.append("Comparison needs two resolved documents.")
            elif not resolved_ids and any(marker in lowered for marker in self.AMBIGUOUS_REFERENCE_MARKERS):
                clarification_required = True
                clarification_prompt = "I need to know which local document you mean before I cite or summarize it."
                reasons.append("The request uses an ambiguous document reference.")
            elif len(title_matches) > 1 and family != "document_compare":
                clarification_required = True
                clarification_prompt = "I found multiple plausible local documents. Tell me which one you want."
                reasons.append("Multiple local documents match the request.")
        if clarification_required:
            family = "clarification_needed"

        signal_count = len(reasons) + len(resolved_ids)
        confidence = min(0.95, 0.35 + (signal_count * 0.12))
        return TaskIntentRecord(
            id=self._stable_id("task-intent", transcript, workspace_slug or self.profile.slug, ",".join(resolved_ids)),
            transcript=transcript,
            task_family=family,
            confidence=round(confidence, 4),
            reasons=reasons or ["No strong deterministic document signal was found."],
            selected_document_ids=resolved_ids,
            clarification_required=clarification_required,
            clarification_prompt=clarification_prompt,
        )

    def build_document_answer_packet(
        self,
        transcript: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
        selected_document_ids: list[str] | None = None,
    ) -> DocumentAnswerPacket | None:
        task_intent = self.classify_task_intent(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
            selected_document_ids=selected_document_ids,
        )
        if task_intent.task_family not in {
            "document_qa",
            "document_citation",
            "document_summary",
            "document_key_sections",
            "document_compare",
            "clarification_needed",
        }:
            return None
        query_plan = self._query_plan_for_intent(task_intent)
        selected_documents = self.memory.get_document_details(task_intent.selected_document_ids)
        retrieval_context = self._retrieve_evidence(
            transcript,
            query_plan=query_plan,
            selected_documents=selected_documents,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            actor_user_id=actor_user_id,
        )
        evidence_bundle = self._build_evidence_bundle(
            transcript,
            task_intent=task_intent,
            query_plan=query_plan,
            retrieval_context=retrieval_context,
            selected_documents=selected_documents,
        )
        readiness_nodes, readiness_edges, missing_inputs = self._build_readiness_graph(
            task_intent=task_intent,
            query_plan=query_plan,
            selected_documents=selected_documents,
            evidence_bundle=evidence_bundle,
            retrieval_context=retrieval_context,
        )
        readiness_status = self._readiness_status_from_graph(
            readiness_nodes=readiness_nodes,
            clarification_required=query_plan.clarification_required,
        )
        why_ready = self._why_ready(
            task_intent=task_intent,
            selected_documents=selected_documents,
            evidence_bundle=evidence_bundle,
            retrieval_context=retrieval_context,
            readiness_status=readiness_status,
        )
        why_blocked = self._why_blocked(readiness_nodes, missing_inputs, retrieval_context)
        generation_contract = self._generation_contract_for_packet(
            task_intent=task_intent,
            query_plan=query_plan,
            readiness_status=readiness_status,
        )
        packet = DocumentAnswerPacket(
            id=self._stable_id("document-packet", transcript, workspace_slug or self.profile.slug, ",".join(task_intent.selected_document_ids)),
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug or self.profile.slug,
            actor_user_id=actor_user_id,
            task_intent=task_intent,
            query_plan=query_plan,
            query_text=transcript,
            title=self._packet_title(task_intent, selected_documents),
            answer_intent=self._answer_intent_label(query_plan.required_output_mode),
            selected_document_ids=list(task_intent.selected_document_ids),
            selected_documents=list(selected_documents.values()),
            readiness_status=readiness_status,
            why_ready=why_ready,
            why_blocked=why_blocked,
            blocker_details=list(dict.fromkeys(why_blocked)),
            missing_inputs=missing_inputs,
            readiness_nodes=readiness_nodes,
            readiness_edges=readiness_edges,
            evidence_pack=evidence_bundle,
            citations=retrieval_context["citations"],
            generation_contract=generation_contract,
            worker_review_required=readiness_status != "ready_now",
            event_refs=[ref for ref in evidence_bundle.event_refs if ref],
            deterministic_answer=self._deterministic_answer(
                transcript,
                task_intent=task_intent,
                query_plan=query_plan,
                retrieval_context=retrieval_context,
                evidence_bundle=evidence_bundle,
                selected_documents=selected_documents,
                readiness_status=readiness_status,
            ),
        )
        self.memory.store_document_answer_packet(packet)
        return packet

    def get_document_answer_packet(self, packet_id: str) -> DocumentAnswerPacket | None:
        return self.memory.get_document_answer_packet(packet_id)

    def _query_plan_for_intent(self, task_intent: TaskIntentRecord) -> DocumentQueryPlan:
        mode_map = {
            "document_citation": "citations",
            "document_summary": "summary",
            "document_key_sections": "key_sections",
            "document_compare": "compare",
            "clarification_needed": "answer",
        }
        output_mode = mode_map.get(task_intent.task_family, "answer")
        evidence_requirements = ["resolved_document_target", "supporting_chunks"]
        if output_mode == "citations":
            evidence_requirements.append("chunk_level_citations")
        elif output_mode == "summary":
            evidence_requirements.append("broad_coverage")
        elif output_mode == "key_sections":
            evidence_requirements.extend(["section_ranking", "important_passages"])
        elif output_mode == "compare":
            evidence_requirements.extend(["comparison_pair", "support_from_each_side"])
        notes = ["Deterministic document reasoning route selected before chat fallback."]
        if task_intent.clarification_required:
            notes.append("A clarification prompt is required before confident grounding.")
        return DocumentQueryPlan(
            id=self._stable_id("document-plan", task_intent.id, output_mode),
            task_type=task_intent.task_family,
            selected_document_ids=list(task_intent.selected_document_ids),
            query_terms=self._query_terms(task_intent.transcript),
            required_output_mode=output_mode,  # type: ignore[arg-type]
            evidence_requirements=evidence_requirements,
            citation_required=output_mode == "citations",
            clarification_required=task_intent.clarification_required,
            notes=notes,
        )

    def _match_documents(self, lowered_transcript: str, recent_documents: list) -> list[str]:
        matches: list[str] = []
        for record in recent_documents:
            title = str(record.title or "").lower()
            stem = re.sub(r"[^0-9a-zäöüß]+", " ", title).strip()
            file_name = re.sub(r"[^0-9a-zäöüß]+", " ", str(record.file_path or "").split("\\")[-1].lower()).strip()
            if title and title in lowered_transcript:
                matches.append(record.id)
                continue
            title_tokens = [token for token in stem.split() if len(token) >= 4]
            if title_tokens and sum(1 for token in title_tokens if token in lowered_transcript) >= max(1, min(2, len(title_tokens))):
                matches.append(record.id)
                continue
            file_tokens = [token for token in file_name.split() if len(token) >= 4]
            if file_tokens and sum(1 for token in file_tokens if token in lowered_transcript) >= max(1, min(2, len(file_tokens))):
                matches.append(record.id)
        return list(dict.fromkeys(matches))

    def _resolve_document_ids(
        self,
        lowered_transcript: str,
        *,
        selected_ids: list[str],
        recent_documents: list,
        title_matches: list[str],
    ) -> list[str]:
        if selected_ids:
            return list(dict.fromkeys(selected_ids))
        if title_matches:
            if any(marker in lowered_transcript for marker in self.COMPARE_MARKERS):
                return title_matches[:2]
            if len(title_matches) == 1:
                return title_matches[:1]
            return []
        if any(marker in lowered_transcript for marker in self.AMBIGUOUS_REFERENCE_MARKERS):
            if len(recent_documents) == 1:
                return [recent_documents[0].id]
            if len(recent_documents) >= 2 and any(marker in lowered_transcript for marker in self.COMPARE_MARKERS):
                return [recent_documents[0].id, recent_documents[1].id]
        return []

    def _query_terms(self, transcript: str) -> list[str]:
        return [
            match.group(0).lower()
            for match in self.TOKEN_PATTERN.finditer(transcript)
            if len(match.group(0)) >= 3 and match.group(0).lower() not in self.STOPWORDS
        ][:12]

    def _retrieve_evidence(
        self,
        transcript: str,
        *,
        query_plan: DocumentQueryPlan,
        selected_documents: dict[str, dict[str, object]],
        organization_id: str | None,
        workspace_slug: str | None,
        actor_user_id: str | None,
    ) -> dict[str, Any]:
        selected_ids = list(selected_documents)
        lexical_hits = self.retrieval.retrieve_from_documents(transcript, selected_ids, limit=10) if selected_ids else []
        broader_hits = [
            hit
            for hit in self.retrieval.retrieve(transcript, scope="profile_plus_archive", limit=12)
            if hit.source_type in {"document", "archive"}
        ]
        memory_hits = self.intelligence.retrieve_memory_context(
            transcript,
            organization_id=organization_id,
            workspace_slug=workspace_slug or self.profile.slug,
            user_id=actor_user_id,
            limit=4,
        )
        fused_hits = self._fuse_hits(
            transcript,
            lexical_hits=lexical_hits,
            broader_hits=broader_hits,
            selected_document_ids=selected_ids,
        )
        hit_stats = self._hit_statistics(
            fused_hits,
            selected_document_ids=selected_ids,
            support_terms=self._support_terms(query_plan, selected_documents),
        )
        section_candidates = self._section_candidates(fused_hits)
        citations = self._citations_for_hits(fused_hits, limit=6)
        memory_support = [
            item
            for item in memory_hits
            if any(term in f"{item.get('key','')} {item.get('value','')}".lower() for term in query_plan.query_terms[:4])
        ][:3]
        negative_evidence = self._negative_evidence(
            query_plan=query_plan,
            selected_document_ids=selected_ids,
            fused_hits=fused_hits,
            citations=citations,
            section_candidates=section_candidates,
            hit_stats=hit_stats,
        )
        conflicts = self._detect_conflicts(fused_hits, query_plan.required_output_mode)
        ordered_items: list[dict[str, Any]] = []
        for hit in fused_hits[:8]:
            ordered_items.append(
                {
                    "ref_id": str(hit.metadata.get("chunk_id") or f"{hit.source_id}:{hit.metadata.get('chunk_index', 0)}"),
                    "title": str(hit.metadata.get("title") or hit.source_id),
                    "status": "retrieved_evidence",
                    "reason": str(hit.text[:220]),
                    "priority": 4 if str(hit.source_id) in selected_ids else 3,
                    "metadata": {"stage": "document_retrieval", "score": hit.score, **hit.metadata},
                }
            )
        for item in memory_support:
            ordered_items.append(
                {
                    "ref_id": str(item.get("id") or item.get("key") or ""),
                    "title": str(item.get("key") or item.get("title") or "memory"),
                    "status": "memory_support",
                    "reason": str(item.get("value") or ""),
                    "priority": 2,
                    "metadata": {"stage": "memory_support", **(item.get("provenance") or {})},
                }
            )
        return {
            "lexical_hits": lexical_hits,
            "broader_hits": broader_hits,
            "fused_hits": fused_hits,
            "memory_support": memory_support,
            "section_candidates": section_candidates,
            "citations": citations,
            "negative_evidence": negative_evidence,
            "conflicts": conflicts,
            "ordered_items": ordered_items,
            "hit_stats": hit_stats,
        }

    def _fuse_hits(
        self,
        transcript: str,
        *,
        lexical_hits: list[RetrievalHit],
        broader_hits: list[RetrievalHit],
        selected_document_ids: list[str],
    ) -> list[RetrievalHit]:
        query_terms = self._query_terms(transcript)
        buckets: dict[str, RetrievalHit] = {}
        for index, hit in enumerate([*lexical_hits, *broader_hits]):
            ref_id = str(hit.metadata.get("chunk_id") or f"{hit.source_id}:{hit.metadata.get('chunk_index', 0)}")
            overlap = sum(1 for term in query_terms if term in str(hit.text).lower())
            stage_bonus = 0.55 if str(hit.source_id) in selected_document_ids else 0.2
            fused_score = float(hit.score) + stage_bonus + min(0.4, overlap * 0.08) - (index * 0.01)
            if ref_id not in buckets or fused_score > buckets[ref_id].score:
                buckets[ref_id] = hit.model_copy(update={"score": round(fused_score, 4)})
        hits = sorted(
            buckets.values(),
            key=lambda item: (
                str(item.source_id) not in selected_document_ids,
                -item.score,
                int(item.metadata.get("chunk_index") or 0),
            ),
        )
        return hits[:10]

    def _support_terms(self, query_plan: DocumentQueryPlan, selected_documents: dict[str, dict[str, object]]) -> list[str]:
        support_terms = list(query_plan.query_terms)
        if query_plan.required_output_mode != "compare":
            return support_terms
        title_terms = {
            token
            for details in selected_documents.values()
            for token in self._query_terms(str(details.get("title") or ""))
        }
        compare_noise = {"compare", "difference", "differences", "versus", "contrast", "vs", "vergleiche", "unterschied"}
        filtered = [term for term in support_terms if term not in title_terms and term not in compare_noise]
        return filtered or support_terms

    def _hit_statistics(
        self,
        hits: list[RetrievalHit],
        *,
        selected_document_ids: list[str],
        support_terms: list[str],
    ) -> dict[str, object]:
        docs_with_hits: dict[str, int] = defaultdict(int)
        content_supported_docs: set[str] = set()
        unique_chunks: set[str] = set()
        unique_sections: set[tuple[str, str]] = set()
        for hit in hits:
            source_id = str(hit.source_id)
            docs_with_hits[source_id] += 1
            if any(term in str(hit.text).lower() for term in support_terms):
                content_supported_docs.add(source_id)
            unique_chunks.add(str(hit.metadata.get("chunk_id") or f"{source_id}:{hit.metadata.get('chunk_index', 0)}"))
            heading = str(hit.metadata.get("heading") or hit.metadata.get("title") or "").strip().lower()
            if heading:
                unique_sections.add((source_id, heading))
            elif int(hit.metadata.get("chunk_index") or 0) >= 0:
                unique_sections.add((source_id, f"chunk:{int(hit.metadata.get('chunk_index') or 0)}"))
        selected_hits = {doc_id: docs_with_hits.get(doc_id, 0) for doc_id in selected_document_ids}
        supported_selected = [doc_id for doc_id, count in selected_hits.items() if count > 0]
        return {
            "docs_with_hits": docs_with_hits,
            "selected_hits": selected_hits,
            "supported_selected": supported_selected,
            "content_supported_selected": [doc_id for doc_id in selected_document_ids if doc_id in content_supported_docs],
            "unique_chunk_count": len(unique_chunks),
            "unique_section_count": len(unique_sections),
        }

    def _section_candidates(self, hits: list[RetrievalHit]) -> list[dict[str, object]]:
        candidates: list[dict[str, object]] = []
        for hit in hits[:8]:
            lines = [line.strip() for line in str(hit.text).splitlines() if line.strip()]
            heading = ""
            for line in lines[:3]:
                if line.startswith("#"):
                    heading = line.lstrip("#").strip()
                    break
                if len(line) <= 80 and len(line.split()) <= 10:
                    heading = line
                    break
            text = " ".join(lines[:5])[:260]
            risk_score = sum(1 for term in self.RISK_TERMS if term in str(hit.text).lower())
            candidates.append(
                {
                    "ref_id": str(hit.metadata.get("chunk_id") or f"{hit.source_id}:{hit.metadata.get('chunk_index', 0)}"),
                    "heading": heading or str(hit.metadata.get("title") or "Section"),
                    "excerpt": text,
                    "score": round(float(hit.score) + min(0.4, risk_score * 0.1), 4),
                    "document_id": str(hit.source_id),
                    "chunk_index": int(hit.metadata.get("chunk_index") or 0),
                }
            )
        candidates.sort(key=lambda item: (-float(item["score"]), int(item["chunk_index"])))
        return candidates[:5]

    def _citations_for_hits(self, hits: list[RetrievalHit], *, limit: int) -> list[DocumentCitationRecord]:
        citations: list[DocumentCitationRecord] = []
        for hit in hits[:limit]:
            chunk_id = str(hit.metadata.get("chunk_id") or f"{hit.source_id}:{hit.metadata.get('chunk_index', 0)}")
            citations.append(
                DocumentCitationRecord(
                    id=self._stable_id("citation", chunk_id),
                    document_id=str(hit.source_id),
                    title=str(hit.metadata.get("title") or hit.source_id),
                    chunk_id=chunk_id,
                    excerpt=str(hit.text[:240]).strip(),
                    page_number=hit.metadata.get("page_number"),
                    chunk_index=int(hit.metadata.get("chunk_index") or 0),
                )
            )
        return citations

    def _negative_evidence(
        self,
        *,
        query_plan: DocumentQueryPlan,
        selected_document_ids: list[str],
        fused_hits: list[RetrievalHit],
        citations: list[DocumentCitationRecord],
        section_candidates: list[dict[str, object]],
        hit_stats: dict[str, object],
    ) -> list[NegativeEvidenceRecord]:
        items: list[NegativeEvidenceRecord] = []
        if not selected_document_ids:
            items.append(
                NegativeEvidenceRecord(
                    id=self._stable_id("neg", query_plan.id, "target"),
                    expected_signal="resolved target document",
                    searched_sources=["recent_documents", "document_titles"],
                    detail="No local document target was resolved from the request.",
                )
            )
        if query_plan.required_output_mode == "citations" and not citations:
            items.append(
                NegativeEvidenceRecord(
                    id=self._stable_id("neg", query_plan.id, "citations"),
                    expected_signal="citation-backed chunks",
                    searched_sources=["selected_document_chunks", "profile_archive_retrieval"],
                    detail="The request asked for citations, but no chunk-level support was found.",
                )
            )
        if query_plan.required_output_mode == "summary" and not self._has_summary_breadth(
            selected_document_ids=selected_document_ids,
            hit_stats=hit_stats,
        ):
            items.append(
                NegativeEvidenceRecord(
                    id=self._stable_id("neg", query_plan.id, "coverage"),
                    expected_signal="broad summary coverage",
                    searched_sources=["selected_document_chunks", "profile_archive_retrieval"],
                    detail="There are not enough grounded hits yet to claim broad document coverage.",
                )
            )
        if query_plan.required_output_mode == "key_sections" and len(section_candidates) < 2:
            items.append(
                NegativeEvidenceRecord(
                    id=self._stable_id("neg", query_plan.id, "sections"),
                    expected_signal="important section candidates",
                    searched_sources=["selected_document_chunks"],
                    detail="No strong section candidates were found for this request.",
                )
            )
        if query_plan.required_output_mode == "compare" and len(selected_document_ids) < 2:
            items.append(
                NegativeEvidenceRecord(
                    id=self._stable_id("neg", query_plan.id, "compare-pair"),
                    expected_signal="comparison pair",
                    searched_sources=["selected_document_ids", "document_titles"],
                    detail="A comparison needs two resolved documents.",
                )
            )
        if query_plan.required_output_mode == "compare" and len(selected_document_ids) >= 2:
            supported_selected = set(hit_stats.get("content_supported_selected", []))
            for doc_id in selected_document_ids[:2]:
                if doc_id not in supported_selected:
                    items.append(
                        NegativeEvidenceRecord(
                            id=self._stable_id("neg", query_plan.id, "compare-support", doc_id),
                            expected_signal=f"grounded comparison support for {doc_id}",
                            searched_sources=["selected_document_chunks", "profile_archive_retrieval"],
                            detail="One side of the comparison does not yet have enough grounded support.",
                        )
                    )
        return items

    def _detect_conflicts(self, hits: list[RetrievalHit], mode: str) -> list[str]:
        if mode == "compare":
            return []
        dates = set()
        amounts = set()
        for hit in hits[:6]:
            text = str(hit.text)
            dates.update(re.findall(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{2,4})\b", text))
            amounts.update(re.findall(r"\b\d[\d.,]*\s*(?:EUR|USD|GBP|€|\$|£)\b", text))
        conflicts: list[str] = []
        if len(dates) > 1:
            conflicts.append("The retrieved passages reference different dates that may need human review.")
        if len(amounts) > 1:
            conflicts.append("The retrieved passages reference different amounts that may need human review.")
        return conflicts

    def _build_evidence_bundle(
        self,
        transcript: str,
        *,
        task_intent: TaskIntentRecord,
        query_plan: DocumentQueryPlan,
        retrieval_context: dict[str, Any],
        selected_documents: dict[str, dict[str, object]],
    ) -> EvidenceBundle:
        citations: list[DocumentCitationRecord] = retrieval_context["citations"]
        fused_hits: list[RetrievalHit] = retrieval_context["fused_hits"]
        conflicts: list[str] = retrieval_context["conflicts"]
        hit_stats: dict[str, object] = retrieval_context["hit_stats"]
        selected_count = len(selected_documents)
        sufficient_hits = self._coverage_ready(
            query_plan=query_plan,
            selected_document_ids=list(selected_documents),
            citations=citations,
            section_candidates=retrieval_context["section_candidates"],
            hit_stats=hit_stats,
        )
        claims: list[ClaimRecord] = [
            ClaimRecord(
                id=self._stable_id("claim", query_plan.id, "target"),
                label="Target document is resolved",
                status="supported" if selected_count and not task_intent.clarification_required else "missing",
                evidence_refs=[
                    ClaimEvidenceRef(ref_id=str(doc_id), source_type="document_record", title=str(details.get("title") or doc_id))
                    for doc_id, details in list(selected_documents.items())[:2]
                ],
                rationale="Document answers must point at a real local target before claims are trusted.",
                derived_from=["selected_documents", "title_match", "recent_documents"],
            ),
            ClaimRecord(
                id=self._stable_id("claim", query_plan.id, "coverage"),
                label="Enough grounded evidence exists for this answer",
                status="supported" if sufficient_hits else "missing",
                evidence_refs=[
                    ClaimEvidenceRef(ref_id=item.chunk_id, source_type="document_chunk", title=item.title, excerpt=item.excerpt[:140])
                    for item in citations[:3]
                ],
                rationale="Document answers should not claim readiness without grounded chunk support.",
                derived_from=["document_retrieval"],
            ),
        ]
        if query_plan.required_output_mode == "summary":
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", query_plan.id, "summary-breadth"),
                    label="Summary coverage spans enough grounded document breadth",
                    status="supported" if self._has_summary_breadth(selected_document_ids=list(selected_documents), hit_stats=hit_stats) else "missing",
                    evidence_refs=[
                        ClaimEvidenceRef(ref_id=item.chunk_id, source_type="document_chunk", title=item.title, excerpt=item.excerpt[:140])
                        for item in citations[:4]
                    ],
                    rationale="A summary should cover more than one narrow passage before KERN treats it as ready.",
                    derived_from=["document_retrieval", "section_ranking"],
                )
            )
        if query_plan.required_output_mode == "key_sections":
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", query_plan.id, "section-ranking"),
                    label="Important sections were ranked from grounded chunks",
                    status="supported" if len(retrieval_context["section_candidates"]) >= 2 else "missing",
                    evidence_refs=[
                        ClaimEvidenceRef(
                            ref_id=str(item["ref_id"]),
                            source_type="document_chunk",
                            title=str(item["heading"]),
                            excerpt=str(item["excerpt"])[:140],
                        )
                        for item in retrieval_context["section_candidates"][:4]
                    ],
                    rationale="Important-section requests should surface multiple grounded section candidates.",
                    derived_from=["section_ranking"],
                )
            )
        if query_plan.citation_required:
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", query_plan.id, "citations"),
                    label="Citation coverage is sufficient",
                    status="supported" if citations else "missing",
                    evidence_refs=[
                        ClaimEvidenceRef(ref_id=item.chunk_id, source_type="document_chunk", title=item.title, excerpt=item.excerpt[:140])
                        for item in citations[:3]
                    ],
                    rationale="Citation requests need direct chunk references.",
                    derived_from=["document_retrieval"],
                )
            )
        if query_plan.required_output_mode == "compare":
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", query_plan.id, "compare-pair"),
                    label="Both comparison sides are resolved",
                    status="supported" if len(selected_documents) >= 2 else "missing",
                    evidence_refs=[
                        ClaimEvidenceRef(ref_id=str(doc_id), source_type="document_record", title=str(details.get("title") or doc_id))
                        for doc_id, details in list(selected_documents.items())[:2]
                    ],
                    rationale="Comparison packets need two grounded document targets.",
                    derived_from=["selected_documents"],
                )
            )
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", query_plan.id, "compare-support"),
                    label="Both comparison sides have grounded support",
                    status="supported" if len(hit_stats.get("content_supported_selected", [])) >= 2 else "missing",
                    evidence_refs=[
                        ClaimEvidenceRef(ref_id=item.chunk_id, source_type="document_chunk", title=item.title, excerpt=item.excerpt[:140])
                        for item in citations[:4]
                    ],
                    rationale="Each side of a comparison needs its own grounded evidence, not just one side with many hits.",
                    derived_from=["document_retrieval"],
                )
            )
        if conflicts:
            claims.append(
                ClaimRecord(
                    id=self._stable_id("claim", query_plan.id, "conflicts"),
                    label="Conflicting passages are resolved",
                    status="conflicted",
                    rationale="Conflicting grounded passages must be surfaced instead of flattened into one answer.",
                    derived_from=["document_retrieval"],
                )
            )
        supported_required = sum(1 for claim in claims if claim.required and claim.status == "supported")
        required_total = max(1, sum(1 for claim in claims if claim.required))
        breadth_bonus = min(
            0.3,
            len(set(hit_stats.get("supported_selected", []))) * 0.08
            + int(hit_stats.get("unique_chunk_count", 0)) * 0.025
            + int(hit_stats.get("unique_section_count", 0)) * 0.015,
        )
        coverage_score = min(1.0, round((supported_required / required_total) * 0.72 + breadth_bonus, 4))
        reasons = list(task_intent.reasons)
        if selected_documents:
            reasons.append("Recent or explicitly matched local documents were prioritized before broader search.")
        if citations:
            reasons.append("Chunk-level evidence was attached so the answer can stay grounded.")
        if retrieval_context["negative_evidence"]:
            reasons.append("Missing evidence stayed visible instead of being silently ignored.")
        features = RankingFeatureVector(
            workflow_match=0.0,
            urgency_score=0.0,
            approval_score=0.0,
            recency_score=min(1.0, len(citations) * 0.2),
            entity_match_score=min(1.0, len(selected_documents) * 0.4),
            document_class_score=min(
                1.0,
                sum(0.3 for details in selected_documents.values() if str(details.get("category") or "").lower() in transcript.lower()),
            ),
        )
        return EvidenceBundle(
            id=self._stable_id("evidence", query_plan.id),
            summary=f"Document evidence pack for {query_plan.required_output_mode}.",
            why_selected=list(dict.fromkeys(reasons))[:6],
            scope="workspace",
            confidence=min(0.98, round(0.35 + coverage_score * 0.55, 4)),
            policy_safe=True,
            source_refs=[item.chunk_id for item in citations] or list(task_intent.selected_document_ids),
            action_relevance=min(1.0, 0.4 + coverage_score * 0.5),
            items=list(retrieval_context["ordered_items"]),
            claims=claims,
            negative_evidence=list(retrieval_context["negative_evidence"]),
            coverage_score=coverage_score,
            freshness=None,
            event_refs=[],
            ranking_explanation=RankingExplanation(
                score=coverage_score,
                reasons=list(dict.fromkeys(reasons))[:5],
                features=features,
            ),
        )

    def _build_readiness_graph(
        self,
        *,
        task_intent: TaskIntentRecord,
        query_plan: DocumentQueryPlan,
        selected_documents: dict[str, dict[str, object]],
        evidence_bundle: EvidenceBundle,
        retrieval_context: dict[str, Any],
    ) -> tuple[list[ReadinessNode], list[ReadinessEdge], list[MissingInputRecord]]:
        nodes: list[ReadinessNode] = []
        edges: list[ReadinessEdge] = []
        missing_inputs: list[MissingInputRecord] = []
        action_node_id = self._stable_id("doc-readiness", query_plan.id, "action")
        nodes.append(
            ReadinessNode(
                id=action_node_id,
                label=f"{query_plan.required_output_mode} answer",
                node_type="action",
                state="candidate",
                reason="Document answer readiness is derived from the target, evidence, and blocker checks below.",
                required=True,
                source_refs=list(evidence_bundle.source_refs),
            )
        )
        target_state = "verified" if selected_documents and not task_intent.clarification_required else "missing"
        if task_intent.clarification_required and len(selected_documents) > 1:
            target_state = "blocked"
        target_id = self._stable_id("doc-readiness", query_plan.id, "target")
        nodes.append(
            ReadinessNode(
                id=target_id,
                label="Target document resolved",
                node_type="document_target",
                state=target_state,  # type: ignore[arg-type]
                reason=task_intent.clarification_prompt or "A clear local document target is required.",
                required=True,
                source_refs=list(selected_documents),
            )
        )
        edges.append(ReadinessEdge(from_node_id=target_id, to_node_id=action_node_id, relationship="supports"))
        if target_state in {"missing", "blocked"}:
            missing_inputs.append(
                MissingInputRecord(
                    id=self._stable_id("doc-missing", query_plan.id, "target"),
                    label="Choose the target document",
                    reason=task_intent.clarification_prompt or "KERN still needs a clear local document target.",
                    required_for=query_plan.required_output_mode,
                    severity="blocking" if target_state == "blocked" else "warning",
                )
            )
        for claim in evidence_bundle.claims:
            state_map = {"supported": "verified", "missing": "missing", "conflicted": "blocked", "inferred": "candidate"}
            node_id = self._stable_id("doc-readiness", query_plan.id, claim.id)
            nodes.append(
                ReadinessNode(
                    id=node_id,
                    label=claim.label,
                    node_type="claim",
                    state=state_map.get(claim.status, "candidate"),  # type: ignore[arg-type]
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
                        id=self._stable_id("doc-missing", query_plan.id, claim.id),
                        label=claim.label,
                        reason="Grounded support for this required claim is still missing.",
                        required_for=query_plan.required_output_mode,
                        severity="warning",
                    )
                )
            elif claim.status == "conflicted":
                missing_inputs.append(
                    MissingInputRecord(
                        id=self._stable_id("doc-missing", query_plan.id, claim.id, "conflict"),
                        label=claim.label,
                        reason="Conflicting grounded passages must be resolved before this answer is trusted.",
                        required_for=query_plan.required_output_mode,
                        severity="blocking",
                    )
                )
        for index, item in enumerate(retrieval_context["negative_evidence"], start=1):
            severity = "blocking" if any(
                marker in item.expected_signal
                for marker in ("citation", "comparison", "target document", "grounded comparison support")
            ) else "warning"
            node_id = self._stable_id("doc-readiness", query_plan.id, "neg", index)
            nodes.append(
                ReadinessNode(
                    id=node_id,
                    label=item.expected_signal,
                    node_type="negative_evidence",
                    state="missing",
                    reason=item.detail,
                    required=True,
                )
            )
            edges.append(ReadinessEdge(from_node_id=node_id, to_node_id=action_node_id, relationship="requires"))
            missing_inputs.append(
                MissingInputRecord(
                    id=self._stable_id("doc-missing", query_plan.id, "neg", index),
                    label=item.expected_signal,
                    reason=item.detail,
                    required_for=query_plan.required_output_mode,
                    severity=severity,
                )
            )
        return nodes, edges, missing_inputs[:8]

    def _has_summary_breadth(self, *, selected_document_ids: list[str], hit_stats: dict[str, object]) -> bool:
        selected_hits = hit_stats.get("selected_hits", {})
        if selected_document_ids:
            return any(int(selected_hits.get(doc_id, 0)) >= 2 for doc_id in selected_document_ids) and int(hit_stats.get("unique_chunk_count", 0)) >= 2
        return int(hit_stats.get("unique_chunk_count", 0)) >= 2 and int(hit_stats.get("unique_section_count", 0)) >= 2

    def _coverage_ready(
        self,
        *,
        query_plan: DocumentQueryPlan,
        selected_document_ids: list[str],
        citations: list[DocumentCitationRecord],
        section_candidates: list[dict[str, object]],
        hit_stats: dict[str, object],
    ) -> bool:
        if query_plan.required_output_mode == "summary":
            return self._has_summary_breadth(selected_document_ids=selected_document_ids, hit_stats=hit_stats)
        if query_plan.required_output_mode == "key_sections":
            return len(section_candidates) >= 2
        if query_plan.required_output_mode == "compare":
            return len(selected_document_ids) >= 2 and len(hit_stats.get("content_supported_selected", [])) >= 2
        if query_plan.required_output_mode == "citations":
            return len(citations) >= 1
        return len(citations) >= 1 or int(hit_stats.get("unique_chunk_count", 0)) >= 1

    def _readiness_status_from_graph(self, *, readiness_nodes: list[ReadinessNode], clarification_required: bool) -> AnswerReadinessStatus:
        if any(item.state == "blocked" and item.required for item in readiness_nodes):
            return "blocked"
        if clarification_required:
            return "waiting_on_input"
        if any(item.state == "missing" and item.required for item in readiness_nodes):
            return "waiting_on_input"
        if any(item.state == "candidate" and item.required for item in readiness_nodes if item.node_type != "action"):
            return "needs_review"
        return "ready_now"

    def _why_ready(
        self,
        *,
        task_intent: TaskIntentRecord,
        selected_documents: dict[str, dict[str, object]],
        evidence_bundle: EvidenceBundle,
        retrieval_context: dict[str, Any],
        readiness_status: str,
    ) -> list[str]:
        if readiness_status != "ready_now":
            return []
        reasons = list(task_intent.reasons)
        if selected_documents:
            reasons.append("A concrete local document target was resolved before answer generation.")
        if evidence_bundle.coverage_score >= 0.6:
            reasons.append("The evidence pack has enough grounded chunk support for the requested output.")
        if retrieval_context["citations"]:
            reasons.append("Citations are attached directly to local document chunks.")
        return list(dict.fromkeys(reason for reason in reasons if reason))[:5]

    def _why_blocked(self, readiness_nodes: list[ReadinessNode], missing_inputs: list[MissingInputRecord], retrieval_context: dict[str, Any]) -> list[str]:
        reasons = [item.reason for item in readiness_nodes if item.state == "blocked" and item.reason]
        reasons.extend(item.reason for item in missing_inputs if item.severity == "blocking")
        reasons.extend(retrieval_context["conflicts"])
        return list(dict.fromkeys(reason for reason in reasons if reason))[:6]

    def _generation_contract_for_packet(
        self,
        *,
        task_intent: TaskIntentRecord,
        query_plan: DocumentQueryPlan,
        readiness_status: str,
    ) -> GenerationContract:
        if readiness_status != "ready_now":
            return GenerationContract(
                mode="clarify" if task_intent.clarification_required else "explain_only",
                allow_answer=False,
                allow_cite=False,
                allow_summarize=False,
                allow_clarify=True,
                allow_explain_only=True,
                note="Document packet is not ready. Only clarification or blocker explanation is allowed.",
            )
        if query_plan.required_output_mode == "citations":
            return GenerationContract(
                mode="cite",
                allow_answer=True,
                allow_cite=True,
                allow_summarize=True,
                allow_clarify=True,
                allow_explain_only=True,
                note="Rewrite citations cleanly, but do not invent any sources or page numbers.",
            )
        if query_plan.required_output_mode in {"summary", "key_sections"}:
            return GenerationContract(
                mode="summarize",
                allow_answer=True,
                allow_cite=False,
                allow_summarize=True,
                allow_clarify=True,
                allow_explain_only=True,
                note="Summaries must stay within the grounded evidence pack.",
            )
        return GenerationContract(
            mode="answer",
            allow_answer=True,
            allow_cite=False,
            allow_summarize=True,
            allow_clarify=True,
            allow_explain_only=True,
            note="Answers must stay within the grounded evidence pack.",
        )

    def _deterministic_answer(
        self,
        transcript: str,
        *,
        task_intent: TaskIntentRecord,
        query_plan: DocumentQueryPlan,
        retrieval_context: dict[str, Any],
        evidence_bundle: EvidenceBundle,
        selected_documents: dict[str, dict[str, object]],
        readiness_status: str,
    ) -> str:
        if readiness_status != "ready_now":
            lines = [task_intent.clarification_prompt or "I need more grounded document context before I can answer this cleanly."]
            if evidence_bundle.negative_evidence:
                lines.append("Still missing: " + "; ".join(item.expected_signal for item in evidence_bundle.negative_evidence[:3]))
            return "\n".join(lines)
        citations: list[DocumentCitationRecord] = retrieval_context["citations"]
        if query_plan.required_output_mode == "citations":
            lines = ["Grounded citations from the selected local document context:"]
            for item in citations[:4]:
                ref = f"{item.title} [chunk {item.chunk_index if item.chunk_index is not None else '?'}]"
                lines.append(f"- {ref}: {item.excerpt}")
            return "\n".join(lines)
        if query_plan.required_output_mode == "key_sections":
            lines = ["Most relevant sections I found in the local document context:"]
            for item in retrieval_context["section_candidates"][:4]:
                lines.append(f"- {item['heading']}: {str(item['excerpt'])[:180]}")
            return "\n".join(lines)
        if query_plan.required_output_mode == "compare":
            grouped: dict[str, list[DocumentCitationRecord]] = defaultdict(list)
            for item in citations:
                grouped[item.title].append(item)
            lines = [f"Comparison prepared for {len(selected_documents)} local documents:"]
            for title, items in list(grouped.items())[:2]:
                lines.append(f"- {title}: {items[0].excerpt}")
            if retrieval_context["conflicts"]:
                lines.append("Differences that need attention: " + "; ".join(retrieval_context["conflicts"][:2]))
            return "\n".join(lines)
        if query_plan.required_output_mode == "summary":
            lines = ["Grounded summary from the local document context:"]
            for item in retrieval_context["section_candidates"][:3]:
                lines.append(f"- {item['excerpt']}")
            if retrieval_context["conflicts"]:
                lines.append("Risk notes: " + "; ".join(retrieval_context["conflicts"][:2]))
            return "\n".join(lines)
        answer_excerpt = citations[0].excerpt if citations else "I found grounded document evidence, but not enough to form a stronger deterministic answer."
        reference = citations[0].title if citations else ", ".join(str(item.get("title") or item) for item in selected_documents.values())
        return f"Based on {reference}, the strongest grounded excerpt is: {answer_excerpt}"

    def _packet_title(self, task_intent: TaskIntentRecord, selected_documents: dict[str, dict[str, object]]) -> str:
        if selected_documents:
            first = next(iter(selected_documents.values()))
            return f"{self._answer_intent_label(self._query_plan_for_intent(task_intent).required_output_mode)} for {first.get('title') or 'document'}"
        return f"{task_intent.task_family.replace('_', ' ')} packet"

    def _answer_intent_label(self, output_mode: str) -> str:
        labels = {
            "answer": "Grounded answer",
            "citations": "Grounded citations",
            "summary": "Document summary",
            "key_sections": "Important sections",
            "compare": "Document comparison",
        }
        return labels.get(output_mode, "Document answer")

    def _stable_id(self, *parts: object) -> str:
        raw = "|".join(str(part or "") for part in parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
