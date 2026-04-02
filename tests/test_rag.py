from __future__ import annotations

import asyncio

import pytest

from app.rag import NoRetrievalHitsError, RAGPipeline, RAGStreamResult
from app.reranker import LLMReranker, ScoreFusionReranker
from app.types import RAGSource, RAGResult, RetrievalHit


def _make_hit(title: str, text: str, score: float = 0.5, source_type: str = "document") -> RetrievalHit:
    return RetrievalHit(
        source_type=source_type,
        source_id=f"doc:{title}",
        score=score,
        text=text,
        metadata={"title": title, "chunk_index": 0, "file_path": f"/docs/{title}.pdf"},
    )


class FakeRetrievalService:
    def __init__(self, hits: list[RetrievalHit] | None = None):
        self._hits = hits or []
        self.last_scope = None

    def retrieve(self, query: str, scope: str = "profile", limit: int = 8) -> list[RetrievalHit]:
        self.last_scope = scope
        return self._hits[:limit]

    def retrieve_from_documents(self, query: str, document_ids: list[str], limit: int = 8) -> list[RetrievalHit]:
        allowed = set(document_ids)
        return [hit for hit in self._hits if hit.source_id in allowed or hit.metadata.get("document_id") in allowed][:limit]


class FakeLlamaClient:
    def __init__(self, *, available: bool = True, response: str = "Answer text."):
        self._available = available
        self._response = response

    @property
    def available(self) -> bool:
        return self._available

    async def chat(self, messages, **kwargs):
        return {"choices": [{"message": {"content": self._response}}]}

    async def chat_stream(self, messages, **kwargs):
        for word in self._response.split():
            yield word + " "


# --- RAGPipeline tests ---


@pytest.mark.asyncio
async def test_rag_answer_returns_result_with_sources():
    hits = [_make_hit("Rechnung_2024", "Total: 4250 EUR", score=0.8)]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(response="Der Gesamtbetrag ist 4.250 EUR.")
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer("Wie hoch?", "system prompt", [])
    assert isinstance(result, RAGResult)
    assert "4.250" in result.answer
    assert len(result.sources) == 1
    assert result.sources[0].title == "Rechnung_2024"
    assert result.had_sufficient_context is True
    assert result.retrieval_hits_count == 1


@pytest.mark.asyncio
async def test_rag_answer_no_hits_returns_empty():
    retrieval = FakeRetrievalService([])
    llm = FakeLlamaClient()
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer("question", "system", [])
    assert result.answer == ""
    assert result.had_sufficient_context is False


@pytest.mark.asyncio
async def test_rag_answer_stream_yields_tokens():
    hits = [_make_hit("Doc1", "Content here", score=0.6)]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(response="token1 token2 token3")
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    tokens = []
    async for token in pipeline.answer_stream("q", "system", []):
        tokens.append(token)
    assert len(tokens) == 3


@pytest.mark.asyncio
async def test_rag_answer_stream_populates_stream_result():
    hits = [_make_hit("Doc1", "Content", score=0.7)]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(response="answer")
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    stream_result = RAGStreamResult()
    tokens = []
    async for token in pipeline.answer_stream("q", "system", [], stream_result=stream_result):
        tokens.append(token)
    assert stream_result.retrieval_hits_count == 1
    assert stream_result.reranked_count == 1
    assert len(stream_result.sources) == 1
    assert stream_result.sources[0].title == "Doc1"


@pytest.mark.asyncio
async def test_rag_answer_stream_raises_on_no_hits():
    retrieval = FakeRetrievalService([])
    llm = FakeLlamaClient()
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    with pytest.raises(NoRetrievalHitsError):
        async for _ in pipeline.answer_stream("q", "system", []):
            pass


@pytest.mark.asyncio
async def test_rag_retrieve_uses_profile_plus_archive_scope():
    retrieval = FakeRetrievalService([_make_hit("D", "text", score=0.5)])
    llm = FakeLlamaClient()
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    await pipeline.answer("q", "system", [])
    assert retrieval.last_scope == "profile_plus_archive"


@pytest.mark.asyncio
async def test_rag_min_score_filters_low_hits():
    hits = [
        _make_hit("Good", "relevant", score=0.8),
        _make_hit("Bad", "irrelevant", score=0.01),
    ]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient()
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer("q", "system", [], min_score=0.1)
    assert result.reranked_count == 1
    assert result.sources[0].title == "Good"


