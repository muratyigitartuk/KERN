import asyncio

from app.tools.system import OpenAppTool, OpenWebsiteTool
from app.types import ToolRequest


def test_open_app_reports_attempt_not_confirmation(monkeypatch):
    launched = []

    class DummyProcess:
        pass

    def fake_popen(args, shell):
        launched.append(args)
        return DummyProcess()

    async def run_tool():
        tool = OpenAppTool()
        request = ToolRequest(
            tool_name="open_app",
            arguments={"app": "notepad"},
            user_utterance="open notepad",
            reason="test",
        )
        return await tool.run(request)

    monkeypatch.setattr("app.tools.system.subprocess.Popen", fake_popen)
    result = asyncio.run(run_tool())

    assert launched
    assert result.success is True
    assert result.data["attempted"] is True
    assert result.status == "attempted"
    assert "launch request" in result.display_text.lower()


def test_open_website_reports_attempt_not_confirmation(monkeypatch):
    def fake_open(_url):
        return True

    async def run_tool():
        tool = OpenWebsiteTool()
        request = ToolRequest(
            tool_name="open_website",
            arguments={"url": "example.com"},
            user_utterance="open example.com",
            reason="test",
        )
        return await tool.run(request)

    monkeypatch.setattr("app.tools.system.webbrowser.open", fake_open)
    result = asyncio.run(run_tool())

    assert result.success is True
    assert result.data["attempted"] is True
    assert result.status == "attempted"
    assert "browser open request" in result.display_text.lower()
