from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import math
import re
from typing import TYPE_CHECKING

from app.intent import ParsedIntent, RuleBasedIntentEngine
from app.planning import HeuristicPlanner, LlamaCppPlanner, LlamaServerPlanner
from app.types import ActiveContextSummary, ExecutionPlan, IntentCandidate, ToolRequest

if TYPE_CHECKING:
    from app.llm_client import LlamaServerClient
    from app.tool_calling import ToolCallingBridge


@dataclass(slots=True)
class CognitionResult:
    parsed_intent: ParsedIntent
    candidates: list[IntentCandidate]
    execution_plan: ExecutionPlan


class SemanticIntentMatcher:
    def match(self, text: str, dialogue_context: dict[str, str] | None = None) -> ParsedIntent | None:
        lowered = text.lower().strip()
        dialogue_context = dialogue_context or {}

        if any(token in lowered for token in ["search my documents", "look in my documents", "find in my docs"]):
            query = text.lower()
            for phrase in [
                "search my documents for",
                "search my documents",
                "look in my documents for",
                "look in my documents",
                "find in my docs for",
                "find in my docs",
            ]:
                query = query.replace(phrase, "", 1)
            return self._request(
                text,
                "query",
                "search_documents",
                "Search ingested documents.",
                0.78,
                "search_documents",
                {"query": query.strip(" :.?!")},
                "The user asked to search documents using a paraphrase.",
            )

        if any(token in lowered for token in ["show recent audit events", "recent audit events", "audit trail"]):
            query = lowered.replace("show recent audit events", "").replace("recent audit events", "").replace("audit trail", "").strip(" :.?!")
            return self._request(
                text,
                "query",
                "read_audit_events",
                "Read recent audit events.",
                0.76,
                "read_audit_events",
                {"query": query, "limit": 8},
                "The user asked to read audit events using a paraphrase.",
            )

        if any(token in lowered for token in ["show my current context", "what is my current context", "what app am i in", "what is in my clipboard"]):
            return self._request(
                text,
                "query",
                "read_current_context",
                "Read the current local context.",
                0.75,
                "read_current_context",
                {},
                "The user asked to read the current local context using a paraphrase.",
            )

        if any(token in lowered for token in ["runtime snapshot", "system snapshot", "active profile", "memory scope"]):
            return self._request(
                text,
                "query",
                "read_runtime_snapshot",
                "Read the runtime snapshot.",
                0.74,
                "read_runtime_snapshot",
                {},
                "The user asked for runtime state using a paraphrase.",
            )

        if any(token in lowered for token in ["list backups", "show backups", "available backups"]):
            return self._request(
                text,
                "query",
                "list_backups",
                "List available backups.",
                0.76,
                "list_backups",
                {},
                "The user asked to list backups using a paraphrase.",
            )

        if any(token in lowered for token in ["create an encrypted backup", "make an encrypted backup"]):
            return self._request(
                text,
                "action",
                "create_backup",
                "Create an encrypted backup.",
                0.74,
                "create_backup",
                {"label": "Manual backup"},
                "The user asked to create an encrypted backup using a paraphrase.",
            )

        if any(token in lowered for token in ["create a draft angebot", "make an angebot", "make an invoice", "create an invoice", "create a rechnung"]):
            if any(token in lowered for token in ["invoice", "rechnung"]):
                intent_name = "create_rechnung"
                tool_name = "create_rechnung"
                hint = "Create a German invoice draft."
                query = re.split(r"(?:invoice|rechnung)", text, maxsplit=1, flags=re.IGNORECASE)[-1]
            else:
                intent_name = "create_angebot"
                tool_name = "create_angebot"
                hint = "Create a German offer draft."
                query = re.split(r"angebot", text, maxsplit=1, flags=re.IGNORECASE)[-1]
            return self._request(
                text,
                "action",
                intent_name,
                hint,
                0.75,
                tool_name,
                {"customer_name": query.strip(" :.?!") or "Kunde"},
                "The user asked for a German business draft using a paraphrase.",
            )

        if any(token in lowered for token in ["what's still on my plate", "what is still on my plate", "what do i still need to do"]):
            return self._request(
                text,
                "query",
                "tasks",
                "Read pending tasks.",
                0.76,
                "get_pending_tasks",
                {},
                "The user asked for active work in paraphrased form.",
            )

        if any(token in lowered for token in ["what is coming up", "what's coming up", "what's next on my agenda", "what is next on my agenda"]):
            return self._request(
                text,
                "query",
                "calendar",
                "Read today's calendar.",
                0.76,
                "get_today_calendar",
                {},
                "The user asked for the agenda in paraphrased form.",
            )

        if any(token in lowered for token in ["capture this", "write this down", "make a note of this"]):
            content = text
            for phrase in ["capture this", "write this down", "make a note of this"]:
                content = content.lower().replace(phrase, "", 1)
            return self._request(
                text,
                "action",
                "create_note",
                "Create a local note.",
                0.75,
                "create_note",
                {"content": content.strip(" :.?!")},
                "The user asked to save a note using a paraphrase.",
            )

        if any(token in lowered for token in ["brief me", "start my day", "give me the lay of the land"]):
            return self._request(
                text,
                "query",
                "morning_brief",
                "Generate a morning brief.",
                0.72,
                "generate_morning_brief",
                {},
                "The user asked for a day briefing using a paraphrase.",
            )

        if any(token in lowered for token in ["keep in mind that", "make a note that you should remember"]):
            payload = lowered.replace("keep in mind that", "").replace("make a note that you should remember", "").strip(" :.?!")
            key, value = self._memory_key_value(payload)
            return self._request(
                text,
                "action",
                "remember_fact",
                "Store a durable fact.",
                0.72,
                "remember_fact",
                {"key": key, "value": value},
                "The user asked to remember information in paraphrased form.",
            )

        if any(token in lowered for token in ["launch ", "fire up ", "boot up "]):
            target = text.split(" ", 1)[-1].strip()
            return self._request(
                text,
                "action",
                "open_app",
                "Open a desktop app.",
                0.71,
                "open_app",
                {"app": target},
                "The user asked to open an application using a paraphrase.",
            )

        if any(token in lowered for token in ["search the workspace for", "look through files for"]):
            query = lowered.replace("search the workspace for", "").replace("look through files for", "").strip(" :.?!")
            return self._request(
                text,
                "query",
                "search_files",
                "Search workspace files.",
                0.73,
                "search_files",
                {"query": query},
                "The user asked to search local files using a paraphrase.",
            )

        if any(token in lowered for token in ["lock me into focus", "put me in focus mode", "start a focus block"]):
            return self._request(
                text,
                "action",
                "focus_mode",
                "Start focus mode.",
                0.79,
                "focus_mode",
                {"minutes": 50, "title": "Focus block"},
                "The user asked to begin focus mode using a paraphrase.",
            )

        if any(token in lowered for token in ["find on the web", "search online for"]):
            query = lowered.replace("find on the web", "").replace("search online for", "").strip(" :.?!")
            return self._request(
                text,
                "action",
                "browser_search",
                "Search the web.",
                0.72,
                "browser_search",
                {"query": query},
                "The user asked to search the web using a paraphrase.",
            )

        if "that reminder" in lowered and "snooze" in lowered:
            raw = dialogue_context.get("last_announced_reminder_id") or "0"
            if raw.isdigit() and int(raw) > 0:
                return self._request(
                    text,
                    "action",
                    "snooze_reminder",
                    "Snooze a reminder.",
                    0.7,
                    "snooze_reminder",
                    {"reminder_id": int(raw), "minutes": 10},
                    "The user referred back to the most recent reminder.",
                )
        return None

    def _request(
        self,
        text: str,
        intent_type,
        intent_name: str,
        hint: str,
        confidence: float,
        tool_name: str,
        arguments: dict[str, object],
        reason: str,
    ) -> ParsedIntent:
        return ParsedIntent(
            intent_type=intent_type,
            intent_name=intent_name,
            response_hint=hint,
            confidence=confidence,
            missing_slots=[],
            tool_request=ToolRequest(
                tool_name=tool_name,
                arguments=arguments,
                user_utterance=text,
                reason=reason,
            ),
        )

    def _memory_key_value(self, payload: str) -> tuple[str, str]:
        if " is " in payload:
            key, value = payload.split(" is ", 1)
            return key.replace("my ", "", 1).strip(), value.strip()
        slug = re.sub(r"[^a-z0-9]+", "_", payload.lower()).strip("_")
        return (slug[:32] or "memory_note"), payload


