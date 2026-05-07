from __future__ import annotations

import json

from fastapi import WebSocket


async def server_websocket_endpoint(websocket: WebSocket, runtime, *, thread_id: str) -> None:
    auth_context = getattr(websocket.state, "auth_context", None)
    if auth_context is None or not auth_context.user_id or not auth_context.organization_id:
        await websocket.close(code=1008, reason="Authenticated user session required.")
        return
    thread = runtime.platform.get_thread_for_user(
        thread_id,
        user_id=auth_context.user_id,
        organization_id=auth_context.organization_id,
    )
    if thread is None:
        await websocket.close(code=1008, reason="Thread not found.")
        return
    await websocket.accept()
    messages = runtime.platform.list_messages_for_user(
        thread_id=thread.id,
        user_id=auth_context.user_id,
        organization_id=auth_context.organization_id,
    )
    await websocket.send_json(
        {
            "type": "thread_snapshot",
            "thread": thread.model_dump(mode="json"),
            "messages": [message.model_dump(mode="json") for message in messages],
        }
    )
    while True:
        raw = await websocket.receive_text()
        payload = json.loads(raw)
        command_type = str(payload.get("type") or "")
        if command_type != "submit_text":
            await websocket.send_json({"type": "error", "detail": "Unsupported server-mode command."})
            continue
        text = str(payload.get("text") or "").strip()
        if not text:
            continue
        message = runtime.platform.append_message(
            thread_id=thread.id,
            actor_user_id=auth_context.user_id,
            acting_user_id=auth_context.user_id,
            organization_id=auth_context.organization_id,
            role="user",
            content=text,
            metadata={"transport": "websocket"},
        )
        await websocket.send_json({"type": "message", "message": message.model_dump(mode="json")})
