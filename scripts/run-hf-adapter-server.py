from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import torch
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer


def _load_quantization_config(*, load_in_4bit: bool):
    if not load_in_4bit:
        return None
    from transformers import BitsAndBytesConfig

    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    )


def _resolve_device_map(device_map: str | None) -> Any:
    if device_map is None:
        return None
    normalized = device_map.strip().lower()
    if normalized in {"", "none"}:
        return None
    if normalized == "cpu":
        return {"": "cpu"}
    if normalized == "cuda":
        return {"": "cuda:0"}
    return device_map


def _build_app(
    *,
    model_name: str,
    adapter_path: str | None,
    trust_remote_code: bool,
    load_in_4bit: bool,
    alias: str | None,
    device_map: str | None,
) -> FastAPI:
    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=trust_remote_code)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"

    model_kwargs: dict[str, Any] = {
        "trust_remote_code": trust_remote_code,
    }
    resolved_device_map = _resolve_device_map(device_map)
    if resolved_device_map is not None:
        model_kwargs["device_map"] = resolved_device_map
    quantization_config = _load_quantization_config(load_in_4bit=load_in_4bit)
    if quantization_config is not None:
        model_kwargs["quantization_config"] = quantization_config

    model = AutoModelForCausalLM.from_pretrained(model_name, **model_kwargs)
    if adapter_path:
        model = PeftModel.from_pretrained(model, adapter_path)
    model.eval()

    app = FastAPI(title="KERN HF Adapter Server")

    def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for message in messages:
            normalized.append(
                {
                    "role": str(message.get("role", "user")),
                    "content": str(message.get("content", "")),
                }
            )
        return normalized

    def _build_prompt(messages: list[dict[str, Any]]) -> str:
        return tokenizer.apply_chat_template(
            _normalize_messages(messages),
            tokenize=False,
            add_generation_prompt=True,
        )

    def _generate_text(
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        prompt = _build_prompt(messages)
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        do_sample = temperature > 0.0
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                do_sample=do_sample,
                temperature=temperature if do_sample else None,
                top_p=0.95 if do_sample else None,
                pad_token_id=tokenizer.pad_token_id,
                eos_token_id=tokenizer.eos_token_id,
            )
        generated = outputs[0][inputs["input_ids"].shape[1] :]
        return tokenizer.decode(generated, skip_special_tokens=True).strip()

    def _completion_payload(content: str) -> dict[str, Any]:
        now = int(time.time())
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": now,
            "model": alias or model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }

    async def _stream_response(content: str) -> AsyncIterator[bytes]:
        now = int(time.time())
        first = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": now,
            "model": alias or model_name,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode("utf-8")
        chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": now,
            "model": alias or model_name,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
        final = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": now,
            "model": alias or model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        await asyncio.sleep(0)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "model": alias or model_name})

    @app.get("/models")
    @app.get("/v1/models")
    async def models() -> JSONResponse:
        model_id = alias or model_name
        return JSONResponse({"data": [{"id": model_id, "object": "model"}], "models": [{"name": model_id, "model": model_id}]})

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        payload = await request.json()
        messages = list(payload.get("messages", []))
        max_tokens = int(payload.get("max_tokens") or 256)
        temperature = float(payload.get("temperature") or 0.0)
        stream = bool(payload.get("stream"))
        content = await asyncio.to_thread(
            _generate_text,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if stream:
            return StreamingResponse(_stream_response(content), media_type="text/event-stream")
        return JSONResponse(_completion_payload(content))

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an OpenAI-compatible server for a HF model plus optional LoRA adapter.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--adapter", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--alias", default=None)
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--load-in-4bit", action="store_true")
    parser.add_argument(
        "--device-map",
        default="auto",
        help="Transformers device_map. Examples: auto, cpu, cuda, sequential, balanced, none.",
    )
    args = parser.parse_args()

    app = _build_app(
        model_name=args.model,
        adapter_path=args.adapter,
        trust_remote_code=args.trust_remote_code,
        load_in_4bit=args.load_in_4bit,
        alias=args.alias,
        device_map=args.device_map,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
