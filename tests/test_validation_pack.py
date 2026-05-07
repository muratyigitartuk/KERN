from pathlib import Path

from app.tools.base import Tool
from app.types import ToolRequest, ToolResult
from app.validation_pack import (
    VALIDATION_ADMIN_TOKEN,
    ValidationPackError,
    _extract_markdown_link_paths,
    _http_post_upload,
    _manual_review_items,
    _runtime_env,
)


class _FakeTool(Tool):
    name = "fake"

    async def run(self, request: ToolRequest) -> ToolResult:
        return ToolResult(display_text="ok")


def test_extract_markdown_link_paths_returns_cli_artifacts() -> None:
    paths = _extract_markdown_link_paths("### Snapshot\n- [Snapshot](.playwright-cli\\page.yml)\n- [Screenshot](.playwright-cli\\shot.png)")
    assert [str(path) for path in paths] == [".playwright-cli\\page.yml", ".playwright-cli\\shot.png"]


def test_runtime_env_uses_isolated_roots_and_seed_defaults(tmp_path: Path) -> None:
    env = _runtime_env(tmp_path / "validation-root", "corporate", product_posture="personal")
    assert env["KERN_POLICY_MODE"] == "corporate"
    assert env["KERN_PRODUCT_POSTURE"] == "personal"
    assert env["KERN_SEED_DEFAULTS"] == "true"
    assert env["KERN_SYSTEM_DB_PATH"].endswith("validation-root\\kern-system.db")
    assert env["KERN_DB_PATH"].endswith("validation-root\\kern.db")
    assert env["KERN_ROOT_PATH"].endswith("validation-root")
    assert env["KERN_PROFILE_ROOT"].endswith("validation-root\\profiles")


def test_manual_review_items_are_present() -> None:
    items = _manual_review_items()
    assert len(items) >= 3
    assert any("screenshots" in item.lower() for item in items)


def test_dashboard_renderer_exposes_validation_testids() -> None:
    source = Path("app/static/js/dashboard-renderer.js").read_text(encoding="utf-8")
    assert 'conversation-search-result' in source
    assert 'schedule-row' in source
    assert 'proactive-alert-row' in source
    assert 'kg-result-row' in source
    assert 'memory-result-row' in source


def test_rollout_dashboard_defaults_to_production_posture() -> None:
    source = Path("app/static/dashboard.html").read_text(encoding="utf-8")
    assert 'data-product-posture="production"' in source
    assert "Play some morning jazz" not in source
    assert "Upload documents, ask grounded questions, and inspect evidence before drafting." in source
    # Tab structure was reorganized to workspace/admin/compliance/intelligence/evidence
    assert 'data-tab="workspace"' in source
    assert 'data-tab="admin"' in source
    assert 'data-tab="compliance"' in source
    assert 'data-tab="evidence"' in source
    assert 'id="composerTranscribeAction"' not in source


def test_http_post_upload_bootstraps_csrf_header(monkeypatch, tmp_path: Path) -> None:
    upload_file = tmp_path / "fixture.txt"
    upload_file.write_text("fixture", encoding="utf-8")
    calls: list[tuple[str, str, dict[str, str] | None]] = []

    class _FakeResponse:
        def __init__(self, status_code: int = 200, payload: dict[str, object] | None = None) -> None:
            self.status_code = status_code
            self._payload = payload or {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.cookies: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
            calls.append(("GET", url, headers))
            self.cookies["kern_csrf_token"] = "csrf-token"
            return _FakeResponse(status_code=200)

        def post(self, url: str, *, files, headers: dict[str, str] | None = None) -> _FakeResponse:
            calls.append(("POST", url, headers))
            return _FakeResponse(payload={"indexed": len(files)})

    monkeypatch.setattr("app.validation_pack.httpx.Client", _FakeClient)
    monkeypatch.setenv("KERN_ADMIN_AUTH_TOKEN", "test-token")

    payload = _http_post_upload("http://127.0.0.1:8123", [upload_file])

    assert payload["indexed"] == 1
    expected_auth = {"Authorization": f"Bearer {VALIDATION_ADMIN_TOKEN}"}
    assert calls[0] == ("GET", "http://127.0.0.1:8123/health", expected_auth)
    assert calls[1] == (
        "POST",
        "http://127.0.0.1:8123/upload",
        {"x-csrf-token": "csrf-token", **expected_auth},
    )


def test_http_post_upload_raises_when_csrf_cookie_missing(monkeypatch, tmp_path: Path) -> None:
    upload_file = tmp_path / "fixture.txt"
    upload_file.write_text("fixture", encoding="utf-8")

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"indexed": 0}

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.cookies: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
            return _FakeResponse()

        def post(self, url: str, *, files, headers: dict[str, str] | None = None) -> _FakeResponse:
            raise AssertionError("Upload POST should not be attempted without a CSRF token.")

    monkeypatch.setattr("app.validation_pack.httpx.Client", _FakeClient)
    monkeypatch.setenv("KERN_ADMIN_AUTH_TOKEN", "test-token")

    try:
        _http_post_upload("http://127.0.0.1:8123", [upload_file])
    except ValidationPackError as exc:
        assert "CSRF token" in str(exc)
    else:
        raise AssertionError("Expected ValidationPackError when CSRF bootstrap fails.")