@pytest.mark.asyncio
async def test_rag_preserves_single_hit_when_reranker_rejects_it():
    class LowScoreReranker:
        async def rerank(self, query: str, hits: list[RetrievalHit], top_n: int) -> list[RetrievalHit]:
            return [hits[0].model_copy(update={"score": 0.0})]

    hits = [_make_hit("Invoice", "Invoice total is 4250 EUR due 2026-04-02.", score=0.8)]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(response="The invoice total is 4250 EUR [Invoice].")
    pipeline = RAGPipeline(retrieval=retrieval, reranker=LowScoreReranker(), llm_client=llm)

    result = await pipeline.answer("What is the invoice total?", "system", [], min_score=0.1)

    assert result.answer
    assert result.reranked_count == 1
    assert result.sources[0].title == "Invoice"


@pytest.mark.asyncio
async def test_rag_focuses_explicit_uploaded_document_queries_on_named_documents():
    hits = [
        _make_hit("Rechnung_2024_001", "Invoice total is 2500 EUR due 2024-04-30.", score=0.95),
        _make_hit("acme_invoice", "ACME GmbH invoice due on 2024-04-30.", score=0.55),
        _make_hit("acme_offer", "ACME offer target amount is 3100 EUR for 50 units.", score=0.52),
    ]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(response="ACME answer.")
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer(
        "Aus den hochgeladenen ACME-Dokumenten: Worum geht es im Angebot und wann ist die Rechnung faellig?",
        "system",
        [],
    )

    assert "acme_offer" in result.answer
    assert "Rechnung_2024_001" not in result.answer
    assert [source.title for source in result.sources] == ["acme_invoice", "acme_offer"]


@pytest.mark.asyncio
async def test_rag_rejects_memory_only_hits_for_explicit_uploaded_document_query():
    hits = [
        RetrievalHit(
            source_type="memory",
            source_id="preferred_workspace",
            score=0.2,
            text="preferred_workspace: Desktop setup",
            metadata={"title": "preferred_workspace"},
        )
    ]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(response="Should not answer from memory.")
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer(
        "Aus den hochgeladenen ACME-Dokumenten: Worum geht es im Angebot und wann ist die Rechnung faellig?",
        "system",
        [],
    )

    assert result.answer == ""
    assert result.had_sufficient_context is False


@pytest.mark.asyncio
async def test_rag_uses_extractive_answer_for_explicit_uploaded_offer_invoice_query():
    hits = [
        _make_hit(
            "acme_invoice.txt",
            "ACME GmbH Invoice\nDue date: 2026-04-02\nAmount: 2,750 EUR",
            score=0.8,
        ),
        _make_hit(
            "acme_offer.txt",
            "ACME GmbH UI Modernization Offer\nProject: UI modernization and validation reporting\nTarget amount: 4,850 EUR",
            score=0.7,
        ),
    ]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(response="Wrong answer that should be bypassed.")
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer(
        "Aus den hochgeladenen ACME-Dokumenten: Worum geht es im Angebot, wie hoch ist der Zielbetrag und wann ist die Rechnung faellig?",
        "system",
        [],
    )

    assert "UI modernization and validation reporting" in result.answer
    assert "4,850 EUR [acme_offer.txt]" in result.answer
    assert "2026-04-02" in result.answer


@pytest.mark.asyncio
async def test_rag_warns_when_answer_depends_on_low_confidence_ocr():
    hits = [
        RetrievalHit(
            source_type="document",
            source_id="doc:invoice",
            score=0.8,
            text="ACME GmbH Invoice\nDue date: 2026-04-02\nAmount: 2,750 EUR",
            metadata={
                "title": "acme_invoice.txt",
                "chunk_index": 0,
                "file_path": "/docs/acme_invoice.pdf",
                "ocr_used": True,
                "ocr_low_confidence": True,
                "ocr_confidence_avg": 0.52,
            },
        ),
        RetrievalHit(
            source_type="document",
            source_id="doc:offer",
            score=0.7,
            text="ACME GmbH Offer\nProject: UI modernization and validation reporting\nTarget amount: 4,850 EUR",
            metadata={
                "title": "acme_offer.txt",
                "chunk_index": 0,
                "file_path": "/docs/acme_offer.pdf",
                "ocr_used": True,
                "ocr_low_confidence": True,
                "ocr_confidence_avg": 0.52,
            },
        ),
    ]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(response="Wrong answer that should be bypassed.")
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer(
        "Aus den hochgeladenen ACME-Dokumenten: Worum geht es im Angebot, wie hoch ist der Zielbetrag und wann ist die Rechnung faellig?",
        "system",
        [],
    )

    assert "Hinweis:" in result.answer


