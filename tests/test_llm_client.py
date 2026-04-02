from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.llm_client import LlamaServerClient


def test_build_body_uses_default_model_when_missing():
    client = LlamaServerClient("http://127.0.0.1:8080", default_model="eurollm9b")

    body = client._build_body([{"role": "user", "content": "hi"}], stream=False, max_tokens=32)

    assert body["model"] == "eurollm9b"
    assert body["max_tokens"] == 32


def test_build_body_preserves_explicit_model():
    client = LlamaServerClient("http://127.0.0.1:8080", default_model="eurollm9b")

    body = client._build_body(
        [{"role": "user", "content": "hi"}],
        stream=False,
        model="override-model",
        temperature=0.2,
    )

    assert body["model"] == "override-model"
    assert body["temperature"] == 0.2


@dataclass
class _FakeResponse:
    status_code: int


class _FakeSession:
    def __init__(self, statuses: dict[str, int]) -> None:
        self.statuses = statuses

    async def get(self, path: str) -> _FakeResponse:
        return _FakeResponse(self.statuses[path])


def test_health_accepts_llama_server_health_endpoint():
    client = LlamaServerClient("http://127.0.0.1:8080")
    client._session = _FakeSession({"/health": 200, "/models": 404})

    assert asyncio.run(client.health()) is True


def test_health_accepts_models_fallback():
    client = LlamaServerClient("http://127.0.0.1:8080")
    client._session = _FakeSession({"/health": 500, "/models": 404})

    assert asyncio.run(client.health()) is False
