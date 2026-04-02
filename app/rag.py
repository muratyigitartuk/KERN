from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config import settings
from app.types import RAGResult, RAGSource, RetrievalHit

if TYPE_CHECKING:
    from app.llm_client import LlamaServerClient
    from app.reranker import Reranker
    from app.retrieval import RetrievalService

logger = logging.getLogger(__name__)

_FOCUS_TERM_PATTERN = re.compile(r"[0-9A-Za-zÃ„Ã–ÃœÃ¤Ã¶Ã¼ÃŸ_-]{3,}")
_EXPLICIT_DOCUMENT_SCOPE_MARKERS = (
    "uploaded",
    "hochgeladen",
    "from the documents",
    "from the uploaded",
    "from the uploaded documents",
    "aus den dokumenten",
    "aus den hochgeladenen",
    "in den dokumenten",
    "documents:",
    "dokumenten:",
)
_GENERIC_QUERY_TERMS = {
    "aus",
    "den",
    "dem",
    "der",
    "die",
    "das",
    "des",
    "und",
    "ist",
    "geht",
    "gibt",
    "hier",
    "dort",
    "dies",
    "diesem",
    "dieser",
    "dieses",
    "einer",
    "einem",
    "einen",
    "eine",
    "eines",
    "zum",
    "zur",
    "von",
    "vom",
    "mit",
    "fuer",
    "für",
    "ueber",
    "über",
    "nach",
    "vor",
    "bei",
    "ist",
    "the",
    "and",
    "with",
    "from",
    "that",
    "this",
    "what",
    "when",
    "which",
    "does",
    "available",
    "context",
    "contain",
    "information",
    "documents",
    "document",
    "uploaded",
    "offer",
    "invoice",
    "amount",
    "total",
    "target",
    "date",
    "due",
    "short",
    "brief",
    "reply",
    "only",
    "using",
    "user",
    "wie",
    "was",
    "wann",
    "worum",
    "hochgeladenen",
    "hochgeladen",
    "dokumenten",
    "dokumente",
    "dokument",
    "angebot",
    "rechnung",
    "zielbetrag",
    "betrag",
    "summe",
    "faellig",
    "fÃ¤llig",
    "quellen",
    "quelle",
    "eckigen",
    "klammern",
    "antworte",
    "kurz",
    "nenne",
}

# Precompiled pattern for token estimation: matches word-like sequences.
# Accounts for German compound words, contractions, and numbers.
_TOKEN_PATTERN = re.compile(r"[A-Za-zÄÖÜäöüß]{2,}|[0-9]+(?:[.,][0-9]+)*|\S")


class NoRetrievalHitsError(Exception):
    pass


@dataclass
class RAGStreamResult:
    """Collects metadata produced during a streaming RAG answer."""
    sources: list[RAGSource] = field(default_factory=list)
    retrieval_hits_count: int = 0
    reranked_count: int = 0
    context_tokens_used: int = 0


