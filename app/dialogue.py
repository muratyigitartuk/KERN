from __future__ import annotations

import json

from app.memory import MemoryRepository


class DialogueStateStore:
    def __init__(self, memory: MemoryRepository) -> None:
        self.memory = memory

    def set_last_response(self, text: str) -> None:
        self.memory.set_value("dialogue_state", "last_response", text)

    def get_last_response(self) -> str | None:
        return self.memory.get_value("dialogue_state", "last_response")

    def set_last_announced_reminder_id(self, reminder_id: int | None) -> None:
        self.memory.set_value("dialogue_state", "last_announced_reminder_id", str(reminder_id or 0))

    def get_last_announced_reminder_id(self) -> int | None:
        raw = self.memory.get_value("dialogue_state", "last_announced_reminder_id")
        if not raw:
            return None
        try:
            value = int(raw)
        except (ValueError, TypeError):
            return None
        return value if value > 0 else None

    def set_last_listed_reminder_ids(self, reminder_ids: list[int]) -> None:
        self.memory.set_value("dialogue_state", "last_listed_reminder_ids", json.dumps(reminder_ids))

    def get_last_listed_reminder_ids(self) -> list[int]:
        raw = self.memory.get_value("dialogue_state", "last_listed_reminder_ids", "[]") or "[]"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        result = []
        for item in data:
            try:
                val = int(item)
                if val > 0:
                    result.append(val)
            except (ValueError, TypeError):
                continue
        return result

    def set_last_listed_task_titles(self, titles: list[str]) -> None:
        self.memory.set_value("dialogue_state", "last_listed_task_titles", json.dumps(titles))

    def get_last_listed_task_titles(self) -> list[str]:
        raw = self.memory.get_value("dialogue_state", "last_listed_task_titles", "[]") or "[]"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in data if str(item).strip()]
