from __future__ import annotations

import asyncio
import re
from typing import Protocol, TYPE_CHECKING

from app.config import settings
from app.types import RetrievalHit

if TYPE_CHECKING:
    from app.llm_client import LlamaServerClient


class Reranker(Protocol):
    async def rerank(self, query: str, hits: list[RetrievalHit], top_n: int) -> list[RetrievalHit]: ...


class LLMReranker:
    """Uses llama-server to score (query, passage) relevance 0-10."""

    _SCORE_SYSTEM = "Rate the relevance of the passage to the query on a scale of 0 to 10. Output only the number."

    def __init__(self, llm_client: LlamaServerClient) -> None:
        self._llm = llm_client

    async def rerank(self, query: str, hits: list[RetrievalHit], top_n: int) -> list[RetrievalHit]:
        if not hits or not self._llm.available:
            return hits[:top_n]
        semaphore = asyncio.Semaphore(max(1, settings.rag_rerank_max_concurrency))
        tasks = [self._score_pair_bounded(query, hit.text, semaphore) for hit in hits]
        scores = await asyncio.gather(*tasks, return_exceptions=True)
        scored: list[tuple[float, int, RetrievalHit]] = []
        for i, (hit, score) in enumerate(zip(hits, scores)):
            if isinstance(score, BaseException):
                score = hit.score * 0.5
            else:
                fused_score = max((score * 0.7) + (hit.score * 0.3), min(hit.score, 0.2) * 0.75)
                score = min(fused_score, 1.0)
            scored.append((float(score), i, hit))
        scored.sort(key=lambda t: (-t[0], t[1]))
        results: list[RetrievalHit] = []
        for rerank_score, _, hit in scored[:top_n]:
            results.append(hit.model_copy(update={"score": round(rerank_score, 4)}))
        return results

    async def _score_pair_bounded(self, query: str, passage: str, semaphore: asyncio.Semaphore) -> float:
        async with semaphore:
            return await asyncio.wait_for(
                self._score_pair(query, passage),
                timeout=max(0.5, settings.rag_rerank_timeout_seconds),
            )

    async def _score_pair(self, query: str, passage: str) -> float:
        passage_trimmed = passage[:1200]
        messages = [
            {"role": "system", "content": self._SCORE_SYSTEM},
            {"role": "user", "content": f"Query: {query}\n\nPassage: {passage_trimmed}"},
        ]
        result = await self._llm.chat(messages, max_tokens=8, temperature=0.0)
        choices = result.get("choices", [])
        if not choices:
            return 0.0
        content = choices[0].get("message", {}).get("content", "").strip()
        match = re.search(r"\d+", content)
        if not match:
            return 0.0
        return max(0, min(int(match.group()), 10)) / 10.0


class ScoreFusionReranker:
    """Fallback: RRF of retrieval score + lexical overlap + title boost."""

    async def rerank(self, query: str, hits: list[RetrievalHit], top_n: int) -> list[RetrievalHit]:
        if not hits:
            return []
        scored: list[tuple[float, int, RetrievalHit]] = []
        for i, hit in enumerate(hits):
            retrieval_rrf = 1.0 / (60 + i)
            lexical = self._lexical_overlap_score(query, hit.text)
            title = self._title_boost(query, hit.metadata.get("title", ""))
            fused = retrieval_rrf + 0.4 * lexical + 0.3 * title
            scored.append((fused, i, hit))
        scored.sort(key=lambda t: (-t[0], t[1]))
        results: list[RetrievalHit] = []
        for fused_score, _, hit in scored[:top_n]:
            results.append(hit.model_copy(update={"score": round(fused_score, 4)}))
        return results

    def _lexical_overlap_score(self, query: str, text: str) -> float:
        query_terms = set(query.lower().split())
        if not query_terms:
            return 0.0
        text_lower = text.lower()
        overlap = sum(1 for t in query_terms if t in text_lower)
        return min(overlap / max(len(query_terms), 1), 1.0)

    def _title_boost(self, query: str, title: str) -> float:
        query_terms = set(query.lower().split())
        if not query_terms or not title:
            return 0.0
        title_lower = title.lower()
        overlap = sum(1 for t in query_terms if t in title_lower)
        return min(overlap / max(len(query_terms), 1), 1.0)
