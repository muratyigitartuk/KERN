from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass


_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")

_POWERSHELL_COMMON = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime] > $null
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime] > $null

function Get-AsTaskMethod {
  return [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } |
    Select-Object -First 1
}

function Invoke-AsyncOperation([object]$operation, [type]$resultType) {
  $method = Get-AsTaskMethod
  if ($null -eq $method) {
    throw 'Unable to resolve generic AsTask helper.'
  }
  $generic = $method.MakeGenericMethod($resultType)
  $task = $generic.Invoke($null, @($operation))
  $task.Wait()
  return $task.Result
}

function Get-SessionManager {
  return Invoke-AsyncOperation ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
}

function Get-SpotifySession([object]$manager) {
  $sessions = @($manager.GetSessions() | Where-Object { $_.SourceAppUserModelId -match 'Spotify' })
  if ($sessions.Count -eq 0) {
    return $null
  }
  $current = $manager.GetCurrentSession()
  if ($null -ne $current -and $current.SourceAppUserModelId -match 'Spotify') {
    return $current
  }
  return $sessions[0]
}

function Get-SessionSnapshot([object]$manager, [object]$session) {
  $playbackInfo = $session.GetPlaybackInfo()
  $properties = Invoke-AsyncOperation ($session.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
  $current = $manager.GetCurrentSession()
  $isCurrent = $null -ne $current -and $current.SourceAppUserModelId -eq $session.SourceAppUserModelId
  return [PSCustomObject]@{
    source_app = $session.SourceAppUserModelId
    status = [string]$playbackInfo.PlaybackStatus
    title = $properties.Title
    artist = $properties.Artist
    album = $properties.AlbumTitle
    can_play = [bool]$playbackInfo.Controls.IsPlayEnabled
    can_pause = [bool]$playbackInfo.Controls.IsPauseEnabled
    can_next = [bool]$playbackInfo.Controls.IsNextEnabled
    is_current = [bool]$isCurrent
  }
}
"""

_QUERY_SCRIPT = (
    _POWERSHELL_COMMON
    + r"""
try {
  $manager = Get-SessionManager
  if ($null -eq $manager) {
    [PSCustomObject]@{ available = $false; reason = 'no_manager' } | ConvertTo-Json -Compress
    exit 0
  }
  $session = Get-SpotifySession $manager
  if ($null -eq $session) {
    [PSCustomObject]@{ available = $true; found = $false } | ConvertTo-Json -Compress
    exit 0
  }
  Get-SessionSnapshot $manager $session | ConvertTo-Json -Compress -Depth 4
}
catch {
  [PSCustomObject]@{ available = $false; found = $false; reason = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""
)

_CONTROL_SCRIPT_TEMPLATE = (
    _POWERSHELL_COMMON
    + r"""
$Action = __ACTION__
try {
  $manager = Get-SessionManager
  if ($null -eq $manager) {
    [PSCustomObject]@{ available = $false; found = $false; reason = 'no_manager' } | ConvertTo-Json -Compress
    exit 0
  }
  $session = Get-SpotifySession $manager
  if ($null -eq $session) {
    [PSCustomObject]@{ available = $true; found = $false; reason = 'spotify_session_not_found' } | ConvertTo-Json -Compress
    exit 0
  }

  switch ($Action) {
    'play' { $accepted = Invoke-AsyncOperation ($session.TryPlayAsync()) ([bool]) }
    'pause' { $accepted = Invoke-AsyncOperation ($session.TryPauseAsync()) ([bool]) }
    'next' { $accepted = Invoke-AsyncOperation ($session.TrySkipNextAsync()) ([bool]) }
    default {
      [PSCustomObject]@{ available = $true; found = $true; reason = 'unsupported_action' } | ConvertTo-Json -Compress
      exit 0
    }
  }

  Start-Sleep -Milliseconds 350
  [PSCustomObject]@{
    available = $true
    found = $true
    action = $Action
    accepted = [bool]$accepted
    snapshot = Get-SessionSnapshot $manager $session
  } | ConvertTo-Json -Compress -Depth 5
}
catch {
  [PSCustomObject]@{ available = $false; found = $false; reason = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""
)


@dataclass(slots=True)
class MediaSessionSnapshot:
    source_app: str
    status: str
    title: str = ""
    artist: str = ""
    album: str = ""
    can_play: bool = False
    can_pause: bool = False
    can_next: bool = False
    is_current: bool = False

    @property
    def is_playing(self) -> bool:
        return self.status.lower() == "playing"

    def searchable_text(self) -> str:
        return " ".join(part for part in [self.title, self.artist, self.album] if part).lower()


@dataclass(slots=True)
class MediaSessionCommandResult:
    action: str
    accepted: bool
    snapshot: MediaSessionSnapshot | None = None
    reason: str | None = None


class WindowsMediaSessionClient:
    def __init__(self, powershell_path: str | None = None) -> None:
        self.powershell_path = powershell_path or _POWERSHELL

    @property
    def available(self) -> bool:
        return platform.system() == "Windows" and bool(self.powershell_path)

    def spotify_session(self) -> MediaSessionSnapshot | None:
        payload = self._run_json(_QUERY_SCRIPT)
        if not isinstance(payload, dict):
            return None
        if payload.get("found") is False or payload.get("available") is False:
            return None
        return self._snapshot_from_payload(payload)

    def control(self, action: str) -> MediaSessionCommandResult | None:
        script = _CONTROL_SCRIPT_TEMPLATE.replace("__ACTION__", json.dumps(action))
        payload = self._run_json(script)
        if not isinstance(payload, dict):
            return None
        if payload.get("found") is False or payload.get("available") is False:
            return MediaSessionCommandResult(action=action, accepted=False, reason=payload.get("reason"))
        snapshot_payload = payload.get("snapshot")
        snapshot = self._snapshot_from_payload(snapshot_payload) if isinstance(snapshot_payload, dict) else None
        return MediaSessionCommandResult(
            action=action,
            accepted=bool(payload.get("accepted")),
            snapshot=snapshot,
            reason=payload.get("reason"),
        )

    def wait_for_session(
        self,
        predicate=None,
        timeout_seconds: float = 3.0,
        poll_interval_seconds: float = 0.35,
    ) -> MediaSessionSnapshot | None:
        deadline = time.monotonic() + timeout_seconds
        last_snapshot: MediaSessionSnapshot | None = None
        while time.monotonic() < deadline:
            last_snapshot = self.spotify_session()
            if last_snapshot is not None and (predicate is None or predicate(last_snapshot)):
                return last_snapshot
            time.sleep(poll_interval_seconds)
        return last_snapshot

    def _run_json(self, script: str) -> dict | list | None:
        if not self.available or not self.powershell_path:
            return None
        try:
            result = subprocess.run(
                [
                    self.powershell_path,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return None
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError:
            return None

    def _snapshot_from_payload(self, payload: dict) -> MediaSessionSnapshot:
        return MediaSessionSnapshot(
            source_app=str(payload.get("source_app", "")),
            status=str(payload.get("status", "")),
            title=str(payload.get("title", "")),
            artist=str(payload.get("artist", "")),
            album=str(payload.get("album", "")),
            can_play=bool(payload.get("can_play")),
            can_pause=bool(payload.get("can_pause")),
            can_next=bool(payload.get("can_next")),
            is_current=bool(payload.get("is_current")),
        )
