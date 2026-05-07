from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.types import PersonaReply, ToolRequest

if TYPE_CHECKING:
    from app.llm_client import LlamaServerClient

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROMPT_ROOT = _PROJECT_ROOT / "prompts"


def _load_prompt(name: str) -> str:
    path = _PROMPT_ROOT / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"KERN prompt file is missing or unreadable: {path}") from exc


KERN_SYSTEM_PROMPT = _load_prompt("system.md")
KERN_RAG_SYSTEM_PROMPT = _load_prompt("rag-system.md")


class PersonaEngine:
    def _address_suffix(self, preferred_title: str) -> str:
        title = preferred_title.strip()
        if not title or title.lower() in {"sir", "boss", "master", "chief", "commander"}:
            return ""
        return f", {title}"

    def chat_reply(self, text: str, preferred_title: str) -> PersonaReply:
        lowered = text.lower().strip()
        suffix = self._address_suffix(preferred_title)
        if re.search(r"\b(?:hello|hi|hey)\b", lowered):
            return PersonaReply(
                display_text=f"Good to hear from you{suffix}. I am ready.",
            )
        if "what can you do" in lowered or "help" in lowered:
            text = (
                f"I can brief your day, remember facts, track open commitments, run routines, open apps and sites, create notes, tasks, reminders, "
                f"read your local calendar, "
                f"inspect workspace files, report system status, and handle local workspace controls."
            )
            return PersonaReply(display_text=text)
        if "thank" in lowered:
            return PersonaReply(display_text="Any time.")
        if "who are you" in lowered:
            return PersonaReply(
                display_text="I am the local KERN runtime for private workspace assistance.",
            )
        if "repeat" in lowered:
            return PersonaReply(
                display_text="Of course. Please say it once more and I will handle it.",
            )
        if "what do you mean" in lowered:
            text = "I mean I am keeping the workflow local, controlled, and explicit."
            return PersonaReply(display_text=text)
        if "mute" in lowered:
            return PersonaReply(
                display_text="Audio output is not part of this product surface. I will continue visually.",
            )
        text = "I could not parse that as a request. Send a complete question or instruction."
        return PersonaReply(display_text=text)

    def tool_preamble(self, request: ToolRequest, preferred_title: str) -> str:
        if request.tool_name in {"open_app", "open_website"}:
            return "Right away."
        if request.tool_name in {"create_reminder", "set_timer"}:
            return "I will set that up."
        if request.tool_name == "dismiss_reminder":
            return "I will clear that reminder."
        if request.tool_name == "snooze_reminder":
            return "I will push that reminder back."
        if request.tool_name in {"complete_task", "list_notes"}:
            return "Done."
        if request.tool_name == "run_routine":
            return "Starting that routine."
        if request.tool_name == "remember_fact":
            return "I will remember that."
        if request.tool_name == "recall_memory":
            return "Let me recall that."
        if request.tool_name == "focus_mode":
            return "Locking in a focus block."
        if request.tool_name in {"search_files", "read_file_excerpt", "system_status"}:
            return "Checking that now."
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
        system_msg = KERN_SYSTEM_PROMPT
        suffix = self._address_suffix(preferred_title)
        if suffix:
            system_msg = f"{system_msg} The user's preferred form of address is '{preferred_title.strip()}'; use it sparingly only when natural."
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
            return PersonaReply(display_text=content)
        except Exception as exc:
            logger.debug("Persona LLM reply generation failed: %s", exc, exc_info=True)
            return None

    def failure_reply(self, preferred_title: str) -> str:
        return "I hit a local problem while doing that. Would you like me to try a different approach?"
