from __future__ import annotations

import logging
import queue
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


logger = logging.getLogger(__name__)


class VoiceAdapter(Protocol):
    available: bool
    backend_name: str
    status: str

    def speak_text(self, text: str) -> bool:
        ...

    def stop(self) -> None:
        ...


class NullVoiceAdapter:
    available = False
    backend_name = "none"
    status = "No offline voice output available."

    def speak_text(self, text: str) -> bool:
        return False

    def stop(self) -> None:
        return None


class PyttsxVoiceAdapter:
    def __init__(self) -> None:
        self.available = False
        self.backend_name = "pyttsx3"
        self.status = "Local fallback voice unavailable."
        self._engine = None
        try:
            import pyttsx3

            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", 190)
            self.available = True
            self.status = "Local fallback voice ready."
        except Exception as exc:  # pragma: no cover - depends on local runtime speech stack
            self.status = f"Local fallback voice unavailable: {exc}"

    def speak_text(self, text: str) -> bool:
        if not self.available or not self._engine or not text:
            return False
        self._engine.say(text)
        self._engine.runAndWait()
        return True

    def set_rate(self, rate: int) -> None:
        if self._engine:
            self._engine.setProperty("rate", rate)

    def set_voice(self, voice_id: str) -> None:
        if not self._engine:
            return
        voices = self._engine.getProperty("voices")
        matched = next(
            (voice for voice in voices if voice_id.lower() in voice.id.lower() or voice_id.lower() in (voice.name or "").lower()),
            None,
        )
        if matched:
            self._engine.setProperty("voice", matched.id)

    def list_voices(self) -> list[object]:
        if not self._engine:
            return []
        return list(self._engine.getProperty("voices") or [])

    def stop(self) -> None:
        if self._engine:
            self._engine.stop()