@pytest.mark.asyncio
async def test_rag_preserves_single_positive_hit_even_below_min_score():
    class LowScoreReranker:
        async def rerank(self, query: str, hits: list[RetrievalHit], top_n: int) -> list[RetrievalHit]:
            return [hits[0].model_copy(update={"score": 0.0})]

    hits = [_make_hit("Invoice", "Invoice total is 4250 EUR due 2026-04-02.", score=0.05)]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(response="The invoice total is 4250 EUR [Invoice].")
    pipeline = RAGPipeline(retrieval=retrieval, reranker=LowScoreReranker(), llm_client=llm)

    result = await pipeline.answer("What is the invoice total?", "system", [], min_score=0.1)

    assert result.answer
    assert result.sources[0].title == "Invoice"


@pytest.mark.asyncio
async def test_rag_does_not_preserve_zero_score_single_hit():
    class LowScoreReranker:
        async def rerank(self, query: str, hits: list[RetrievalHit], top_n: int) -> list[RetrievalHit]:
            return [hits[0].model_copy(update={"score": 0.0})]

    hits = [_make_hit("Invoice", "Invoice total is 4250 EUR due 2026-04-02.", score=0.0)]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(response="Should not be used.")
    pipeline = RAGPipeline(retrieval=retrieval, reranker=LowScoreReranker(), llm_client=llm)

    result = await pipeline.answer("What is the invoice total?", "system", [], min_score=0.1)

    assert result.answer == ""
    assert result.had_sufficient_context is False


@pytest.mark.asyncio
async def test_rag_budget_trims_history_when_context_full():
    big_chunk = "x " * 4000
    hits = [_make_hit("Big", big_chunk, score=0.9)]
    long_history = [{"role": "user", "content": f"turn {i}"} for i in range(50)]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient()
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer(
        "q", "system prompt", long_history, context_window=2048, max_tokens=256,
    )
    assert isinstance(result, RAGResult)


@pytest.mark.asyncio
async def test_rag_llm_unavailable_returns_empty():
    hits = [_make_hit("D", "text")]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(available=False)
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer("q", "system", [])
    assert result.answer == ""
    assert result.had_sufficient_context is False


@pytest.mark.asyncio
async def test_rag_multi_document_falls_back_to_extractive_summary_without_llm():
    hits = [
        RetrievalHit(source_type="document", source_id="doc-a", score=0.8, text="Invoice A is due on 2026-04-02.", metadata={"title": "Invoice A", "document_id": "doc-a"}),
        RetrievalHit(source_type="document", source_id="doc-b", score=0.7, text="Invoice B is due on 2026-04-10.", metadata={"title": "Invoice B", "document_id": "doc-b"}),
    ]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(available=False)
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer_multi_document("compare invoice due dates", ["doc-a", "doc-b"])

    assert "Cross-document summary" in result.answer
    assert "Invoice A" in result.answer
    assert "Invoice B" in result.answer
    assert "Key due dates:" in result.answer
    assert "different due dates" in result.answer
    assert result.had_sufficient_context is True


@pytest.mark.asyncio
async def test_rag_multi_document_extracts_amount_and_counterparty_differences_without_llm():
    hits = [
        RetrievalHit(
            source_type="document",
            source_id="doc-a",
            score=0.8,
            text="Acme GmbH invoice total is 4200 EUR due on 2026-04-02.",
            metadata={"title": "Invoice A", "document_id": "doc-a"},
        ),
        RetrievalHit(
            source_type="document",
            source_id="doc-b",
            score=0.7,
            text="Beta GmbH invoice total is 5100 EUR due on 2026-04-02.",
            metadata={"title": "Invoice B", "document_id": "doc-b"},
        ),
    ]
    retrieval = FakeRetrievalService(hits)
    llm = FakeLlamaClient(available=False)
    pipeline = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=llm)

    result = await pipeline.answer_multi_document("compare invoices", ["doc-a", "doc-b"])

    assert "Key amounts:" in result.answer
    assert "4200 EUR" in result.answer
    assert "5100 EUR" in result.answer
    assert "Key entities:" in result.answer
    assert "different amounts" in result.answer
    assert "different counterparties" in result.answer


