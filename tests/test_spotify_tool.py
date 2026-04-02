import asyncio

from app.tools.spotify import SpotifyTool
from app.types import ToolRequest
from app.windows_media import MediaSessionCommandResult, MediaSessionSnapshot


class FakeSessionClient:
    def __init__(self, sessions=None, controls=None):
        self.sessions = list(sessions or [])
        self.controls = list(controls or [])
        self.control_calls: list[str] = []

    def spotify_session(self):
        if self.sessions:
            return self.sessions.pop(0)
        return None

    def control(self, action: str):
        self.control_calls.append(action)
        if self.controls:
            return self.controls.pop(0)
        return None

    def wait_for_session(self, predicate=None, timeout_seconds=3.0, poll_interval_seconds=0.35):
        candidate = self.spotify_session()
        if candidate is None:
            return None
        if predicate is None or predicate(candidate):
            return candidate
        return candidate


def test_spotify_tool_reports_observed_playlist_playback(monkeypatch):
    launches = []

    def fake_popen(args, shell):
        launches.append(args)
        return object()

    async def fake_sleep(_seconds):
        return None

    async def run_tool():
        tool = SpotifyTool(
            session_client=FakeSessionClient(
                sessions=[
                    None,
                    MediaSessionSnapshot(
                        source_app="Spotify",
                        status="Playing",
                        title="Blue in Green",
                        artist="Miles Davis",
                        album="Kind of Blue",
                    ),
                ]
            )
        )
        request = ToolRequest(
            tool_name="play_spotify",
            arguments={"query": "morning jazz", "mode": "search_and_play"},
            user_utterance="play some morning jazz",
            reason="test",
        )
        return await tool.run(request)

    monkeypatch.setattr("app.tools.spotify.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.tools.spotify._is_spotify_running", lambda: False)
    monkeypatch.setattr("app.tools.spotify.asyncio.sleep", fake_sleep)
    result = asyncio.run(run_tool())

    assert launches
    assert result.status == "observed"
    assert "scripted morning jazz playlist" in result.display_text.lower()


def test_spotify_tool_retargets_running_spotify_and_observes_playback(monkeypatch):
    launches = []

    def fake_open_target(target_uri):
        launches.append(target_uri)
        return True

    async def fake_sleep(_seconds):
        return None

    async def run_tool():
        tool = SpotifyTool(
            session_client=FakeSessionClient(
                sessions=[
                    MediaSessionSnapshot(
                        source_app="Spotify",
                        status="Playing",
                        title="After Dark",
                        artist="Mr.Kitty",
                        album="Time",
                    ),
                    MediaSessionSnapshot(
                        source_app="Spotify",
                        status="Playing",
                        title="So What",
                        artist="Miles Davis",
                        album="Kind of Blue",
                    ),
                ]
            )
        )
        request = ToolRequest(
            tool_name="play_spotify",
            arguments={"query": "morning jazz", "mode": "search_and_play"},
            user_utterance="play some morning jazz",
            reason="test",
        )
        return await tool.run(request)

    monkeypatch.setattr("app.tools.spotify._is_spotify_running", lambda: True)
    monkeypatch.setattr("app.tools.spotify._open_spotify_target", fake_open_target)
    monkeypatch.setattr("app.tools.spotify.asyncio.sleep", fake_sleep)
    result = asyncio.run(run_tool())

    assert launches
    assert result.status == "observed"
    assert "confirmed spotify is playing" in result.display_text.lower()


def test_spotify_tool_resume_mode_uses_session_play():
    snapshot = MediaSessionSnapshot(
        source_app="Spotify",
        status="Playing",
        title="After Dark",
        artist="Mr.Kitty",
        album="Time",
    )

    async def run_tool():
        tool = SpotifyTool(
            session_client=FakeSessionClient(
                sessions=[None],
                controls=[MediaSessionCommandResult(action="play", accepted=True, snapshot=snapshot)],
            )
        )
        request = ToolRequest(
            tool_name="play_spotify",
            arguments={"mode": "resume"},
            user_utterance="resume spotify",
            reason="test",
        )
        return await tool.run(request)

    result = asyncio.run(run_tool())

    assert result.status == "observed"
    assert "resumed spotify playback" in result.display_text.lower()
    assert result.data["session"]["title"] == "After Dark"


def test_spotify_tool_exact_match_does_not_reopen(monkeypatch):
    launches = []
    session = MediaSessionSnapshot(
        source_app="Spotify",
        status="Playing",
        title="After Dark",
        artist="Mr.Kitty",
        album="Time",
    )

    def fake_open_target(target_uri):
        launches.append(target_uri)
        return True

    async def run_tool():
        tool = SpotifyTool(session_client=FakeSessionClient(sessions=[session]))
        request = ToolRequest(
            tool_name="play_spotify",
            arguments={"query": "After Dark", "mode": "search_and_play"},
            user_utterance="play after dark",
            reason="test",
        )
        return await tool.run(request)

    monkeypatch.setattr("app.tools.spotify._open_spotify_target", fake_open_target)
    result = asyncio.run(run_tool())

    assert launches == []
    assert result.status == "observed"
    assert "already playing after dark" in result.display_text.lower()


def test_spotify_tool_falls_back_to_media_key_when_observer_missing(monkeypatch):
    play_toggles = []

    async def fake_sleep(_seconds):
        return None

    async def run_tool():
        tool = SpotifyTool(session_client=FakeSessionClient())
        request = ToolRequest(
            tool_name="play_spotify",
            arguments={"query": "morning jazz", "mode": "search_and_play"},
            user_utterance="play some morning jazz",
            reason="test",
        )
        return await tool.run(request)

    monkeypatch.setattr("app.tools.spotify._is_spotify_running", lambda: False)
    monkeypatch.setattr("app.tools.spotify._open_spotify_target", lambda _target_uri: True)
    monkeypatch.setattr("app.tools.spotify._send_media_play_pause", lambda: play_toggles.append(True))
    monkeypatch.setattr("app.tools.spotify.asyncio.sleep", fake_sleep)
    result = asyncio.run(run_tool())

    assert result.status == "attempted"
    assert play_toggles == [True]
