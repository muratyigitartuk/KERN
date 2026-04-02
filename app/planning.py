from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from app.intent import ParsedIntent
from app.types import ActiveContextSummary, ExecutionPlan, PlanStep

if TYPE_CHECKING:
    from app.llm_client import LlamaServerClient
    from app.tool_calling import ToolCallingBridge

logger = logging.getLogger(__name__)


class HeuristicPlanner:
    def __init__(self) -> None:
        self._split_pattern = re.compile(r"\b(?:and then|then| and )\b", re.IGNORECASE)

    def plan(
        self,
        text: str,
        parse_single: Callable[[str], ParsedIntent],
        context: ActiveContextSummary | None = None,
    ) -> ExecutionPlan:
        full_parse = parse_single(text)
        if full_parse.tool_request and self._preserve_single_intent(text, full_parse):
            return self._single_step_plan(full_parse, source="rule")
        parts = [segment.strip(" ,.") for segment in self._split_pattern.split(text) if segment.strip(" ,.")]
        steps: list[PlanStep] = []
        summary_bits: list[str] = []

        if "after the meeting" in text.lower():
            reminder_step = self._resolve_after_meeting_reminder(text, context)
            if reminder_step is not None:
                steps.append(reminder_step)
                summary_bits.append("schedule a reminder after the next meeting")
                return ExecutionPlan(
                    source="planner",
                    summary="Then ".join(summary_bits).capitalize(),
                    steps=steps,
                    confidence=0.76,
                )

        for part in parts or [text]:
            parsed = parse_single(part)
            if not parsed.tool_request:
                continue
            steps.append(
                PlanStep(
                    capability_name=parsed.tool_request.tool_name,
                    arguments=parsed.tool_request.arguments,
                    reason=parsed.tool_request.reason,
                    title=parsed.intent_name.replace("_", " "),
                )
            )
            summary_bits.append(parsed.intent_name.replace("_", " "))

        source = "planner" if len(steps) > 1 else "rule"
        confidence = 0.84 if len(steps) > 1 else 0.72 if steps else 0.0
        if len(steps) == 1 and full_parse.tool_request and len(parts) > 1:
            return self._single_step_plan(full_parse, source="rule")
        if not steps and full_parse.tool_request:
            return self._single_step_plan(full_parse, source="rule")
        return ExecutionPlan(
            source=source,
            summary=", then ".join(summary_bits) if summary_bits else "No executable plan.",
            steps=steps,
            confidence=confidence,
        )

    def _preserve_single_intent(self, text: str, parsed: ParsedIntent) -> bool:
        if parsed.tool_request is None:
            return False
        if parsed.tool_request.tool_name == "remember_fact":
            return True
        lowered = text.lower()
        if " and " not in lowered and " then " not in lowered and "and then" not in lowered:
            return True
        return False

    def _single_step_plan(self, parsed: ParsedIntent, *, source: str) -> ExecutionPlan:
        if parsed.tool_request is None:
            raise RuntimeError("Planner expected a tool request but none was produced.")
        return ExecutionPlan(
            source=source,
            summary=parsed.intent_name.replace("_", " "),
            steps=[
                PlanStep(
                    capability_name=parsed.tool_request.tool_name,
                    arguments=parsed.tool_request.arguments,
                    reason=parsed.tool_request.reason,
                    title=parsed.intent_name.replace("_", " "),
                )
            ],
            confidence=parsed.confidence,
        )

    def _resolve_after_meeting_reminder(
        self,
        text: str,
        context: ActiveContextSummary | None,
    ) -> PlanStep | None:
        if context is None or not context.events:
            return None
        next_event = context.events[0]
        if not next_event.starts_at:
            return None
        due_at = next_event.ends_at or (next_event.starts_at + timedelta(hours=1))
        title = text.lower().replace("remind me to", "").replace("after the meeting", "").strip(" ,.?!") or "follow up"
        return PlanStep(
            capability_name="create_reminder",
            arguments={"title": title, "due_at": due_at.isoformat(), "kind": "reminder"},
            reason="The user asked for a reminder after the next meeting.",
            title="after meeting reminder",
        )


class LlamaCppPlanner:
    def __init__(self, model_path: str | None) -> None:
        self.model_path = Path(model_path).expanduser().resolve() if model_path else None
        self.available = False
        self._model = None
        if not self.model_path or not self.model_path.exists():
            return
        try:
            from llama_cpp import Llama

            self._model = Llama(model_path=str(self.model_path), n_ctx=2048, verbose=False)
            self.available = True
        except Exception:  # pragma: no cover - optional dependency path
            self._model = None

    def plan(
        self,
        text: str,
        context: ActiveContextSummary | None,
        capability_names: list[str],
    ) -> ExecutionPlan | None:
        if not self.available or self._model is None:
            return None
        context_lines = context.summary_lines if context else []
        prompt = (
            "You are a local planner. Return only compact JSON with keys summary and steps. "
            "Each step needs capability_name, arguments, and reason. "
            f"Capabilities: {', '.join(capability_names)}. "
            f"Context: {' | '.join(context_lines)}. "
            f"User: {text}"
        )
        try:
            result = self._model.create_completion(prompt=prompt, max_tokens=220, temperature=0.1)
            payload = result["choices"][0]["text"].strip()
            data = json.loads(payload)
            steps = [PlanStep.model_validate(item) for item in data.get("steps", [])]
            if not steps:
                return None
            return ExecutionPlan(
                source="planner",
                summary=str(data.get("summary", "Local planner generated a plan.")),
                steps=steps,
                confidence=0.7,
            )
        except Exception:  # pragma: no cover - depends on local model behavior
            return None


class LlamaServerPlanner:
    """Plans execution via llama-server function calling."""

    def __init__(
        self,
        llm_client: "LlamaServerClient",
        tool_bridge: "ToolCallingBridge",
    ) -> None:
        self.llm_client = llm_client
        self.tool_bridge = tool_bridge

    @property
    def available(self) -> bool:
        return self.llm_client.available

    async def plan_async(
        self,
        text: str,
        context: ActiveContextSummary | None,
        capability_names: list[str],
    ) -> ExecutionPlan | None:
        if not self.available:
            return None
        tools = self.tool_bridge.build_tool_schemas()
        if not tools:
            return None
        context_lines = context.summary_lines if context else []
        system_msg = (
            "You are KERN, a local AI planner. Decide which tools to call based on the user request. "
            "Only call tools that are directly relevant. If no tool fits, reply with text only."
        )
        if context_lines:
            system_msg += f" Context: {' | '.join(context_lines[:8])}"
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": text},
        ]
        try:
            response = await self.llm_client.chat_with_tools(
                messages, tools, temperature=0.1, max_tokens=512
            )
            tool_requests = self.tool_bridge.parse_tool_calls(response)
            if not tool_requests:
                return None
            steps = []
            for req in tool_requests:
                if req.tool_name not in capability_names and capability_names:
                    continue
                steps.append(PlanStep(
                    capability_name=req.tool_name,
                    arguments=req.arguments,
                    reason=req.reason,
                    title=req.tool_name.replace("_", " "),
                ))
            if not steps:
                return None
            return ExecutionPlan(
                source="planner",
                summary=", then ".join(s.title or s.capability_name for s in steps),
                steps=steps,
                confidence=0.8,
            )
        except Exception as exc:
            logger.debug("LLM-based planning failed: %s", exc, exc_info=True)
            return None
