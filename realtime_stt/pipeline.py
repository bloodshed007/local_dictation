import logging
import threading
from collections.abc import Callable

from .audio import MicrophoneCapture
from .events import AudioChunk, TranscriptEvent
from .stt.base import StateHandler, StreamingSTT

logger = logging.getLogger(__name__)


class TranscriptionPipeline:
    """Keeps the model warm and gates microphone audio with push-to-talk."""

    def __init__(
        self,
        microphone: MicrophoneCapture,
        stt: StreamingSTT,
        release_microphone_when_idle: bool = False,
    ) -> None:
        self.microphone = microphone
        self.stt = stt
        self.release_microphone_when_idle = release_microphone_when_idle
        self.on_level: Callable[[int], None] | None = None
        self._running = False
        self._capturing = False
        self._capture_lock = threading.Lock()

    def start(
        self,
        on_transcript: Callable[[TranscriptEvent], None],
        on_state: StateHandler,
    ) -> None:
        if self._running:
            return
        self.stt.start(on_transcript, on_state)
        if not self.release_microphone_when_idle:
            try:
                self.microphone.start(self._on_audio)
            except Exception:
                self.stt.stop()
                raise
        self._running = True
        logger.info("Transcription pipeline started; waiting for push-to-talk")

    def begin_capture(self) -> bool:
        if not self._running:
            return False
        with self._capture_lock:
            if self._capturing:
                return False
            self._capturing = True
        try:
            if self.release_microphone_when_idle:
                self.microphone.start(self._on_audio)
            else:
                # PortAudio streams can remain open but stop producing callbacks
                # after Windows sleep or a Bluetooth/USB reconnect.
                self.microphone.ensure_active()
        except Exception:
            with self._capture_lock:
                self._capturing = False
            raise
        logger.info("Dictation capture started")
        return True

    def end_capture(self) -> bool:
        with self._capture_lock:
            if not self._capturing:
                return False
            self._capturing = False
            # The lock preserves queue ordering against the microphone callback.
            self.stt.finalize()
        self._release_microphone_if_idle()
        logger.info("Dictation capture ended; finalization requested")
        return True

    def cancel_capture(self) -> bool:
        with self._capture_lock:
            if not self._capturing:
                return False
            self._capturing = False
            self.stt.cancel()
        self._release_microphone_if_idle()
        logger.info("Dictation capture cancelled; buffered audio will be discarded")
        return True

    def set_release_microphone_when_idle(self, enabled: bool) -> None:
        with self._capture_lock:
            if self._capturing:
                raise RuntimeError("Finish the current dictation before changing mic behavior")
            if enabled == self.release_microphone_when_idle:
                return
            self.release_microphone_when_idle = enabled
            running = self._running

        if not running:
            return
        try:
            if enabled:
                self.microphone.stop()
            else:
                self.microphone.start(self._on_audio)
        except Exception:
            self.release_microphone_when_idle = not enabled
            raise
        logger.info("Release microphone while idle: %s", enabled)

    def _release_microphone_if_idle(self) -> None:
        if not self.release_microphone_when_idle:
            return
        try:
            self.microphone.stop()
        except Exception:
            logger.exception("Could not release microphone while idle")

    def change_microphone(self, device: str | int) -> str:
        with self._capture_lock:
            if self._capturing:
                raise RuntimeError("Release the dictation key before changing microphone")
            return self.microphone.switch_device(device)

    def _on_audio(self, chunk: AudioChunk) -> None:
        with self._capture_lock:
            if self._capturing:
                self.stt.send_audio(chunk)
                if self.on_level is not None:
                    self.on_level(chunk.rms)

    def stop(self) -> None:
        if not self._running:
            return
        with self._capture_lock:
            self._capturing = False
        self.microphone.stop()
        self.stt.stop()
        self._running = False
        logger.info("Transcription pipeline stopped")
