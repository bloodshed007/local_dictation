import json
import logging
import queue
import threading
import time
from pathlib import Path

from vosk import KaldiRecognizer, Model, SetLogLevel

from ..events import AudioChunk, TranscriptEvent
from .base import StateHandler, StreamingSTT, TranscriptHandler

logger = logging.getLogger(__name__)


class VoskStreamingSTT(StreamingSTT):
    """Free, offline streaming STT adapter using a local Vosk model."""

    def __init__(
        self,
        model_path: Path,
        sample_rate: int = 16_000,
        speech_rms_threshold: int = 350,
    ) -> None:
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.speech_rms_threshold = speech_rms_threshold
        self._audio_queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=200)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_transcript: TranscriptHandler | None = None
        self._on_state: StateHandler | None = None
        self._first_speech_at: float | None = None
        self._first_transcript_logged = False
        self._last_partial = ""

    def start(self, on_transcript: TranscriptHandler, on_state: StateHandler) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.model_path.is_dir():
            raise RuntimeError(f"Vosk model not found: {self.model_path}")

        self._on_transcript = on_transcript
        self._on_state = on_state
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="vosk", daemon=True)
        self._thread.start()

    def send_audio(self, chunk: AudioChunk) -> None:
        try:
            self._audio_queue.put_nowait(chunk)
        except queue.Full:
            logger.error("Audio queue is full; dropping a 50 ms chunk")
            self._emit_state("error", "Local transcription cannot process audio fast enough")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        self._emit_state("loading", "Loading local speech model…")
        logger.info("Loading local Vosk model: %s", self.model_path)
        try:
            SetLogLevel(-1)
            model = Model(str(self.model_path))
            recognizer = KaldiRecognizer(model, self.sample_rate)
            self._emit_state("connected", "Listening locally")
            logger.info("Local Vosk streaming recognizer ready")

            while not self._stop_event.is_set() or not self._audio_queue.empty():
                try:
                    chunk = self._audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if self._first_speech_at is None and chunk.rms >= self.speech_rms_threshold:
                    self._first_speech_at = chunk.captured_at
                    logger.info("Approximate local speech onset detected (RMS=%d)", chunk.rms)

                if recognizer.AcceptWaveform(chunk.data):
                    self._emit_result(json.loads(recognizer.Result()).get("text", ""), True, chunk)
                else:
                    partial = json.loads(recognizer.PartialResult()).get("partial", "")
                    if partial != self._last_partial:
                        self._emit_result(partial, False, chunk)
                        self._last_partial = partial

            final_text = json.loads(recognizer.FinalResult()).get("text", "")
            if final_text:
                self._emit_result(final_text, True, None)
        except Exception as exc:
            logger.exception("Local Vosk streaming error")
            self._emit_state("error", str(exc))
        finally:
            logger.info("Local Vosk worker stopped")
            self._emit_state("closed", "Stopped")

    def _emit_result(self, text: str, is_final: bool, chunk: AudioChunk | None) -> None:
        text = text.strip()
        if is_final:
            self._last_partial = ""
            if not text:
                return
        elif not text and not self._last_partial:
            return

        now = time.perf_counter()
        processing_lag_ms = (now - chunk.captured_at) * 1000.0 if chunk else None
        first_transcript_ms = None
        if text and not self._first_transcript_logged:
            self._first_transcript_logged = True
            if self._first_speech_at is not None:
                first_transcript_ms = (now - self._first_speech_at) * 1000.0
                logger.info("First-transcript latency: approximately %.0f ms", first_transcript_ms)

        if is_final:
            logger.info("FINAL: %s", text)
        else:
            logger.info("INTERIM (capture-to-result ≈ %.0f ms): %s", processing_lag_ms, text)

        if self._on_transcript:
            self._on_transcript(
                TranscriptEvent(
                    text=text,
                    is_final=is_final,
                    latency_ms=processing_lag_ms,
                    first_transcript_ms=first_transcript_ms,
                )
            )

    def _emit_state(self, state: str, detail: str) -> None:
        if self._on_state:
            self._on_state(state, detail)
