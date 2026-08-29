import logging
import os
import queue
import threading
import time

from deepgram import DeepgramClient
from deepgram.core.events import EventType
from deepgram.listen.v1.types import ListenV1Results

from ..events import AudioChunk, TranscriptEvent
from .base import StateHandler, StreamingSTT, TranscriptHandler

logger = logging.getLogger(__name__)


class DeepgramStreamingSTT(StreamingSTT):
    """Deepgram Listen V1/Nova-3 streaming adapter."""

    def __init__(
        self,
        sample_rate: int = 16_000,
        model: str = "nova-3",
        language: str = "en-US",
        speech_rms_threshold: int = 350,
    ) -> None:
        self.sample_rate = sample_rate
        self.model = model
        self.language = language
        self.speech_rms_threshold = speech_rms_threshold
        self._audio_queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=200)
        self._stop_event = threading.Event()
        self._open_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_transcript: TranscriptHandler | None = None
        self._on_state: StateHandler | None = None
        self._submitted_audio_seconds = 0.0
        self._first_speech_sent_at: float | None = None
        self._first_transcript_logged = False

    def start(self, on_transcript: TranscriptHandler, on_state: StateHandler) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not os.getenv("DEEPGRAM_API_KEY") and not os.getenv("DEEPGRAM_TOKEN"):
            raise RuntimeError("Set DEEPGRAM_API_KEY in the environment or a local .env file.")

        self._on_transcript = on_transcript
        self._on_state = on_state
        self._stop_event.clear()
        self._open_event.clear()
        self._thread = threading.Thread(target=self._run, name="deepgram", daemon=True)
        self._thread.start()

    def send_audio(self, chunk: AudioChunk) -> None:
        try:
            self._audio_queue.put_nowait(chunk)
        except queue.Full:
            logger.error("Audio queue is full; dropping a 50 ms chunk")
            self._emit_state("error", "Audio cannot be sent fast enough")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        self._emit_state("connecting", "Connecting to Deepgram…")
        logger.info("Connecting to Deepgram Listen V1 (model=%s)", self.model)

        try:
            client = DeepgramClient()
            with client.listen.v1.connect(
                model=self.model,
                language=self.language,
                encoding="linear16",
                channels=1,
                sample_rate=self.sample_rate,
                interim_results=True,
                endpointing=300,
                smart_format=False,
                punctuate=False,
            ) as connection:
                connection.on(EventType.OPEN, self._handle_open)
                connection.on(EventType.MESSAGE, self._handle_message)
                connection.on(EventType.CLOSE, self._handle_close)
                connection.on(EventType.ERROR, self._handle_error)

                listener = threading.Thread(
                    target=self._listen,
                    args=(connection,),
                    name="deepgram-listener",
                    daemon=True,
                )
                sender = threading.Thread(
                    target=self._send_audio,
                    args=(connection,),
                    name="deepgram-sender",
                    daemon=True,
                )
                listener.start()
                sender.start()

                while not self._stop_event.wait(0.1) and listener.is_alive():
                    pass

                self._stop_event.set()
                sender.join(timeout=2.0)
                try:
                    connection.send_finalize()
                    time.sleep(0.15)
                    connection.send_close_stream()
                except Exception:
                    logger.debug("Connection was already closed", exc_info=True)
                listener.join(timeout=2.0)
        except Exception as exc:
            logger.exception("Deepgram streaming error")
            self._emit_state("error", str(exc))
        finally:
            self._open_event.clear()
            logger.info("Deepgram worker stopped")

    def _listen(self, connection) -> None:
        try:
            connection.start_listening()
        except Exception as exc:
            if not self._stop_event.is_set():
                logger.exception("Deepgram listener failed")
                self._emit_state("error", str(exc))

    def _send_audio(self, connection) -> None:
        if not self._open_event.wait(timeout=10.0):
            if not self._stop_event.is_set():
                self._emit_state("error", "Deepgram connection timed out")
            return

        while not self._stop_event.is_set() or not self._audio_queue.empty():
            try:
                chunk = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                connection.send_media(chunk.data)
                sent_at = time.perf_counter()
                self._submitted_audio_seconds = chunk.stream_end_seconds
                if self._first_speech_sent_at is None and chunk.rms >= self.speech_rms_threshold:
                    self._first_speech_sent_at = sent_at
                    logger.info("Approximate local speech onset detected (RMS=%d)", chunk.rms)
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.exception("Failed to send microphone audio")
                    self._emit_state("error", str(exc))
                return

    def _handle_open(self, _event) -> None:
        self._open_event.set()
        logger.info("Deepgram connection open")
        self._emit_state("connected", "Listening")

    def _handle_close(self, _event) -> None:
        logger.info("Deepgram connection closed")
        self._emit_state("closed", "Connection closed")

    def _handle_error(self, error) -> None:
        logger.error("Deepgram connection error: %s", error)
        self._emit_state("error", str(error))

    def _handle_message(self, message) -> None:
        if not isinstance(message, ListenV1Results):
            return
        if not message.channel or not message.channel.alternatives:
            return

        text = (message.channel.alternatives[0].transcript or "").strip()
        if not text:
            return

        is_final = bool(message.is_final)
        stream_lag_ms = None
        if not is_final:
            transcript_cursor = float(message.start or 0.0) + float(message.duration or 0.0)
            stream_lag_ms = max(
                0.0,
                (self._submitted_audio_seconds - transcript_cursor) * 1000.0,
            )

        first_transcript_ms = None
        if not self._first_transcript_logged:
            self._first_transcript_logged = True
            if self._first_speech_sent_at is not None:
                first_transcript_ms = (time.perf_counter() - self._first_speech_sent_at) * 1000.0
                logger.info("First-transcript latency: approximately %.0f ms", first_transcript_ms)
            else:
                logger.info("First transcript received before local speech-onset measurement")

        if is_final:
            logger.info("FINAL: %s", text)
        else:
            logger.info("INTERIM (stream lag ≈ %.0f ms): %s", stream_lag_ms, text)

        if self._on_transcript:
            self._on_transcript(
                TranscriptEvent(
                    text=text,
                    is_final=is_final,
                    latency_ms=stream_lag_ms,
                    first_transcript_ms=first_transcript_ms,
                )
            )

    def _emit_state(self, state: str, detail: str) -> None:
        if self._on_state:
            self._on_state(state, detail)