# --- Token estimation tests ---


def test_token_estimation_accuracy():
    from app.rag import _TOKEN_PATTERN
    text = "Der Gesamtbetrag der Rechnung beträgt 4.250,00 EUR inklusive Mehrwertsteuer."
    tokens = _TOKEN_PATTERN.findall(text)
    assert 8 <= len(tokens) <= 14


def test_token_estimation_german_compounds():
    from app.rag import _TOKEN_PATTERN
    text = "Umsatzsteuervoranmeldung Einkommenssteuererklärung"
    tokens = _TOKEN_PATTERN.findall(text)
    assert len(tokens) == 2


# --- ScoreFusionReranker tests ---


@pytest.mark.asyncio
async def test_score_fusion_reranker_respects_top_n():
    hits = [_make_hit(f"Doc{i}", f"text {i}", score=0.5 - i * 0.1) for i in range(6)]
    reranker = ScoreFusionReranker()
    result = await reranker.rerank("text query", hits, top_n=3)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_score_fusion_reranker_boosts_title_matches():
    hits = [
        _make_hit("Unrelated", "some text about weather"),
        _make_hit("Rechnung Schmidt", "invoice total 500"),
    ]
    reranker = ScoreFusionReranker()
    result = await reranker.rerank("Rechnung Schmidt", hits, top_n=2)
    assert result[0].metadata.get("title") == "Rechnung Schmidt"


@pytest.mark.asyncio
async def test_score_fusion_reranker_empty_input():
    reranker = ScoreFusionReranker()
    result = await reranker.rerank("query", [], top_n=4)
    assert result == []


# --- LLMReranker tests ---


@pytest.mark.asyncio
async def test_llm_reranker_scores_and_sorts():
    class MockLLM:
        available = True
        _call_count = 0

        async def chat(self, messages, **kwargs):
            self._call_count += 1
            score = 8 if "important data" in messages[1]["content"] else 2
            return {"choices": [{"message": {"content": str(score)}}]}

    hits = [
        _make_hit("LowRelevance", "nothing useful here", score=0.9),
        _make_hit("HighRelevance", "important data about invoices", score=0.3),
    ]
    llm = MockLLM()
    reranker = LLMReranker(llm)
    result = await reranker.rerank("find invoices", hits, top_n=2)
    assert result[0].metadata.get("title") == "HighRelevance"
    assert llm._call_count == 2
    assert result[0].score >= 0.3


@pytest.mark.asyncio
async def test_llm_reranker_handles_failures_gracefully():
    class FailingLLM:
        available = True

        async def chat(self, messages, **kwargs):
            raise ConnectionError("server down")

    hits = [_make_hit("Doc", "text", score=0.6)]
    reranker = LLMReranker(FailingLLM())
    result = await reranker.rerank("query", hits, top_n=1)
    assert len(result) == 1
    assert result[0].score == 0.3  # 0.6 * 0.5 fallback


@pytest.mark.asyncio
async def test_llm_reranker_retains_some_original_score_when_llm_scores_zero():
    class ZeroLLM:
        available = True

        async def chat(self, messages, **kwargs):
            return {"choices": [{"message": {"content": "0"}}]}

    hits = [_make_hit("Invoice", "invoice text", score=0.2)]
    reranker = LLMReranker(ZeroLLM())
    result = await reranker.rerank("query", hits, top_n=1)
    assert len(result) == 1
    assert result[0].score >= 0.15


@pytest.mark.asyncio
async def test_llm_reranker_unavailable_returns_truncated():
    class OfflineLLM:
        available = False

    hits = [_make_hit(f"D{i}", f"t{i}") for i in range(5)]
    reranker = LLMReranker(OfflineLLM())
    result = await reranker.rerank("q", hits, top_n=2)
    assert len(result) == 2


# --- RAGSource / RAGResult model tests ---


def test_rag_source_model():
    src = RAGSource(title="Test", score=0.85, source_type="document")
    assert src.title == "Test"
    assert src.file_path is None
    assert src.chunk_index == 0


def test_rag_result_model():
    result = RAGResult(
        answer="Test answer",
        sources=[RAGSource(title="Doc1", score=0.9)],
        retrieval_hits_count=5,
        reranked_count=2,
    )
    assert result.had_sufficient_context is True
    assert len(result.sources) == 1