class RAGPipeline:
    """retrieve -> rerank -> budget -> inject -> generate"""

    def __init__(
        self,
        retrieval: RetrievalService,
        reranker: Reranker | None,
        llm_client: LlamaServerClient | None,
    ) -> None:
        self._retrieval = retrieval
        self._reranker = reranker
        self._llm = llm_client

    async def answer_stream(
        self,
        query: str,
        system_prompt: str,
        conversation_history: list[dict[str, str]],
        *,
        top_k: int = 12,
        rerank_top_n: int = 4,
        min_score: float = 0.1,
        context_window: int = 8192,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        stream_result: RAGStreamResult | None = None,
        model_override: str | None = None,
    ) -> AsyncIterator[str]:
        if self._llm is None or not self._llm.available:
            return

        hits = self._focus_hits_for_explicit_document_scope(query, self._retrieve(query, top_k))
        if not hits:
            logger.info("RAG: no retrieval hits for query: %s", query[:120])
            raise NoRetrievalHitsError("No retrieval hits for query.")

        reranked = await self._rerank(query, hits, rerank_top_n, min_score)
        if not reranked:
            restored = self._preserve_single_hit(hits, min_score)
            if restored:
                logger.info("RAG: preserving lone retrieval hit after reranker rejection")
                reranked = restored
            else:
                logger.info("RAG: no hits survived reranking (had %d candidates)", len(hits))
                raise NoRetrievalHitsError("No hits survived reranking.")

        context_message, trimmed_history = self._budget(
            system_prompt, reranked, conversation_history, query,
            context_window, max_tokens,
        )

        if stream_result is not None:
            stream_result.sources = self._build_sources(reranked)
            stream_result.retrieval_hits_count = len(hits)
            stream_result.reranked_count = len(reranked)
            stream_result.context_tokens_used = (
                self._estimate_tokens(context_message) if context_message else 0
            )

        extractive_answer = self._extractive_explicit_document_answer(query, reranked)
        if extractive_answer:
            final_answer = self._append_ocr_notice(extractive_answer, query, reranked)
            for token in final_answer.split():
                yield token + " "
            return

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(trimmed_history)
        if context_message:
            messages.append({"role": "user", "content": context_message})
        messages.append({"role": "user", "content": query})

        async for token in self._llm.chat_stream(
            messages, max_tokens=max_tokens, temperature=temperature, model=model_override,
        ):
            yield token
        notice = self._ocr_notice(query, reranked)
        if notice:
            yield notice

    async def answer(
        self,
        query: str,
        system_prompt: str,
        conversation_history: list[dict[str, str]],
        *,
        top_k: int = 12,
        rerank_top_n: int = 4,
        min_score: float = 0.1,
        context_window: int = 8192,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        model_override: str | None = None,
    ) -> RAGResult:
        if self._llm is None or not self._llm.available:
            return RAGResult(answer="", had_sufficient_context=False)

        hits = self._focus_hits_for_explicit_document_scope(query, self._retrieve(query, top_k))
        if not hits:
            return RAGResult(answer="", had_sufficient_context=False)

        reranked = await self._rerank(query, hits, rerank_top_n, min_score)
        if not reranked:
            restored = self._preserve_single_hit(hits, min_score)
            if restored:
                logger.info("RAG: preserving lone retrieval hit after reranker rejection")
                reranked = restored
            else:
                return RAGResult(answer="", retrieval_hits_count=len(hits), had_sufficient_context=False)

        context_message, trimmed_history = self._budget(
            system_prompt, reranked, conversation_history, query,
            context_window, max_tokens,
        )

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(trimmed_history)
        if context_message:
            messages.append({"role": "user", "content": context_message})
        messages.append({"role": "user", "content": query})

        try:
            result = await self._llm.chat(messages, max_tokens=max_tokens, temperature=temperature, model=model_override)
            choices = result.get("choices", [])
            content = ""
            if choices:
                content = choices[0].get("message", {}).get("content", "").strip()
        except Exception as exc:
            logger.warning("LLM chat failed during RAG answer generation: %s", exc, exc_info=True)
            return RAGResult(answer="", retrieval_hits_count=len(hits), reranked_count=len(reranked), had_sufficient_context=False)

        context_tokens = self._estimate_tokens(context_message) if context_message else 0

        extractive_answer = self._extractive_explicit_document_answer(query, reranked)
        if extractive_answer:
            return RAGResult(
                answer=self._append_ocr_notice(extractive_answer, query, reranked),
                sources=self._build_sources(reranked),
                retrieval_hits_count=len(hits),
                reranked_count=len(reranked),
                context_tokens_used=context_tokens,
                had_sufficient_context=True,
            )

        return RAGResult(
            answer=self._append_ocr_notice(content, query, reranked),
            sources=self._build_sources(reranked),
            retrieval_hits_count=len(hits),
            reranked_count=len(reranked),
            context_tokens_used=context_tokens,
            had_sufficient_context=bool(content),
        )

    async def answer_multi_document(
        self,
        query: str,
        document_ids: list[str],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        model_override: str | None = None,
    ) -> RAGResult:
        """Answer a query by retrieving from specific documents and constructing a comparative prompt."""
        hits = self._retrieval.retrieve_from_documents(query, document_ids, limit=settings.rag_top_k)
        if not hits:
            return RAGResult(answer="No relevant content found in the selected documents.", had_sufficient_context=False)
        sources = self._build_sources(hits[:settings.rag_rerank_top_n])
        if self._llm is None or not self._llm.available:
            return RAGResult(
                answer=self._extractive_multi_document_answer(query, hits[:settings.rag_rerank_top_n]),
                sources=sources,
                retrieval_hits_count=len(hits),
                reranked_count=len(sources),
                had_sufficient_context=bool(sources),
            )
        context_blocks: list[str] = []
        for i, hit in enumerate(hits[:settings.rag_rerank_top_n]):
            label = hit.metadata.get("title") or hit.source_id
            context_blocks.append(f"[Document {i + 1}: {label}]\n{hit.text}")
        context_text = "\n\n---\n\n".join(context_blocks)
        system_prompt = (
            "You are KERN, a local AI assistant. Answer the user's question using ONLY the provided document excerpts. "
            "When comparing documents, label each clearly. Be concise and factual."
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Document excerpts:\n{context_text}"},
            {"role": "user", "content": query},
        ]
        try:
            result = await self._llm.chat(messages, max_tokens=max_tokens, temperature=temperature, model=model_override)
            choices = result.get("choices", [])
            content = choices[0].get("message", {}).get("content", "").strip() if choices else ""
        except Exception as exc:
            logger.warning("LLM chat failed during multi-document RAG: %s", exc, exc_info=True)
            return RAGResult(answer="", retrieval_hits_count=len(hits), had_sufficient_context=False)
        return RAGResult(
            answer=content,
            sources=sources,
            retrieval_hits_count=len(hits),
            reranked_count=len(sources),
            had_sufficient_context=bool(content),
        )

    def _extractive_multi_document_answer(self, query: str, hits: list[RetrievalHit]) -> str:
        grouped: dict[str, list[RetrievalHit]] = {}
        for hit in hits:
            label = str(hit.metadata.get("title") or hit.source_id)
            grouped.setdefault(label, []).append(hit)
        lines = [f"Cross-document summary for: {query}"]
        due_dates: dict[str, str] = {}
        amounts: dict[str, str] = {}
        companies: dict[str, str] = {}
        for label, group_hits in grouped.items():
            excerpt = group_hits[0].text.strip().replace("\n", " ")
            lines.append(f"- {label}: {excerpt[:220]}")
            combined_text = " ".join(hit.text for hit in group_hits[:3])
            due_date = self._extract_first_date(combined_text)
            amount = self._extract_first_amount(combined_text)
            company = self._extract_first_company(combined_text)
            if due_date:
                due_dates[label] = due_date
            if amount:
                amounts[label] = amount
            if company:
                companies[label] = company
        if due_dates:
            lines.append("")
            lines.append("Key due dates:")
            for label, due_date in due_dates.items():
                lines.append(f"- {label}: {due_date}")
        if amounts:
            lines.append("")
            lines.append("Key amounts:")
            for label, amount in amounts.items():
                lines.append(f"- {label}: {amount}")
        if companies:
            lines.append("")
            lines.append("Key entities:")
            for label, company in companies.items():
                lines.append(f"- {label}: {company}")
        if len(grouped) >= 2:
            lines.append("")
            lines.append("Differences observed:")
            if len(set(due_dates.values())) > 1:
                lines.append("- The selected documents reference different due dates.")
            if len(set(amounts.values())) > 1:
                lines.append("- The selected documents reference different amounts.")
            if len(set(companies.values())) > 1:
                lines.append("- The selected documents reference different counterparties.")
            if len(lines) and lines[-1] == "Differences observed:":
                lines.append("- Compare the cited excerpts directly; no strong structured difference was extracted.")
        return "\n".join(lines)

    def _extract_first_date(self, text: str) -> str | None:
        match = re.search(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{2,4})\b", text)
        return match.group(1) if match else None

    def _extract_first_amount(self, text: str) -> str | None:
        match = re.search(r"\b(\d[\d.,]*\s*(?:EUR|USD|GBP|€|\$|£))\b", text)
        return match.group(1) if match else None

    def _extract_first_company(self, text: str) -> str | None:
        match = re.search(r"\b([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)* (?:GmbH|AG|Ltd|Inc|Corp|LLC|UG|SE|KG))\b", text)
        return match.group(1) if match else None

    def _extractive_explicit_document_answer(self, query: str, hits: list[RetrievalHit]) -> str | None:
        lowered = query.lower()
        if not any(marker in lowered for marker in _EXPLICIT_DOCUMENT_SCOPE_MARKERS):
            return None
        document_hits = [hit for hit in hits if hit.source_type in {"document", "archive"}]
        if not document_hits:
            return None

        offer_hit = next((hit for hit in document_hits if self._looks_like_offer(hit)), None)
        invoice_hit = next((hit for hit in document_hits if self._looks_like_invoice(hit)), None)
        lines: list[str] = []

        if any(token in lowered for token in ("angebot", "offer")) and offer_hit is not None:
            project = self._extract_offer_topic(offer_hit.text)
            if project:
                lines.append(f"Das Angebot betrifft {project} [{offer_hit.metadata.get('title', offer_hit.source_id)}].")
        if any(token in lowered for token in ("zielbetrag", "betrag", "amount", "summe")) and offer_hit is not None:
            amount = self._extract_labeled_value(offer_hit.text, ("target amount", "zielbetrag", "amount", "total"))
            if amount:
                lines.append(f"Der Zielbetrag beträgt {amount} [{offer_hit.metadata.get('title', offer_hit.source_id)}].")
        if any(token in lowered for token in ("rechnung", "invoice")) and any(token in lowered for token in ("faellig", "fällig", "due")) and invoice_hit is not None:
            due_date = self._extract_labeled_value(invoice_hit.text, ("due date", "fälligkeitsdatum", "faelligkeitsdatum", "fällig", "faellig"))
            if due_date:
                lines.append(f"Die Rechnung ist am {due_date} fällig [{invoice_hit.metadata.get('title', invoice_hit.source_id)}].")

        return " ".join(lines) if lines else None

    def _append_ocr_notice(self, answer: str, query: str, hits: list[RetrievalHit]) -> str:
        if not answer:
            return answer
        notice = self._ocr_notice(query, hits)
        if not notice:
            return answer
        if answer.endswith((" ", "\n")):
            return f"{answer}{notice.strip()}"
        return f"{answer} {notice.strip()}"

    def _ocr_notice(self, query: str, hits: list[RetrievalHit]) -> str:
        if not self._has_low_confidence_ocr(hits):
            return ""
        lowered = query.lower()
        if any(marker in lowered for marker in ("hochgeladen", "rechnung", "angebot", "dokument", "quelle")):
            return "Hinweis: Diese Antwort stützt sich teilweise auf OCR-ausgelesenen Text mit niedriger Zuverlässigkeit."
        return "Note: This answer relies partly on OCR-extracted text with low confidence."

    def _has_low_confidence_ocr(self, hits: list[RetrievalHit]) -> bool:
        return any(bool(hit.metadata.get("ocr_low_confidence")) for hit in hits)

    def _looks_like_offer(self, hit: RetrievalHit) -> bool:
        haystack = f"{hit.metadata.get('title', '')} {hit.text}".lower()
        return "offer" in haystack or "angebot" in haystack

    def _looks_like_invoice(self, hit: RetrievalHit) -> bool:
        haystack = f"{hit.metadata.get('title', '')} {hit.text}".lower()
        return "invoice" in haystack or "rechnung" in haystack

    def _extract_offer_topic(self, text: str) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            if lowered.startswith("project:"):
                return stripped.split(":", 1)[1].strip().rstrip(".")
            if lowered.startswith("projekt:"):
                return stripped.split(":", 1)[1].strip().rstrip(".")
        title_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not title_line:
            return None
        cleaned = re.sub(r"\b(acme gmbh|offer|angebot|invoice|rechnung)\b", "", title_line, flags=re.IGNORECASE).strip(" -:,.")
        return cleaned or title_line

    def _extract_labeled_value(self, text: str, labels: tuple[str, ...]) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            for label in labels:
                if lowered.startswith(f"{label}:"):
                    return stripped.split(":", 1)[1].strip().rstrip(".")
        return None

    def _retrieve(self, query: str, top_k: int) -> list[RetrievalHit]:
        return self._retrieval.retrieve(query, scope="profile_plus_archive", limit=top_k)

    def _focus_hits_for_explicit_document_scope(self, query: str, hits: list[RetrievalHit]) -> list[RetrievalHit]:
        lowered = query.lower()
        if not any(marker in lowered for marker in _EXPLICIT_DOCUMENT_SCOPE_MARKERS):
            return hits
        focus_terms = self._extract_focus_terms(query)
        document_hits = [hit for hit in hits if hit.source_type in {"document", "archive"}]
        if not focus_terms:
            return document_hits or []
        focused_hits: list[tuple[int, RetrievalHit]] = []
        for hit in document_hits:
            haystack = f"{hit.metadata.get('title', '')} {hit.text}".lower()
            overlap = sum(1 for term in focus_terms if term in haystack)
            if overlap > 0:
                focused_hits.append((overlap, hit))
        if not focused_hits:
            return []
        focused_hits.sort(key=lambda item: (-item[0], -item[1].score))
        return [hit for _, hit in focused_hits]

    def _extract_focus_terms(self, query: str) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()
        for match in _FOCUS_TERM_PATTERN.finditer(query):
            raw_token = match.group(0).lower().strip("_-")
            for token in re.split(r"[_-]+", raw_token):
                normalized = token.strip()
                if len(normalized) < 3 or normalized in _GENERIC_QUERY_TERMS:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                terms.append(normalized)
        return terms

    async def _rerank(
        self,
        query: str,
        hits: list[RetrievalHit],
        top_n: int,
        min_score: float,
    ) -> list[RetrievalHit]:
        if self._reranker is not None:
            try:
                hits = await self._reranker.rerank(query, hits, top_n)
            except Exception:
                logger.warning("Reranker failed, using original retrieval order", exc_info=True)
                hits = hits[:top_n]
        else:
            hits = hits[:top_n]
        return [h for h in hits if h.score >= min_score]

    def _preserve_single_hit(self, hits: list[RetrievalHit], min_score: float) -> list[RetrievalHit]:
        if len(hits) != 1:
            return []
        hit = hits[0]
        if hit.score <= 0:
            return []
        return [hit]

    def _budget(
        self,
        system_prompt: str,
        hits: list[RetrievalHit],
        history: list[dict[str, str]],
        query: str,
        context_window: int,
        max_tokens: int,
    ) -> tuple[str, list[dict[str, str]]]:
        system_tokens = self._estimate_tokens(system_prompt)
        query_tokens = self._estimate_tokens(query)
        reserved = system_tokens + query_tokens + max_tokens + 50

        remaining = context_window - reserved
        if remaining <= 0:
            return "", []

        context_budget = int(remaining * 0.6)
        history_budget = remaining - context_budget

        context_parts: list[str] = []
        context_used = 0
        for hit in hits:
            formatted = self._format_single_source(hit)
            tokens = self._estimate_tokens(formatted)
            if context_used + tokens > context_budget:
                break
            context_parts.append(formatted)
            context_used += tokens

        if context_parts:
            context_block = "\n\n---\n\n".join(context_parts)
            context_message = f"Document context:\n{context_block}"
        else:
            context_message = ""

        trimmed_history = self._trim_history(history, history_budget)

        return context_message, trimmed_history

    def _format_single_source(self, hit: RetrievalHit) -> str:
        title = hit.metadata.get("title", "Unknown")
        return f"[{title}]\n{hit.text}"

    def _build_sources(self, hits: list[RetrievalHit]) -> list[RAGSource]:
        return [
            RAGSource(
                title=h.metadata.get("title", ""),
                text=h.text,
                file_path=h.metadata.get("file_path"),
                chunk_index=h.metadata.get("chunk_index", 0),
                score=h.score,
                source_type=h.source_type,
            )
            for h in hits
        ]

    def _trim_history(self, history: list[dict[str, str]], budget_tokens: int) -> list[dict[str, str]]:
        if not history or budget_tokens <= 0:
            return []
        result: list[dict[str, str]] = []
        used = 0
        for turn in reversed(history):
            tokens = self._estimate_tokens(turn.get("content", ""))
            if used + tokens > budget_tokens:
                break
            result.append(turn)
            used += tokens
        result.reverse()
        return result

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(_TOKEN_PATTERN.findall(text)))
