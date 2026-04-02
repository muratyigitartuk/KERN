from __future__ import annotations

from typing import Any

from app.capabilities import CapabilityRegistry
from app.types import ToolRequest, ToolResult


class ToolCallingBridge:
    """Bridges KERN capabilities to Mistral function calling format."""

    def __init__(self, capability_registry: CapabilityRegistry) -> None:
        self.registry = capability_registry
        self._schema_cache: list[dict[str, Any]] | None = None

    def build_tool_schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for descriptor in self.registry.available_descriptors():
            if not descriptor.available:
                continue
            capability = self.registry.get_capability(descriptor.name)
            if capability is None:
                continue
            param_schema = capability.tool.parameter_schema()
            if not param_schema.get("properties"):
                continue
            schemas.append({
                "type": "function",
                "function": {
                    "name": descriptor.name,
                    "description": descriptor.summary,
                    "parameters": param_schema,
                },
            })
        return schemas

    def invalidate_cache(self) -> None:
        self._schema_cache = None

    def parse_tool_calls(self, response: dict[str, Any]) -> list[ToolRequest]:
        choices = response.get("choices", [])
        if not choices:
            return []
        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return []
        requests: list[ToolRequest] = []
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "")
            capability = self.registry.get_capability(name)
            if capability is None:
                continue
            try:
                import json
                arguments = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            validation_error = capability.tool.validate_arguments(arguments)
            if validation_error:
                continue
            requests.append(ToolRequest(
                tool_name=name,
                arguments=arguments,
                user_utterance="",
                reason=f"LLM tool call: {name}",
            ))
        return requests

    def build_tool_result_message(
        self,
        tool_call_id: str,
        name: str,
        result: ToolResult,
    ) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": result.display_text or str(result.data),
        }

    def extract_text_response(self, response: dict[str, Any]) -> str | None:
        choices = response.get("choices", [])
        if not choices:
            return None
        message = choices[0].get("message", {})
        if message.get("tool_calls"):
            return None
        content = message.get("content", "")
        return content.strip() if content else None
