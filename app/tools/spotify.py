from __future__ import annotations

import asyncio
import ctypes
import os
import re
import subprocess
import urllib.parse
import webbrowser
from typing import Any

from app.tools.base import Tool
from app.types import ToolRequest, ToolResult
from app.windows_media import MediaSessionSnapshot, WindowsMediaSessionClient


VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
KEYEVENTF_KEYUP = 0x0002

SCRIPTED_URI_MAP = {
    "morning jazz": "spotify:playlist:37i9dQZF1DX3Ogo9pFvBkY",
    "jazz": "spotify:playlist:37i9dQZF1DXbITWG1ZJKYt",
    "lofi": "spotify:playlist:37i9dQZF1DWWQRwui0ExPn",
    "lo-fi": "spotify:playlist:37i9dQZF1DWWQRwui0ExPn",
    "focus": "spotify:playlist:37i9dQZF1DWZeKCadgRdKQ",
}

QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "music",
    "on",
    "play",
    "put",
    "some",
    "song",
    "songs",
    "spotify",
    "start",
    "the",
}


def _send_media_play_pause() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)


def _send_media_next_track() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_MEDIA_NEXT_TRACK, 0, 0, 0)
    user32.keybd_event(VK_MEDIA_NEXT_TRACK, 0, KEYEVENTF_KEYUP, 0)


def _is_spotify_running() -> bool:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Spotify.exe"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=creation_flags,
        )
    except Exception:
        return False
    return "Spotify.exe" in result.stdout


def _resolve_scripted_uri(query: str) -> str:
    lowered = query.lower().strip()
    for key, uri in SCRIPTED_URI_MAP.items():
        if key in lowered:
            return uri
    return f"spotify:search:{urllib.parse.quote(query)}"


def _open_spotify_target(target_uri: str) -> bool:
    try:
        subprocess.Popen(["cmd", "/c", "start", "", target_uri], shell=False)
        return True
    except Exception:
        return webbrowser.open(target_uri)


