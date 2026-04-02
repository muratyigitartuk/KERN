from __future__ import annotations

import ctypes
import logging
import platform
import shutil
import subprocess
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from app.types import ClipboardSnapshot, CurrentContextSnapshot, ForegroundWindowSnapshot, MediaContextSnapshot
from app.windows_media import WindowsMediaSessionClient


_USER32 = ctypes.windll.user32 if platform.system() == "Windows" else None
_KERNEL32 = ctypes.windll.kernel32 if platform.system() == "Windows" else None
_PSAPI = ctypes.windll.psapi if platform.system() == "Windows" else None
_POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WindowsWindowClient:
    @property
    def available(self) -> bool:
        return _USER32 is not None

    def snapshot(self) -> ForegroundWindowSnapshot | None:
        if not self.available or _USER32 is None:
            return None
        try:
            hwnd = _USER32.GetForegroundWindow()
            if not hwnd:
                return None
            length = _USER32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return None
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            _USER32.GetWindowTextW(hwnd, title_buffer, length + 1)
            process_id = ctypes.c_ulong(0)
            _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            return ForegroundWindowSnapshot(
                title=title_buffer.value.strip()[:180],
                process_id=int(process_id.value) if process_id.value else None,
                process_name=_resolve_process_name(process_id.value),
                captured_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.debug("foreground window snapshot failed: %s", exc)
            return None


class WindowsClipboardClient:
    def __init__(self, max_chars: int = 280) -> None:
        self.max_chars = max_chars

    @property
    def available(self) -> bool:
        return platform.system() == "Windows" and bool(_POWERSHELL)

    def read_text(self) -> str:
        if not self.available or not _POWERSHELL:
            return ""
        try:
            result = subprocess.run(
                [_POWERSHELL, "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:
            logger.debug("clipboard read failed: %s", exc)
            return ""
        if result.returncode != 0:
            return ""
        text = (result.stdout or "").strip()
        if not text:
            return ""
        return " ".join(text.split())[: self.max_chars]

    def snapshot(self) -> ClipboardSnapshot | None:
        text = self.read_text()
        return ClipboardSnapshot(
            has_text=bool(text),
            excerpt=text,
            char_count=len(text),
            captured_at=datetime.now(timezone.utc),
        )


def _resolve_process_name(process_id: int) -> str:
    if not process_id or _KERNEL32 is None or _PSAPI is None:
        return ""
    process_handle = None
    try:
        process_handle = _KERNEL32.OpenProcess(0x0410, False, process_id)
        if not process_handle:
            return ""
        buffer = ctypes.create_unicode_buffer(260)
        if _PSAPI.GetModuleBaseNameW(process_handle, None, buffer, len(buffer)):
            return buffer.value
    except Exception as exc:
        logger.debug("process name resolution failed: %s", exc)
        return ""
    finally:
        if process_handle:
            try:
                _KERNEL32.CloseHandle(process_handle)
            except Exception:  # cleanup — best-effort
                pass
    return ""


class CurrentContextService:
    def __init__(
        self,
        *,
        window_client=None,
        clipboard_client: WindowsClipboardClient | None = None,
        media_client: WindowsMediaSessionClient | None = None,
        window_enabled: bool = True,
        clipboard_enabled: bool = False,
        media_enabled: bool = True,
    ) -> None:
        self.window_client = window_client or WindowsWindowClient()
        self.clipboard_client = clipboard_client or WindowsClipboardClient()
        self.media_client = media_client or WindowsMediaSessionClient()
        self.window_enabled = window_enabled
        self.clipboard_enabled = clipboard_enabled
        self.media_enabled = media_enabled

    def collect(self) -> CurrentContextSnapshot:
        window = self._read_window() if self.window_enabled else None
        clipboard = self._read_clipboard() if self.clipboard_enabled else None
        media = self._read_media() if self.media_enabled else None
        return CurrentContextSnapshot(
            window=window,
            clipboard=clipboard,
            media=media,
            sources={
                "window": bool(window),
                "clipboard": bool(clipboard and clipboard.has_text),
                "media": bool(media and media.title),
            },
        )

    def _read_window(self) -> ForegroundWindowSnapshot | None:
        try:
            return self.window_client.snapshot()
        except Exception as exc:
            logger.debug("window read failed: %s", exc)
            return None

    def _read_clipboard(self) -> ClipboardSnapshot | None:
        return self.clipboard_client.snapshot()

    def _read_media(self) -> MediaContextSnapshot | None:
        if not self.media_client.available:
            return None
        snapshot = self.media_client.spotify_session()
        if snapshot is None:
            return None
        return MediaContextSnapshot(
            source_app=snapshot.source_app,
            status=snapshot.status,
            title=snapshot.title,
            artist=snapshot.artist,
            album=snapshot.album,
            captured_at=datetime.now(timezone.utc),
        )
