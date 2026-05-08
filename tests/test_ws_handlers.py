from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.ws_handlers import _is_websocket_transport_error, _safe_send_json, websocket_endpoint


class _FakeSnapshot:
    def __init__(self) -> None:
        self.response_text = "stable"
        self.last_action = "ok"

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return {"response_text": self.response_text, "last_action": self.last_action}


class _FakePlatform:
    def __init__(self) -> None:
        self.audit_calls: list[tuple] = []

    def record_audit(self, *args, **kwargs) -> None:
        self.audit_calls.append((args, kwargs))


class _FakeWebSocket:
    def __init__(self, send_exc: Exception | None = None) -> None:
        self._send_exc = send_exc
        self.accepted = False
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers = {}
        self.query_params = {}
        self.state = SimpleNamespace(workspace_context=SimpleNamespace(roles=[]))

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict[str, object]) -> None:
        if self._send_exc is not None:
            raise self._send_exc


class _FakeRuntime:
    def __init__(self) -> None:
        self._ws_connection_count = 0
        self.orchestrator = SimpleNamespace(snapshot=_FakeSnapshot())
        self.platform = _FakePlatform()
        self.active_profile = SimpleNamespace(slug="default")

    async def _refresh_platform_snapshot(self) -> None:
        return None


def test_is_websocket_transport_error_recognizes_send_race():
    exc = RuntimeError('WebSocket is not connected. Need to call "accept" first.')
    assert _is_websocket_transport_error(exc) is True


def test_safe_send_json_returns_false_for_transport_error():
    websocket = _FakeWebSocket(RuntimeError('WebSocket is not connected. Need to call "accept" first.'))

    assert asyncio.run(_safe_send_json(websocket, {"type": "snapshot"})) is False


def test_websocket_endpoint_does_not_poison_snapshot_on_transport_error():
    websocket = _FakeWebSocket(RuntimeError('WebSocket is not connected. Need to call "accept" first.'))
    runtime = _FakeRuntime()

    asyncio.run(websocket_endpoint(websocket, runtime, workspace_checked=True))

    assert websocket.accepted is True
    assert runtime.orchestrator.snapshot.response_text == "stable"
    assert runtime.orchestrator.snapshot.last_action == "ok"
    assert runtime.platform.audit_calls == []
