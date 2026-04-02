from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.metrics import metrics

logger = logging.getLogger(__name__)


class LlamaServerClient:
    """Async HTTP client for llama-server's OpenAI-compatible API."""

    def __init__(self, base_url: str, timeout: float = 30.0, default_model: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.default_model = default_model.strip() if default_model else None
        self._session: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        self._session = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout, connect=10.0),
        )

    async def shutdown(self) -> None:
        if self._session:
            await self._session.aclose()
            self._session = None

    @property
    def available(self) -> bool:
        return self._session is not None

    async def health(self) -> bool:
        if not self._session:
            return False
        # Try standard llama-server /health first, then ollama-compatible /models fallback
        for path in ("/health", "/models"):
            try:
                response = await self._session.get(path)
                if response.status_code == 200:
                    return True
            except (httpx.HTTPError, httpx.StreamError):
                continue
        return False

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        return await self._post_chat(messages, stream=False, **kwargs)

    async def chat_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[str]:
        if not self._session:
            return
        body = self._build_body(messages, stream=True, **kwargs)
        try:
            async with self._session.stream("POST", "/v1/chat/completions", json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
        except (httpx.HTTPError, httpx.StreamError):
            return

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self._post_chat(messages, stream=False, tools=tools, **kwargs)

    async def _post_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self._session:
            raise RuntimeError("LlamaServerClient is not started.")
        body = self._build_body(messages, stream=stream, **kwargs)
        t0 = time.monotonic()
        metrics.inc("kern_llm_requests_total")
        try:
            with metrics.timer("kern_llm_inference_seconds"):
                response = await self._session.post("/v1/chat/completions", json=body)
                response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            metrics.inc("kern_llm_requests_total", labels={"status": "error"})
            elapsed = time.monotonic() - t0
            logger.warning("LLM attempt 1 failed (%.1fs): %s", elapsed, exc)
            if "model" in body:
                t1 = time.monotonic()
                try:
                    fallback_body = dict(body)
                    fallback_body.pop("model", None)
                    response = await self._session.post("/v1/chat/completions", json=fallback_body)
                    response.raise_for_status()
                    logger.info("LLM retry without model succeeded (%.1fs)", time.monotonic() - t1)
                    return response.json()
                except Exception as retry_exc:
                    logger.error("LLM retry without model failed (%.1fs): %s", time.monotonic() - t1, retry_exc)
            raise exc
        except (httpx.HTTPError, httpx.StreamError) as exc:
            metrics.inc("kern_llm_requests_total", labels={"status": "error"})
            elapsed = time.monotonic() - t0
            logger.warning("LLM attempt 1 failed (%.1fs): %s — retrying", elapsed, exc)
            t1 = time.monotonic()
            try:
                response = await self._session.post("/v1/chat/completions", json=dict(body))
                response.raise_for_status()
                logger.info("LLM retry 1 succeeded (%.1fs)", time.monotonic() - t1)
                return response.json()
            except Exception as retry_exc:
                logger.error("LLM retry 1 failed (%.1fs): %s — trying without model param", time.monotonic() - t1, retry_exc)
                if "model" in body:
                    t2 = time.monotonic()
                    try:
                        retry_body = dict(body)
                        retry_body.pop("model", None)
                        response = await self._session.post("/v1/chat/completions", json=retry_body)
                        response.raise_for_status()
                        logger.info("LLM retry 2 (no model) succeeded (%.1fs)", time.monotonic() - t2)
                        return response.json()
                    except Exception as final_exc:
                        logger.error("LLM all retries exhausted (%.1fs): %s", time.monotonic() - t2, final_exc)
                raise exc from retry_exc

    def _build_body(self, messages: list[dict[str, Any]], *, stream: bool, **kwargs: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"messages": messages, "stream": stream}
        filtered = {key: value for key, value in kwargs.items() if value is not None}
        if "model" not in filtered and self.default_model:
            filtered["model"] = self.default_model
        body.update(filtered)
        return body
