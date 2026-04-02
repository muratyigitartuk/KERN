from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.types import PersonaReply, ToolRequest

if TYPE_CHECKING:
    from app.llm_client import LlamaServerClient

logger = logging.getLogger(__name__)


KERN_SYSTEM_PROMPT = (
    "You are KERN, a local workspace assistant. "
    "Be precise, calm, direct, and operational. "
    "You work in a local-first environment and should sound like a practical office assistant, not a generic chatbot. "
    "Address the user by their preferred title. "
    "Reply in the same language the user is using unless they explicitly ask you to switch. "
    "Keep replies concise and actionable. "
    "Do not invent facts, capabilities, or access you do not actually have. "
    "If the user asks you to write or draft an email, letter, offer, or invoice, produce the draft directly unless they explicitly ask you to send it or use a tool. "
    "If asked what you can do, describe only realistic workspace capabilities such as documents, notes, reminders, schedules, drafting, summaries, and system status. "
    "Do not drift into unrelated capabilities or long marketing-style lists."
)

KERN_RAG_SYSTEM_PROMPT = (
    "You are KERN, a local workspace assistant. "
    "Be precise, calm, direct, and operational. "
    "Address the user by their preferred title. "
    "Reply in the same language the user is using unless they explicitly ask you to switch. "
    "You have been given relevant document excerpts below as CONTEXT. "
    "Answer only from that context. "
    "If the context does not contain enough information, say so directly. "
    "Use the exact source titles exactly as they appear in the context labels. "
    "Do not invent, translate, normalize, or rename source titles. "
    "When the context includes dates, amounts, IDs, or deadlines, copy those values exactly from the context instead of paraphrasing them. "
    "Every factual statement that comes from the context must cite at least one source title in square brackets, for example [Rechnung_2024_001]. "
    "If you provide multiple facts, keep the citations attached to those facts rather than only at the end. "
    "If the answer depends on multiple documents, keep each fact attached to the document it came from instead of attributing everything to one source. "
    "Do not use background knowledge to fill gaps. "
    "Keep replies concise and actionable."
)


class PersonaEngine:
    def chat_reply(self, text: str, preferred_title: str) -> PersonaReply:
        lowered = text.lower().strip()
        if re.search(r"\b(?:hello|hi|hey)\b", lowered):
            return PersonaReply(
                display_text=f"Good to hear from you, {preferred_title}. I am ready.",
                spoken_text=f"Good to hear from you, {preferred_title}. I am ready.",
            )
        if "what can you do" in lowered or "help" in lowered:
            text = (
                f"I can brief your day, remember facts, track open commitments, run routines, open apps and sites, create notes, tasks, reminders, "
                f"read your local calendar, "
                f"inspect workspace files, report system status, and handle Spotify or other media controls locally, {preferred_title}."
            )
            return PersonaReply(display_text=text, spoken_text=text)
        if "thank" in lowered:
            return PersonaReply(display_text=f"Any time, {preferred_title}.", spoken_text=f"Any time, {preferred_title}.")
        if "who are you" in lowered:
            return PersonaReply(
                display_text=f"I am your local KERN runtime, {preferred_title}.",
                spoken_text=f"I am your local KERN runtime, {preferred_title}.",
            )
        if "repeat" in lowered:
            return PersonaReply(
                display_text=f"Of course, {preferred_title}. Please say it once more and I will handle it.",
                spoken_text=f"Of course, {preferred_title}. Please say it once more and I will handle it.",
            )
        if "what do you mean" in lowered:
            text = f"I mean I am keeping the workflow local and controlled, {preferred_title}."
            return PersonaReply(display_text=text, spoken_text=text)
        if "mute" in lowered:
            return PersonaReply(
                display_text=f"I can mute spoken output and continue visually, {preferred_title}.",
                spoken_text=f"I can mute spoken output and continue visually, {preferred_title}.",
            )
        return PersonaReply(
            display_text="Understood. I can handle that locally.",
            spoken_text="Understood. I can handle that locally.",
        )

    def tool_preamble(self, request: ToolRequest, preferred_title: str) -> str:
        if request.tool_name == "play_spotify":
            mode = str(request.arguments.get("mode", "search_and_play"))
            if mode == "pause":
                return f"Pausing playback, {preferred_title}."
            if mode == "resume":
                return f"Resuming playback, {preferred_title}."
            if mode == "next":
                return f"Skipping ahead, {preferred_title}."
            if mode == "playlist":
                return f"Opening your preferred playlist, {preferred_title}."
            return f"Of course, {preferred_title}."
        if request.tool_name in {"open_app", "open_website"}:
            return f"Right away, {preferred_title}."
        if request.tool_name in {"create_reminder", "set_timer"}:
            return f"I will set that up, {preferred_title}."
        if request.tool_name == "dismiss_reminder":
            return f"I will clear that reminder, {preferred_title}."
        if request.tool_name == "snooze_reminder":
            return f"I will push that reminder back, {preferred_title}."
        if request.tool_name in {"complete_task", "list_notes"}:
            return f"Understood, {preferred_title}."
        if request.tool_name == "run_routine":
            return f"Starting that routine, {preferred_title}."
        if request.tool_name == "remember_fact":
            return f"I will remember that, {preferred_title}."
        if request.tool_name == "recall_memory":
            return f"Let me recall that, {preferred_title}."
        if request.tool_name == "focus_mode":
            return f"Locking in a focus block, {preferred_title}."
        if request.tool_name in {"search_files", "read_file_excerpt", "system_status"}:
            return f"Checking that now, {preferred_title}."
        if request.tool_name == "generate_morning_brief":
            return ""
        if request.tool_name == "set_preference":
            return ""
        return ""

    async def llm_chat_reply(
        self,
        text: str,
        preferred_title: str,
        conversation_history: list[dict[str, str]],
        llm_client: LlamaServerClient,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        model_override: str | None = None,
    ) -> PersonaReply | None:
        if not llm_client.available:
            return None
        system_msg = f"{KERN_SYSTEM_PROMPT} Address the user as '{preferred_title}'."
        messages: list[dict[str, str]] = [{"role": "system", "content": system_msg}]
        messages.extend(conversation_history[-20:])
        messages.append({"role": "user", "content": text})
        try:
            result = await llm_client.chat(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                model=model_override,
            )
            choices = result.get("choices", [])
            if not choices:
                return None
            content = choices[0].get("message", {}).get("content", "").strip()
            if not content:
                return None
            return PersonaReply(display_text=content, spoken_text=content)
        except Exception as exc:
            logger.debug("Persona LLM reply generation failed: %s", exc, exc_info=True)
            return None

    def failure_reply(self, preferred_title: str) -> str:
        return f"I hit a local problem while doing that, {preferred_title}. Would you like me to try a different approach?"
