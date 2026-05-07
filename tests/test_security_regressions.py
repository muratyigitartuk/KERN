from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import StrictHostMiddleware, is_allowed_origin
from app.csrf import CSRFMiddleware


def test_public_csrf_origin_check_uses_configured_allowed_origins() -> None:
    assert is_allowed_origin("https://localhost:8000") is True
    assert is_allowed_origin("https://localhost.evil.example") is False
    assert is_allowed_origin("javascript:alert(1)") is False


def test_strict_host_middleware_rejects_dns_rebinding_host() -> None:
    app = FastAPI()
    app.add_middleware(StrictHostMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    response = TestClient(app).get("/health", headers={"Host": "attacker.example"})

    assert response.status_code == 400


def test_public_csrf_rejects_hostile_origin_independent_of_host() -> None:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/auth/break-glass/login")
    async def break_glass_login():
        return {"status": "ok"}

    response = TestClient(app).post(
        "/auth/break-glass/login",
        headers={"Host": "attacker.example", "Origin": "https://attacker.example"},
    )

    assert response.status_code == 403
