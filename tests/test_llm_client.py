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
        return _FakeResponse(self.statuses.get(path, 404))


def test_health_accepts_llama_server_health_endpoint():
    client = LlamaServerClient("http://127.0.0.1:8080")
    client._session = _FakeSession({"/health": 200, "/models": 404})

    assert asyncio.run(client.health()) is True


def test_health_accepts_models_fallback():
    client = LlamaServerClient("http://127.0.0.1:8080")
    client._session = _FakeSession({"/health": 500, "/v1/models": 200, "/models": 404})

    assert asyncio.run(client.health()) is True


def test_health_rejects_when_all_probe_endpoints_fail():
    client = LlamaServerClient("http://127.0.0.1:8080")
    client._session = _FakeSession({"/health": 500, "/v1/models": 404, "/models": 404})

    assert asyncio.run(client.health()) is False


def test_render_completion_prompt_uses_llama_turn_format():
    client = LlamaServerClient("http://127.0.0.1:8080")

    prompt = client._render_completion_prompt(
        [
            {"role": "system", "content": "Du bist KERN."},
            {"role": "user", "content": "Nenne den Preis."},
            {"role": "assistant", "content": "Welchen Preis?"},
            {"role": "user", "content": "48.000 EUR."},
        ]
    )

    assert prompt.startswith("<bos><|turn|>system\nDu bist KERN.<turn|>")
    assert "<|turn|>model\nWelchen Preis?<turn|>" in prompt
    assert prompt.endswith("<|turn|>model\n")


def test_empty_or_none_chat_content_is_unusable():
    client = LlamaServerClient("http://127.0.0.1:8080")

    assert client._has_usable_chat_content({"choices": [{"message": {"content": ""}}]}) is False
    assert client._has_usable_chat_content({"choices": [{"message": {"content": "None"}}]}) is False
    assert client._has_usable_chat_content({"choices": [{"message": {"content": "bereit"}}]}) is True
