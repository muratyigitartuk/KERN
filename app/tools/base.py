from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.types import ToolRequest, ToolResult


class Tool(ABC):
    name: str

    @abstractmethod
    async def run(self, request: ToolRequest) -> ToolResult:
        raise NotImplementedError

    def availability(self) -> tuple[bool, str | None]:
        return True, None

    def parameter_schema(self) -> dict[str, Any]:
        """JSON Schema for this tool's arguments. Override in subclass."""
        return {"type": "object", "properties": {}}

    def validate_arguments(self, arguments: dict[str, Any]) -> str | None:
        schema = self.parameter_schema() or {}
        if schema.get("type") != "object":
            return None
        if not isinstance(arguments, dict):
            return "Tool arguments must be an object."
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        if not properties and not required:
            return None
        for key in required:
            if key not in arguments:
                return f"Missing required argument: {key}."
        for key, value in arguments.items():
            spec = properties.get(key)
            if spec is None:
                return f"Unexpected argument: {key}."
            error = self._validate_value(value, spec, field=key)
            if error:
                return error
        return None

    def timeout_seconds(self) -> float | None:
        return 12.0

    def _validate_value(self, value: Any, spec: dict[str, Any], *, field: str) -> str | None:
        expected_type = spec.get("type")
        if expected_type == "string":
            if not isinstance(value, str):
                return f"Argument {field} must be a string."
            enum = spec.get("enum")
            if enum and value not in enum:
                return f"Argument {field} must be one of: {', '.join(str(item) for item in enum)}."
        elif expected_type == "boolean":
            if not isinstance(value, bool):
                return f"Argument {field} must be a boolean."
        elif expected_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"Argument {field} must be an integer."
        elif expected_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"Argument {field} must be numeric."
        elif expected_type == "array":
            if not isinstance(value, list):
                return f"Argument {field} must be an array."
            item_spec = spec.get("items") or {}
            for index, item in enumerate(value):
                error = self._validate_value(item, item_spec, field=f"{field}[{index}]")
                if error:
                    return error
        elif expected_type == "object":
            if not isinstance(value, dict):
                return f"Argument {field} must be an object."
        return None