class PiperVoiceAdapter:
    def __init__(self, binary_path: str | None, model_path: str | None) -> None:
        self.binary_path = Path(binary_path).expanduser().resolve() if binary_path else None
        self.model_path = Path(model_path).expanduser().resolve() if model_path else None
        self.available = bool(
            self.binary_path
            and self.model_path
            and self.binary_path.exists()
            and self.model_path.exists()
        )
        self.backend_name = "piper"
        self.status = "Primary local voice unavailable."
        if self.available:
            self.status = f"Primary local voice ready ({self.model_path.name})."

    def speak_text(self, text: str) -> bool:
        if not self.available or not text or not self.binary_path or not self.model_path:
            return False
        try:
            import winsound
        except Exception as exc:
            logger.debug("winsound unavailable, cannot speak: %s", exc)
            return False

        wav_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                wav_path = Path(handle.name)
            result = subprocess.run(
                [str(self.binary_path), "--model", str(self.model_path), "--output_file", str(wav_path)],
                input=text,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and wav_path.exists() and wav_path.stat().st_size > 0:
                winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
                return True
            return False
        finally:
            if wav_path:
                wav_path.unlink(missing_ok=True)

    def stop(self) -> None:
        try:  # pragma: no cover - Windows specific
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception as exc:
            logger.debug("Failed to stop playback: %s", exc)
            return


@dataclass(slots=True)
class SpeechItem:
    text: str
    done: threading.Event | None = None


class TTSService:
    def __init__(
        self,
        enabled: bool = True,
        piper_binary: str | None = None,
        piper_model: str | None = None,
        preferred_backend: str = "piper",
    ) -> None:
        self._queue: queue.Queue[SpeechItem | None] = queue.Queue()
        self._preferred_backend = preferred_backend
        self._primary_adapter, self._fallback_adapter = self._build_adapters(piper_binary, piper_model)
        self._desired_enabled = enabled
        self._active_adapter = self._select_active_adapter()
        self.backend_name = self._active_adapter.backend_name
        self.status = self._compose_status()
        self.enabled = self._desired_enabled and self._active_adapter.available
        self.on_speech_done: "Callable[[], None] | None" = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _build_adapters(self, piper_binary: str | None, piper_model: str | None) -> tuple[VoiceAdapter, VoiceAdapter]:
        pyttsx_adapter = PyttsxVoiceAdapter()
        piper_adapter = PiperVoiceAdapter(piper_binary, piper_model)
        if self._preferred_backend == "piper":
            primary = piper_adapter
            fallback = pyttsx_adapter if pyttsx_adapter.available else NullVoiceAdapter()
        else:
            primary = pyttsx_adapter if pyttsx_adapter.available else NullVoiceAdapter()
            fallback = piper_adapter if piper_adapter.available else NullVoiceAdapter()
        return primary, fallback

    def _select_active_adapter(self) -> VoiceAdapter:
        if self._primary_adapter.available:
            return self._primary_adapter
        if self._fallback_adapter.available:
            return self._fallback_adapter
        return NullVoiceAdapter()

    def _compose_status(self) -> str:
        if not self._desired_enabled:
            return "Voice output muted by settings."
        if self._active_adapter.available:
            if self._primary_adapter.available and self._fallback_adapter.available:
                return f"{self._active_adapter.status} Fallback ready: {self._fallback_adapter.backend_name}."
            return self._active_adapter.status
        return "No offline voice output available."

    def set_enabled(self, enabled: bool) -> None:
        self._desired_enabled = enabled
        self._active_adapter = self._select_active_adapter()
        self.backend_name = self._active_adapter.backend_name
        self.enabled = self._desired_enabled and self._active_adapter.available
        self.status = self._compose_status()

    def set_speed(self, speed: float) -> None:
        """Set speech rate (1.0 = normal). Applies to pyttsx3 only."""
        if isinstance(self._active_adapter, PyttsxVoiceAdapter):
            rate = max(50, min(400, int(190 * speed)))
            self._active_adapter.set_rate(rate)
        elif isinstance(self._fallback_adapter, PyttsxVoiceAdapter):
            rate = max(50, min(400, int(190 * speed)))
            self._fallback_adapter.set_rate(rate)

    def set_voice(self, voice_id: str) -> None:
        """Select a pyttsx3 voice by id or name fragment."""
        if isinstance(self._active_adapter, PyttsxVoiceAdapter):
            self._active_adapter.set_voice(voice_id)
        elif isinstance(self._fallback_adapter, PyttsxVoiceAdapter):
            self._fallback_adapter.set_voice(voice_id)

    def list_voices(self) -> list[str]:
        if isinstance(self._active_adapter, PyttsxVoiceAdapter):
            return [str(getattr(voice, "id", "")) for voice in self._active_adapter.list_voices()]
        if isinstance(self._fallback_adapter, PyttsxVoiceAdapter):
            return [str(getattr(voice, "id", "")) for voice in self._fallback_adapter.list_voices()]
        return []

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            if not self.enabled or not item.text:
                if item.done:
                    item.done.set()
                continue
            spoken = self._active_adapter.speak_text(item.text)
            if not spoken and self._fallback_adapter.available and self._fallback_adapter is not self._active_adapter:
                self._fallback_adapter.speak_text(item.text)
                self._active_adapter = self._fallback_adapter
                self.backend_name = self._active_adapter.backend_name
                self.enabled = self._desired_enabled and self._active_adapter.available
                self.status = f"Primary voice failed. Switched to fallback: {self._active_adapter.backend_name}."
            if item.done:
                item.done.set()
            if self.on_speech_done:
                try:
                    self.on_speech_done()
                except Exception as exc:
                    logger.warning("on_speech_done callback failed: %s", exc)

    def speak(self, text: str) -> None:
        if self.enabled:
            self._queue.put(SpeechItem(text=text))

    def speak_and_wait(self, text: str) -> None:
        if not self.enabled:
            return
        done = threading.Event()
        self._queue.put(SpeechItem(text=text, done=done))
        done.wait(timeout=20)

    def stop(self) -> None:
        self._active_adapter.stop()
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._queue.put(None)
                break
            if item.done:
                item.done.set()

    def shutdown(self) -> None:
        self._queue.put(None)

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()
