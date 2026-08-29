import logging
import threading
from collections.abc import Callable

from .audio import MicrophoneCapture
from .events import AudioChunk, TranscriptEvent
from .stt.base import StateHandler, StreamingSTT

logger = logging.getLogger(__name__)


class TranscriptionPipeline:
    """Keeps the model warm and gates microphone audio with push-to-talk."""

    def __init__(self, microphone: MicrophoneCapture, stt: StreamingSTT) -> None:
        self.microphone = microphone
        self.stt = stt
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
        try:
            self.microphone.start(self._on_audio)
        except Exception:
            self.stt.stop()
            raise
        self._running = True
        logger.info("Transcription pipeline started; waiting for push-to-talk")

    def begin_capture(self) -> bool:
        with self._capture_lock:
            if not self._running or self._capturing:
                return False
            self._capturing = True
        logger.info("Push-to-talk capture started")
        return True

    def end_capture(self) -> bool:
        with self._capture_lock:
            if not self._capturing:
                return False
            self._capturing = False
            # The lock preserves queue ordering against the microphone callback.
            self.stt.finalize()
        logger.info("Push-to-talk capture ended; finalization requested")
        return True

    def change_microphone(self, device: str | int) -> str:
        with self._capture_lock:
            if self._capturing:
                raise RuntimeError("Release the dictation key before changing microphone")
            return self.microphone.switch_device(device)

    def _on_audio(self, chunk: AudioChunk) -> None:
        with self._capture_lock:
            if self._capturing:
                self.stt.send_audio(chunk)

    def stop(self) -> None:
        if not self._running:
            return
        with self._capture_lock:
            self._capturing = False
        self.microphone.stop()
        self.stt.stop()
        self._running = False
        logger.info("Transcription pipeline stopped")
