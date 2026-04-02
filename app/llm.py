from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from app.cognition import CognitionResult, HybridCognitionEngine
from app.config import settings
from app.intent import ParsedIntent, RuleBasedIntentEngine
from app.persona import KERN_RAG_SYSTEM_PROMPT, KERN_SYSTEM_PROMPT, PersonaEngine
from app.rag import RAGStreamResult
from app.types import ActiveContextSummary

if TYPE_CHECKING:
    from app.llm_client import LlamaServerClient
    from app.rag import RAGPipeline
    from app.tool_calling import ToolCallingBridge

logger = logging.getLogger(__name__)


class Brain:
    """Compatibility wrapper around local speech, cognition, and persona layers."""

    def __init__(
        self,
        openai_api_key: str | None,
        allow_cloud_llm: bool = False,
        local_mode_enabled: bool = True,
        intent_engine: RuleBasedIntentEngine | None = None,
        persona_engine: PersonaEngine | None = None,
        cognition_backend: str = "hybrid",
        cognition_model: str | None = None,
        llm_client: "LlamaServerClient | None" = None,
        tool_calling_bridge: "ToolCallingBridge | None" = None,
        rag_pipeline: "RAGPipeline | None" = None,
    ) -> None:
        self.openai_api_key = openai_api_key
        self.allow_cloud_llm = False if settings.llm_local_only else allow_cloud_llm
        self.local_mode_enabled = local_mode_enabled
        self._persona = persona_engine or PersonaEngine()
        self.llm_client = llm_client
        self._rag = rag_pipeline
        self._cognition = HybridCognitionEngine(
            rule_engine=intent_engine or RuleBasedIntentEngine(),
            backend=cognition_backend,
            model_path=cognition_model,
            intent_fallback_mode=settings.intent_fallback_mode,
            intent_fallback_min_confidence=settings.intent_fallback_min_confidence,
            llm_client=llm_client,
            tool_calling_bridge=tool_calling_bridge,
        )

    @property
    def llm_available(self) -> bool:
        return self.llm_client is not None and self.llm_client.available

    @property
    def rag_available(self) -> bool:
        return self._rag is not None and self.llm_available

    @property
    def cloud_available(self) -> bool:
        return (not settings.llm_local_only) and self.allow_cloud_llm and bool(self.openai_api_key)

    @property
    def cognition_backend(self) -> str:
        return self._cognition.backend

    def set_local_mode(self, enabled: bool) -> None:
        self.local_mode_enabled = enabled

    def parse_intent(self, text: str, dialogue_context: dict[str, str] | None = None) -> ParsedIntent:
        return self._cognition.match_single(text, dialogue_context=dialogue_context)

    def analyze_intent(
        self,
        text: str,
        dialogue_context: dict[str, str] | None = None,
        context_summary: ActiveContextSummary | None = None,
        available_capabilities: list[str] | None = None,
    ) -> CognitionResult:
        return self._cognition.analyze(
            text,
            dialogue_context=dialogue_context,
            context_summary=context_summary,
            available_capabilities=available_capabilities,
        )

    def generate_chat_reply(self, text: str, preferred_title: str) -> str:
        return self._persona.chat_reply(text, preferred_title).display_text

    def generate_chat_persona_reply(self, text: str, preferred_title: str):
        return self._persona.chat_reply(text, preferred_title)

    def looks_operational_request(self, text: str) -> bool:
        return self._cognition.looks_operational_request(text)

    async def generate_llm_reply(
        self,
        text: str,
        preferred_title: str,
        conversation_history: list[dict[str, str]] | None = None,
        context_summary: ActiveContextSummary | None = None,
        model_override: str | None = None,
    ) -> str | None:
        if not self.llm_available:
            return None
        history = conversation_history or []
        reply = await self._persona.llm_chat_reply(
            text,
            preferred_title,
            history,
            self.llm_client,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            model_override=model_override,
        )
        return reply.display_text if reply else None

    async def generate_rag_reply_stream(
        self,
        text: str,
        preferred_title: str,
        conversation_history: list[dict[str, str]] | None = None,
        stream_result: RAGStreamResult | None = None,
        model_override: str | None = None,
    ) -> AsyncIterator[str]:
        if not self.rag_available:
            return
        history = conversation_history or []
        system_msg = f"{KERN_RAG_SYSTEM_PROMPT} Address the user as '{preferred_title}'."
        async for token in self._rag.answer_stream(
            text,
            system_msg,
            history,
            top_k=settings.rag_top_k,
            rerank_top_n=settings.rag_rerank_top_n,
            min_score=settings.rag_min_score,
            context_window=settings.llm_context_window,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            stream_result=stream_result,
            model_override=model_override,
        ):
            yield token

    async def generate_rag_reply(
        self,
        text: str,
        preferred_title: str,
        conversation_history: list[dict[str, str]] | None = None,
        model_override: str | None = None,
    ) -> tuple[str | None, list]:
        if not self.rag_available:
            return None, []
        history = conversation_history or []
        system_msg = f"{KERN_RAG_SYSTEM_PROMPT} Address the user as '{preferred_title}'."
        try:
            result = await self._rag.answer(
                text,
                system_msg,
                history,
                top_k=settings.rag_top_k,
                rerank_top_n=settings.rag_rerank_top_n,
                min_score=settings.rag_min_score,
                context_window=settings.llm_context_window,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
                model_override=model_override,
            )
            if result.answer:
                return result.answer, [s.model_dump() for s in result.sources]
        except Exception as exc:
            logger.debug("Cloud LLM RAG fallback failed: %s", exc, exc_info=True)
        return None, []

    async def generate_llm_reply_stream(
        self,
        text: str,
        preferred_title: str,
        conversation_history: list[dict[str, str]] | None = None,
        model_override: str | None = None,
    ) -> AsyncIterator[str]:
        if not self.llm_available:
            return
        history = conversation_history or []
        system_msg = f"{KERN_SYSTEM_PROMPT} Address the user as '{preferred_title}'."
        messages: list[dict[str, str]] = [{"role": "system", "content": system_msg}]
        messages.extend(history[-20:])
        messages.append({"role": "user", "content": text})
        async for token in self.llm_client.chat_stream(
            messages,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            model=model_override,
        ):
            yield token

    def tool_preamble(self, request, preferred_title: str) -> str:
        return self._persona.tool_preamble(request, preferred_title)

    def failure_reply(self, preferred_title: str) -> str:
        return self._persona.failure_reply(preferred_title)