@dataclass(frozen=True)
class ClassifierRoute:
    tool_name: str
    intent_type: str
    intent_name: str
    response_hint: str
    examples: tuple[str, ...]
    builder_name: str


class CapabilityClassifierMatcher:
    TOKEN_PATTERN = re.compile(r"[a-z0-9_]{2,}")
    ROUTES: tuple[ClassifierRoute, ...] = (
        ClassifierRoute("search_documents", "query", "search_documents", "Search ingested documents.", ("search my documents for backups", "look in my documents for encryption", "find in my docs for contracts"), "_build_search_documents"),
        ClassifierRoute("read_audit_events", "query", "read_audit_events", "Read recent audit events.", ("show recent audit events", "show the audit trail", "audit events for backup restore"), "_build_read_audit_events"),
        ClassifierRoute("read_current_context", "query", "read_current_context", "Read the current local context.", ("show my current context", "what app am i in", "what is in my clipboard"), "_build_empty"),
        ClassifierRoute("read_runtime_snapshot", "query", "read_runtime_snapshot", "Read the runtime snapshot.", ("give me a runtime snapshot summary", "what is my active profile", "show current memory scope"), "_build_empty"),
        ClassifierRoute("list_backups", "query", "list_backups", "List available backups.", ("list available backups", "show backups", "which backup is newest"), "_build_list_backups"),
        ClassifierRoute("create_backup", "action", "create_backup", "Create an encrypted backup.", ("create an encrypted backup", "make an encrypted backup named weekend-checkpoint", "create backup"), "_build_create_backup"),
        ClassifierRoute("create_angebot", "action", "create_angebot", "Create a German offer draft.", ("create a draft angebot for acme", "make an angebot for a client", "prepare an offer draft"), "_build_create_angebot"),
        ClassifierRoute("create_rechnung", "action", "create_rechnung", "Create a German invoice draft.", ("make an invoice for acme", "create a rechnung for the client", "prepare an invoice draft"), "_build_create_rechnung"),
        ClassifierRoute("get_pending_tasks", "query", "tasks", "Read pending tasks.", ("what is still on my plate", "what do i still need to do", "show my pending tasks"), "_build_empty"),
        ClassifierRoute("get_today_calendar", "query", "calendar", "Read today's calendar.", ("what is coming up", "what is next on my agenda", "show today calendar"), "_build_empty"),
        ClassifierRoute("create_note", "action", "create_note", "Create a local note.", ("capture this for me", "write this down", "make a note of this"), "_build_create_note"),
        ClassifierRoute("remember_fact", "action", "remember_fact", "Store a durable fact.", ("keep in mind that my editor is vscode", "remember that i prefer concise answers", "make a note that you should remember this"), "_build_remember_fact"),
        ClassifierRoute("open_app", "action", "open_app", "Open a desktop app.", ("launch vscode", "fire up outlook", "open terminal"), "_build_open_app"),
        ClassifierRoute("search_files", "query", "search_files", "Search workspace files.", ("search the workspace for retention", "look through files for audit", "find files about encryption"), "_build_search_files"),
        ClassifierRoute("focus_mode", "action", "focus_mode", "Start focus mode.", ("put me in focus mode", "start a focus block", "lock me into focus"), "_build_focus_mode"),
        ClassifierRoute("browser_search", "action", "browser_search", "Search the web.", ("find on the web privacy laws", "search online for sqlite tuning", "look this up on the internet"), "_build_browser_search"),
    )

    def __init__(self) -> None:
        self._idf: dict[str, float] = {}
        self._route_vectors: dict[str, list[list[float]]] = {}
        self._vocabulary: list[str] = []
        self._prepare_index()

    def match(
        self,
        text: str,
        *,
        available_capabilities: list[str] | None = None,
        dialogue_context: dict[str, str] | None = None,
    ) -> ParsedIntent | None:
        lowered = text.lower().strip()
        operational_prefixes = (
            "search",
            "show",
            "read",
            "sync",
            "create",
            "list",
            "start",
            "stop",
            "transcribe",
            "record",
            "make",
            "capture",
            "write",
            "launch",
            "fire up",
            "boot up",
            "put",
            "find",
            "look",
            "remember",
            "keep in mind",
            "give me",
        )
        allowed = set(available_capabilities or [])
        routes = [route for route in self.ROUTES if not allowed or route.tool_name in allowed]
        if not routes:
            return None
        query_vector = self._vectorize(lowered)
        if not query_vector:
            return None
        ranked: list[tuple[float, ClassifierRoute]] = []
        for route in routes:
            vectors = self._route_vectors.get(route.tool_name, [])
            if not vectors:
                continue
            score = max(self._cosine(query_vector, vector) for vector in vectors)
            if score > 0.0:
                ranked.append((score, route))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        best_score, best_route = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        margin = best_score - second_score
        confidence = min(0.96, max(0.0, 0.28 + (best_score * 0.82) + (max(margin, 0.0) * 0.35)))
        if best_score < 0.26 or (best_score < 0.4 and margin < 0.05):
            return None
        if not any(lowered.startswith(prefix) for prefix in operational_prefixes) and best_score < 0.55:
            return None
        builder = getattr(self, best_route.builder_name)
        arguments = builder(text, lowered, dialogue_context or {})
        return ParsedIntent(
            intent_type=best_route.intent_type,
            intent_name=best_route.intent_name,
            response_hint=best_route.response_hint,
            confidence=round(confidence, 3),
            missing_slots=[],
            tool_request=ToolRequest(
                tool_name=best_route.tool_name,
                arguments=arguments,
                user_utterance=text,
                reason=f"Classifier matched {best_route.tool_name} with similarity {best_score:.3f} and margin {margin:.3f}.",
            ),
        )

    def _prepare_index(self) -> None:
        document_frequency: Counter[str] = Counter()
        phrase_tokens: dict[tuple[str, str], list[str]] = {}
        for route in self.ROUTES:
            for phrase in route.examples:
                tokens = self._tokenize(phrase)
                phrase_tokens[(route.tool_name, phrase)] = tokens
                for token in set(tokens):
                    document_frequency[token] += 1
        self._vocabulary = sorted(document_frequency.keys())
        total = max(sum(len(route.examples) for route in self.ROUTES), 1)
        self._idf = {token: math.log((1.0 + total) / (1.0 + document_frequency[token])) + 1.0 for token in self._vocabulary}
        for route in self.ROUTES:
            self._route_vectors[route.tool_name] = [self._tfidf(phrase_tokens[(route.tool_name, phrase)]) for phrase in route.examples]

    def _tokenize(self, text: str) -> list[str]:
        return [match.group(0).lower() for match in self.TOKEN_PATTERN.finditer(text)]

    def _tfidf(self, tokens: list[str]) -> list[float]:
        if not tokens or not self._vocabulary:
            return []
        counts = Counter(tokens)
        max_count = max(counts.values()) if counts else 1
        vector: list[float] = []
        for token in self._vocabulary:
            tf = counts.get(token, 0) / max_count
            vector.append(tf * self._idf.get(token, 0.0))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector

    def _vectorize(self, text: str) -> list[float]:
        return self._tfidf(self._tokenize(text))

    def _cosine(self, left: list[float], right: list[float]) -> float:
        if not left or not right:
            return 0.0
        return float(sum(a * b for a, b in zip(left, right)))

    def _strip_prefixes(self, text: str, prefixes: tuple[str, ...]) -> str:
        lowered = text.lower().strip()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return text[len(prefix):].strip(" :.?!")
        return text.strip(" :.?!")

    def _build_empty(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        return {}

    def _build_search_documents(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        return {"query": self._strip_prefixes(text, ("search my documents for", "search my documents", "look in my documents for", "look in my documents", "find in my docs for", "find in my docs"))}

    def _build_read_audit_events(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        query = self._strip_prefixes(text, ("show recent audit events", "recent audit events", "show the audit trail", "audit trail"))
        return {"query": query, "limit": 8}

    def _build_list_backups(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        return {"newest_only": any(token in lowered for token in ("newest", "latest"))}

    def _build_create_backup(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        match = re.search(r"(?:named|called|labelled|labeled)\s+([a-z0-9._-]+)", lowered)
        return {"label": match.group(1) if match else "Manual backup"}

    def _build_create_angebot(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        customer = re.split(r"angebot", text, maxsplit=1, flags=re.IGNORECASE)[-1]
        customer = re.sub(r"^\s*(for|fÃ¼r)\s+", "", customer, flags=re.IGNORECASE).strip(" .")
        return {"customer_name": customer or "Kunde"}

    def _build_create_rechnung(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        tail = re.split(r"(?:invoice|rechnung)", text, maxsplit=1, flags=re.IGNORECASE)[-1]
        customer = re.sub(r"^\s*(for|fÃ¼r)\s+", "", tail, flags=re.IGNORECASE).strip(" .")
        return {"customer_name": customer or "Kunde"}

    def _build_create_note(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        return {"content": self._strip_prefixes(text, ("capture this", "capture this for me", "write this down", "make a note of this"))}

    def _build_remember_fact(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        payload = self._strip_prefixes(text, ("keep in mind that", "make a note that you should remember", "remember that"))
        if " is " in payload:
            key, value = payload.split(" is ", 1)
            key = key.replace("my ", "", 1).strip()
            return {"key": key, "value": value.strip()}
        slug = re.sub(r"[^a-z0-9]+", "_", payload.lower()).strip("_")
        return {"key": slug[:32] or "memory_note", "value": payload}

    def _build_open_app(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        return {"app": self._strip_prefixes(text, ("launch ", "fire up ", "boot up "))}

    def _build_search_files(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        return {"query": self._strip_prefixes(text, ("search the workspace for", "look through files for", "find files about"))}

    def _build_focus_mode(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        minutes_match = re.search(r"(\d+)\s*(?:minute|min)", lowered)
        minutes = int(minutes_match.group(1)) if minutes_match else 50
        return {"minutes": minutes, "title": "Focus block"}

    def _build_browser_search(self, text: str, lowered: str, dialogue_context: dict[str, str]) -> dict[str, object]:
        return {"query": self._strip_prefixes(text, ("find on the web", "search online for", "look this up on the internet"))}


class LocalIntentFallback:
    def __init__(self, mode: str, model_path: str | None) -> None:
        self.mode = mode
        self.model_path = model_path
        self.available = False
        self._model = None
        if mode != "local" or not model_path:
            return
        try:
            from llama_cpp import Llama

            self._model = Llama(model_path=model_path, n_ctx=2048, verbose=False)
            self.available = True
        except Exception:  # pragma: no cover - optional local-model dependency
            self._model = None

    def match(self, text: str, available_capabilities: list[str]) -> ParsedIntent | None:
        if not self.available or self._model is None:
            return None
        prompt = (
            "Return compact JSON only with keys intent_type, intent_name, response_hint, confidence, "
            "tool_name, arguments, reason, missing_slots. "
            f"Allowed tools: {', '.join(sorted(available_capabilities))}. "
            f"User request: {text}"
        )
        try:
            result = self._model.create_completion(prompt=prompt, max_tokens=220, temperature=0.0)
            payload = json.loads(result["choices"][0]["text"].strip())
        except Exception:  # pragma: no cover - depends on local model behavior
            return None
        tool_name = str(payload.get("tool_name") or "").strip()
        if tool_name not in available_capabilities:
            return None
        confidence = float(payload.get("confidence") or 0.0)
        return ParsedIntent(
            intent_type=str(payload.get("intent_type") or "action"),
            intent_name=str(payload.get("intent_name") or tool_name),
            response_hint=str(payload.get("response_hint") or "Handle the operational request."),
            confidence=max(0.0, min(confidence, 1.0)),
            missing_slots=[str(item) for item in payload.get("missing_slots", []) if str(item).strip()],
            tool_request=ToolRequest(
                tool_name=tool_name,
                arguments=dict(payload.get("arguments") or {}),
                user_utterance=text,
                reason=str(payload.get("reason") or "Local intent fallback matched the request."),
            ),
        )


class HybridCognitionEngine:
    def __init__(
        self,
        rule_engine: RuleBasedIntentEngine | None = None,
        semantic_matcher: SemanticIntentMatcher | None = None,
        backend: str = "hybrid",
        model_path: str | None = None,
        intent_fallback_mode: str = "off",
        intent_fallback_min_confidence: float = 0.6,
        llm_client: "LlamaServerClient | None" = None,
        tool_calling_bridge: "ToolCallingBridge | None" = None,
    ) -> None:
        self.rule_engine = rule_engine or RuleBasedIntentEngine()
        self.semantic_matcher = semantic_matcher or SemanticIntentMatcher()
        self.classifier_matcher = CapabilityClassifierMatcher()
        self.heuristic_planner = HeuristicPlanner()
        self.local_planner = LlamaCppPlanner(model_path) if backend == "llama_cpp" else None
        self.server_planner: LlamaServerPlanner | None = None
        if llm_client is not None and tool_calling_bridge is not None:
            self.server_planner = LlamaServerPlanner(llm_client, tool_calling_bridge)
        self.intent_fallback = LocalIntentFallback(intent_fallback_mode, model_path)
        self.intent_fallback_min_confidence = intent_fallback_min_confidence
        self.backend = "llama_cpp" if self.local_planner and self.local_planner.available else backend

    def analyze(
        self,
        text: str,
        dialogue_context: dict[str, str] | None = None,
        context_summary: ActiveContextSummary | None = None,
        available_capabilities: list[str] | None = None,
    ) -> CognitionResult:
        candidates: list[IntentCandidate] = []
        rule = self.rule_engine.parse(text, dialogue_context=dialogue_context)
        candidates.append(self._candidate_from_parsed(rule, "rule"))

        classifier = None
        if available_capabilities:
            classifier = self.classifier_matcher.match(
                text,
                available_capabilities=available_capabilities,
                dialogue_context=dialogue_context,
            )
        if classifier is not None:
            candidates.append(self._candidate_from_parsed(classifier, "classifier"))

        semantic = self.semantic_matcher.match(text, dialogue_context=dialogue_context)
        if semantic is not None:
            candidates.append(self._candidate_from_parsed(semantic, "semantic"))

        parsed = rule
        selected_source = "rule"
        if semantic is not None and semantic.confidence > rule.confidence:
            parsed = semantic
            selected_source = "semantic"
        preserve_rule_memory = bool(
            rule.tool_request
            and rule.tool_request.tool_name == "remember_fact"
            and isinstance(rule.tool_request.arguments.get("facts"), list)
            and len(rule.tool_request.arguments.get("facts", [])) > 1
        )
        if (
            classifier is not None
            and not preserve_rule_memory
            and (
                parsed.tool_request is None
                or parsed.confidence < 0.7
                or classifier.confidence > parsed.confidence + 0.08
            )
            and classifier.confidence > parsed.confidence
        ):
            parsed = classifier
            selected_source = "classifier"

        plan = None
        if self.local_planner and self.local_planner.available:
            plan = self.local_planner.plan(text, context_summary, available_capabilities or [])
        if plan is None:
            plan = self.heuristic_planner.plan(
                text,
                lambda part: self.match_single(part, dialogue_context=dialogue_context),
                context_summary,
            )
        if (
            not plan.steps
            and self.looks_operational_request(text)
            and self.intent_fallback.available
            and parsed.confidence < self.intent_fallback_min_confidence
        ):
            fallback = self.intent_fallback.match(text, available_capabilities or [])
            if fallback is not None and fallback.tool_request is not None:
                candidates.append(self._candidate_from_parsed(fallback, "fallback"))
                parsed = fallback
                plan = ExecutionPlan(
                    source="fallback",
                    summary=fallback.intent_name.replace("_", " "),
                    steps=[
                        {
                            "capability_name": fallback.tool_request.tool_name,
                            "arguments": fallback.tool_request.arguments,
                            "reason": fallback.tool_request.reason,
                            "title": fallback.intent_name.replace("_", " "),
                        }
                    ],
                    confidence=fallback.confidence,
                    candidates=candidates,
                )
        plan.candidates = candidates
        if (
            classifier is not None
            and len(plan.steps) == 1
            and classifier.tool_request is not None
            and plan.steps[0].capability_name == classifier.tool_request.tool_name
            and classifier.confidence >= plan.confidence
        ):
            plan.source = "classifier"
            plan.confidence = classifier.confidence
        if not plan.summary and parsed.tool_request:
            plan.summary = parsed.intent_name.replace("_", " ")
        if plan.confidence == 0.0:
            plan.confidence = parsed.confidence
            plan.source = selected_source
        return CognitionResult(parsed_intent=parsed, candidates=candidates, execution_plan=plan)

    async def analyze_async(
        self,
        text: str,
        dialogue_context: dict[str, str] | None = None,
        context_summary: ActiveContextSummary | None = None,
        available_capabilities: list[str] | None = None,
    ) -> CognitionResult:
        """Async variant of analyze() that can use LLM-based intent resolution."""
        result = self.analyze(text, dialogue_context, context_summary, available_capabilities)
        if (
            not result.execution_plan.steps
            and self.server_planner is not None
            and self.server_planner.available
            and result.parsed_intent.confidence < self.intent_fallback_min_confidence
        ):
            llm_plan = await self.server_planner.plan_async(
                text, context_summary, available_capabilities or []
            )
            if llm_plan is not None and llm_plan.steps:
                result.execution_plan = llm_plan
                for step in llm_plan.steps:
                    result.candidates.append(IntentCandidate(
                        name=step.capability_name,
                        intent_type="action",
                        confidence=llm_plan.confidence,
                        source="planner",
                        reason=step.reason,
                        tool_name=step.capability_name,
                        arguments=step.arguments,
                    ))
        return result

    def match_single(self, text: str, dialogue_context: dict[str, str] | None = None) -> ParsedIntent:
        rule = self.rule_engine.parse(text, dialogue_context=dialogue_context)
        semantic = self.semantic_matcher.match(text, dialogue_context=dialogue_context)
        if semantic is not None and semantic.confidence > rule.confidence:
            return semantic
        return rule

    def looks_operational_request(self, text: str) -> bool:
        lowered = text.lower().strip()
        operational_verbs = {
            "search",
            "find",
            "show",
            "read",
            "sync",
            "create",
            "list",
            "start",
            "stop",
            "transcribe",
            "schedule",
            "export",
            "remember",
            "recall",
            "import",
            "ingest",
            "draft",
            "make",
            "give",
            "tell",
            "what",
            "which",
        }
        domain_terms = {
            "document",
            "documents",
            "doc",
            "docs",
            "backup",
            "audit",
            "meeting",
            "transcript",
            "profile",
            "memory scope",
            "snapshot",
            "security",
            "csv",
            "angebot",
            "rechnung",
        }
        return any(re.search(rf"\b{re.escape(token)}\b", lowered) for token in operational_verbs) and any(
            term in lowered for term in domain_terms
        )

    def _candidate_from_parsed(self, parsed: ParsedIntent, source: str) -> IntentCandidate:
        return IntentCandidate(
            name=parsed.intent_name,
            intent_type=parsed.intent_type,
            confidence=parsed.confidence,
            source=source,
            reason=parsed.tool_request.reason if source == "classifier" and parsed.tool_request else parsed.response_hint,
            tool_name=parsed.tool_request.tool_name if parsed.tool_request else None,
            arguments=parsed.tool_request.arguments if parsed.tool_request else {},
        )