class SpotifyTool(Tool):
    name = "play_spotify"

    def __init__(self, session_client: WindowsMediaSessionClient | None = None) -> None:
        self.session_client = session_client or WindowsMediaSessionClient()

    def availability(self) -> tuple[bool, str | None]:
        if os.name != "nt":
            return False, "Spotify desktop control is only supported on Windows."
        return True, None

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query or track/playlist name"},
                "mode": {
                    "type": "string",
                    "enum": ["search_and_play", "pause", "resume", "resume_current", "next", "playlist"],
                    "description": "Playback control mode",
                },
            },
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        mode = str(request.arguments.get("mode", "search_and_play"))
        query = str(request.arguments.get("query", "")).strip() or "morning jazz"

        if mode == "pause":
            return await self._pause_playback()
        if mode in {"resume", "resume_current"}:
            return await self._resume_playback()
        if mode == "next":
            return await self._skip_next()
        return await self._play_query(query=query, mode=mode)

    async def _pause_playback(self) -> ToolResult:
        command = await asyncio.to_thread(self.session_client.control, "pause")
        if command and command.accepted and command.snapshot and command.snapshot.status.lower() == "paused":
            return ToolResult(
                success=True,
                status="observed",
                display_text="Paused Spotify.",
                spoken_text="",
                evidence=[self._describe_session(command.snapshot), "Observed Spotify in a paused state."],
                side_effects=["media_session_pause"],
                data={"mode": "pause", "session": self._snapshot_data(command.snapshot)},
            )

        await asyncio.to_thread(_send_media_play_pause)
        return ToolResult(
            success=True,
            status="attempted",
            display_text="Sent a pause command to the active media session.",
            spoken_text="",
            evidence=["Fallback media play/pause key sent."],
            side_effects=["media_key_sent"],
            data={"mode": "pause"},
        )

    async def _resume_playback(self) -> ToolResult:
        session = await asyncio.to_thread(self.session_client.spotify_session)
        if session and session.is_playing:
            return ToolResult(
                success=True,
                status="observed",
                display_text="Spotify is already playing.",
                spoken_text="",
                evidence=[self._describe_session(session)],
                side_effects=[],
                data={"mode": "resume", "session": self._snapshot_data(session)},
            )

        command = await asyncio.to_thread(self.session_client.control, "play")
        if command and command.accepted and command.snapshot and command.snapshot.is_playing:
            return ToolResult(
                success=True,
                status="observed",
                display_text="Resumed Spotify playback.",
                spoken_text="",
                evidence=[self._describe_session(command.snapshot), "Observed Spotify in a playing state."],
                side_effects=["media_session_play"],
                data={"mode": "resume", "session": self._snapshot_data(command.snapshot)},
            )

        await asyncio.to_thread(_send_media_play_pause)
        return ToolResult(
            success=True,
            status="attempted",
            display_text="Sent a resume command to the active media session.",
            spoken_text="",
            evidence=["Fallback media play/pause key sent."],
            side_effects=["media_key_sent"],
            data={"mode": "resume"},
        )

    async def _skip_next(self) -> ToolResult:
        command = await asyncio.to_thread(self.session_client.control, "next")
        if command and command.accepted and command.snapshot:
            return ToolResult(
                success=True,
                status="observed",
                display_text="Skipped to the next Spotify track.",
                spoken_text="",
                evidence=[self._describe_session(command.snapshot), "Observed Spotify after the skip command."],
                side_effects=["media_session_next"],
                data={"mode": "next", "session": self._snapshot_data(command.snapshot)},
            )

        await asyncio.to_thread(_send_media_next_track)
        return ToolResult(
            success=True,
            status="attempted",
            display_text="Sent the system next-track command.",
            spoken_text="",
            evidence=["Fallback next-track key sent."],
            side_effects=["media_key_sent"],
            data={"mode": "next"},
        )

    async def _play_query(self, query: str, mode: str) -> ToolResult:
        target_uri = _resolve_scripted_uri(query)
        was_running = await asyncio.to_thread(_is_spotify_running)
        before = await asyncio.to_thread(self.session_client.spotify_session)
        exact_match_possible = not target_uri.startswith("spotify:playlist:")

        if before and exact_match_possible and self._session_matches_query(before, query):
            if before.is_playing:
                return ToolResult(
                    success=True,
                    status="observed",
                    display_text=f"Spotify is already playing {query}.",
                    spoken_text="",
                    evidence=[self._describe_session(before), "Observed the requested track in the active Spotify session."],
                    side_effects=[],
                    data={
                        "query": query,
                        "target_uri": target_uri,
                        "mode": mode,
                        "session": self._snapshot_data(before),
                        "exact_match_observed": True,
                    },
                )
            command = await asyncio.to_thread(self.session_client.control, "play")
            if command and command.accepted and command.snapshot and command.snapshot.is_playing:
                return ToolResult(
                    success=True,
                    status="observed",
                    display_text=f"Resumed Spotify on {query}.",
                    spoken_text="",
                    evidence=[self._describe_session(command.snapshot), "Observed the requested track after a direct play command."],
                    side_effects=["media_session_play"],
                    data={
                        "query": query,
                        "target_uri": target_uri,
                        "mode": mode,
                        "session": self._snapshot_data(command.snapshot),
                        "exact_match_observed": True,
                    },
                )

        opened = await asyncio.to_thread(_open_spotify_target, target_uri)
        await asyncio.sleep(1.1 if was_running else 1.8)

        final_session = await asyncio.to_thread(
            self.session_client.wait_for_session,
            None,
            2.8 if was_running else 4.0,
            0.35,
        )

        if final_session and not final_session.is_playing:
            command = await asyncio.to_thread(self.session_client.control, "play")
            if command and command.snapshot:
                final_session = command.snapshot
            else:
                await asyncio.to_thread(_send_media_play_pause)
                await asyncio.sleep(0.35)
                final_session = await asyncio.to_thread(self.session_client.spotify_session)
        elif final_session is None:
            await asyncio.to_thread(_send_media_play_pause)
            await asyncio.sleep(0.35)
            final_session = await asyncio.to_thread(self.session_client.spotify_session)

        evidence = [f"Target URI: {target_uri}."]
        side_effects = ["spotify_open_request"]
        if final_session and final_session.is_playing:
            evidence.append(self._describe_session(final_session))
        if before:
            evidence.append(f"Previous Spotify session was {before.status.lower()}: {self._describe_session(before)}")

        exact_match = bool(final_session and exact_match_possible and self._session_matches_query(final_session, query))
        playlist_navigation_observed = bool(
            final_session
            and final_session.is_playing
            and not exact_match_possible
            and (before is None or self._fingerprint(before) != self._fingerprint(final_session) or not before.is_playing)
        )

        if exact_match or playlist_navigation_observed:
            side_effects.append("media_session_play")
            display_text = f"Retargeted Spotify to {query} and confirmed playback."
            if not was_running:
                display_text = f"Opened Spotify for {query} and confirmed playback."
            if target_uri.startswith("spotify:playlist:"):
                display_text = f"Loaded the scripted {query} playlist and confirmed Spotify is playing."
            return ToolResult(
                success=True,
                status="observed",
                display_text=display_text,
                spoken_text="",
                evidence=evidence,
                side_effects=side_effects,
                data={
                    "query": query,
                    "target_uri": target_uri,
                    "mode": mode,
                    "was_running": was_running,
                    "session": self._snapshot_data(final_session),
                    "exact_match_observed": exact_match,
                },
            )

        if target_uri.startswith("spotify:search:") and not opened:
            webbrowser.open(f"https://open.spotify.com/search/{urllib.parse.quote(query)}")
            evidence.append("Desktop URI dispatch failed; opened Spotify web search as a fallback.")

        if final_session and final_session.is_playing and not exact_match_possible:
            evidence.append("Spotify is playing, but Windows media session does not expose playlist identity.")
        elif final_session and final_session.is_playing:
            evidence.append("Spotify is playing, but the active track metadata does not match the request yet.")
        else:
            evidence.append("Could not verify an active Spotify playback session after dispatch.")

        display_text = f"Sent Spotify the request for {query}, but I could not fully verify playback yet."
        if was_running:
            display_text = f"Retargeted the active Spotify session toward {query}, but playback could not be fully verified."
        return ToolResult(
            success=True,
            status="attempted",
            display_text=display_text,
            spoken_text="",
            evidence=evidence,
            side_effects=side_effects + ["media_key_sent"],
            suggested_follow_up="Try a more specific track request or add an exact Spotify URI for this item.",
            data={
                "query": query,
                "target_uri": target_uri,
                "mode": mode,
                "was_running": was_running,
                "session": self._snapshot_data(final_session),
                "exact_match_observed": exact_match,
            },
        )

    def _session_matches_query(self, session: MediaSessionSnapshot, query: str) -> bool:
        normalized_query = query.lower().strip()
        if not normalized_query:
            return False
        searchable_text = session.searchable_text()
        if normalized_query in searchable_text:
            return True
        query_tokens = self._query_tokens(query)
        metadata_tokens = self._query_tokens(searchable_text)
        return bool(query_tokens) and query_tokens.issubset(metadata_tokens)

    def _query_tokens(self, text: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", text.lower())
            if len(token) > 1 and token not in QUERY_STOPWORDS
        }

    def _fingerprint(self, session: MediaSessionSnapshot | None) -> tuple[str, str, str, str]:
        if session is None:
            return ("", "", "", "")
        return (session.title, session.artist, session.album, session.status)

    def _describe_session(self, session: MediaSessionSnapshot) -> str:
        title = session.title or "Unknown title"
        artist = session.artist or "Unknown artist"
        return f"Spotify {session.status.lower()} with '{title}' by {artist}."

    def _snapshot_data(self, session: MediaSessionSnapshot | None) -> dict[str, object]:
        if session is None:
            return {}
        return {
            "source_app": session.source_app,
            "status": session.status,
            "title": session.title,
            "artist": session.artist,
            "album": session.album,
            "can_play": session.can_play,
            "can_pause": session.can_pause,
            "can_next": session.can_next,
            "is_current": session.is_current,
        }
