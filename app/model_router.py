from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from pathlib import Path

from app.config import settings
from app.types import ActiveContextSummary, ModelRouteSnapshot, PromptCacheSnapshot


class PromptResponseCache:
    def __init__(self, *, enabled: bool, max_entries: int) -> None:
        self.enabled = enabled
        self.max_entries = max(1, max_entries)
        self._entries: OrderedDict[str, str] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def key_for(
        self,
        *,
        route: ModelRouteSnapshot,
        text: str,
        history: list[dict[str, str]],
        context_revision: str = "",
        knowledge_revision: str = "",
        memory_revision: str = "",
    ) -> str:
        payload = {
            "mode": route.selected_mode,
            "model": route.requested_model or "",
            "text": text.strip().lower(),
            "history": history[-4:],
            "context_revision": context_revision,
            "knowledge_revision": knowledge_revision,
            "memory_revision": memory_revision,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        if not self.enabled:
            return None
        value = self._entries.get(key)
        if value is None:
            self.misses += 1
            return None
        self.hits += 1
        self._entries.move_to_end(key)
        return value

    def put(self, key: str, value: str) -> None:
        if not self.enabled or not value.strip():
            return
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def snapshot(self) -> PromptCacheSnapshot:
        return PromptCacheSnapshot(
            enabled=self.enabled,
            entries=len(self._entries),
            hits=self.hits,
            misses=self.misses,
        )


class ModelRouter:
    def __init__(self, *, mode: str | None = None, fast_model: str | None = None, deep_model: str | None = None) -> None:
        self.mode = (mode or settings.model_mode or "off").lower()
        self.fast_model = fast_model or settings.fast_model_path
        self.deep_model = deep_model or settings.deep_model_path
        cache_enabled = settings.prompt_cache_enabled and self.mode not in {"off", "none", "disabled"}
        self.cache = PromptResponseCache(enabled=cache_enabled, max_entries=settings.prompt_cache_size)

    def choose(
        self,
        text: str,
        *,
        context_summary: ActiveContextSummary | None = None,
        rag_candidate: bool = False,
        llm_available: bool = True,
    ) -> ModelRouteSnapshot:
        if not llm_available or self.mode in {"off", "none", "disabled"}:
            return ModelRouteSnapshot(
                selected_mode="unavailable",
                strategy="disabled",
                reason="LLM mode is disabled or unavailable.",
                fallback_used=True,
            )

        lowered = text.lower().strip()
        mode = self.mode if self.mode in {"fast", "deep", "auto", "hybrid", "single", "llm", "on"} else "auto"
        workload_markers = (
            "compare",
            "summarize",
            "analyse",
            "analyze",
            "reason",
            "tradeoff",
            "why",
            "invoice",
            "rechnung",
            "contract",
            "audit",
            "backup",
            "meeting",
            "transcript",
            "document",
            "spreadsheet",
            "architecture",
            "roadmap",
        )
        rag_markers = (
            "document",
            "documents",
            "dokument",
            "dokumente",
            "dokumenten",
            "archive",
            "archives",
            "knowledge",
            "invoice",
            "contract",
            "meeting",
            "transcript",
            "search",
            "find",
        )
        drafting_markers = (
            "write ",
            "draft ",
            "compose ",
            "schreibe ",
            "formuliere ",
            "e-mail",
            "email",
        )
        capability_markers = (
            "what can you do",
            "what can you actually help me with",
            "what can you help me with",
            "what can you do here",
            "what do you have here",
            "womit kannst",
            "was kannst du",
        )
        explicit_context_markers = (
            "uploaded",
            "hochgeladen",
            "from the documents",
            "aus den dokumenten",
            "from context",
            "aus dem kontext",
            "based on the document",
            "based on the documents",
        )
        context_pressure = 0
        if context_summary is not None:
            context_pressure = (
                len(context_summary.tasks)
                + len(context_summary.reminders)
                + len(context_summary.open_loops)
                + len(context_summary.events)
            )
        wants_deep = len(text) > 220 or context_pressure >= 6 or lowered.count(" and ") >= 2 or any(
            marker in lowered for marker in workload_markers
        )
        asks_for_capabilities = any(marker in lowered for marker in capability_markers) or (
            "workspace" in lowered
            and any(fragment in lowered for fragment in ("what can you", "help me", "what do you"))
        )
        is_drafting_request = any(marker in lowered for marker in drafting_markers)
        explicitly_context_grounded = any(marker in lowered for marker in explicit_context_markers)
        use_rag = rag_candidate and any(marker in lowered for marker in rag_markers)
        if asks_for_capabilities or (is_drafting_request and not explicitly_context_grounded):
            use_rag = False

        selected_mode = "fast"
        reason = "Single-model mode routes conversational replies to the fast path."
        if mode == "deep":
            selected_mode = "deep"
            reason = "Manual model mode selected."
        elif mode == "fast":
            selected_mode = "fast"
            reason = "Manual model mode selected."
        elif mode in {"auto", "hybrid"}:
            selected_mode = "deep" if wants_deep else "fast"
            reason = (
                "Auto route selected a deeper path for higher-context reasoning."
                if wants_deep
                else "Auto route selected a faster path for lightweight chat."
            )

        requested_model = self._normalize_model_name(self.deep_model if selected_mode == "deep" else self.fast_model)
        return ModelRouteSnapshot(
            selected_mode=selected_mode,
            requested_model=requested_model,
            strategy="rag" if use_rag else ("single" if mode in {"single", "llm", "on"} else mode),
            reason=reason,
            used_rag=use_rag,
            fallback_used=False,
            cache_hit=False,
        )

    def cache_lookup(
        self,
        route: ModelRouteSnapshot,
        text: str,
        history: list[dict[str, str]],
        *,
        context_revision: str = "",
        knowledge_revision: str = "",
        memory_revision: str = "",
    ) -> tuple[str | None, str]:
        key = self.cache.key_for(
            route=route,
            text=text,
            history=history,
            context_revision=context_revision,
            knowledge_revision=knowledge_revision,
            memory_revision=memory_revision,
        )
        value = self.cache.get(key)
        if value is not None:
            route = route.model_copy(update={"cache_hit": True})
        return value, key

    def cache_store(self, cache_key: str, value: str) -> None:
        self.cache.put(cache_key, value)

    def cache_snapshot(self) -> PromptCacheSnapshot:
        return self.cache.snapshot()

    def _normalize_model_name(self, value: str | None) -> str | None:
        if not value:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if any(sep in stripped for sep in ("\\", "/", ":")):
            stripped = Path(stripped).name
        if stripped.endswith(".gguf"):
            stripped = stripped[:-5]
        return stripped or None
