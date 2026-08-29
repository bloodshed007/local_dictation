import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from ..events import AudioChunk, TranscriptEvent
from .base import StateHandler, StreamingSTT, TranscriptHandler

logger = logging.getLogger(__name__)
_FINALIZE = object()


class FasterWhisperStreamingSTT(StreamingSTT):
    """Low-latency local dictation using repeated faster-whisper decoding."""

    def __init__(
        self,
        sample_rate: int = 16_000,
        model_name: str = "small",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "en",
        speech_rms_threshold: int = 350,
        decode_interval_ms: int = 500,
        final_silence_ms: int = 700,
        max_utterance_seconds: int = 30,
    ) -> None:
        self.sample_rate = sample_rate
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.speech_rms_threshold = speech_rms_threshold
        self.decode_interval_seconds = decode_interval_ms / 1000.0
        self.final_silence_ms = final_silence_ms
        self.max_utterance_seconds = max_utterance_seconds

        self._audio_queue: queue.Queue[AudioChunk | object] = queue.Queue(maxsize=400)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_transcript: TranscriptHandler | None = None
        self._on_state: StateHandler | None = None
        self._dll_handles: list[object] = []
        self._first_speech_at: float | None = None
        self._first_transcript_logged = False
        self._last_partial = ""

    def start(self, on_transcript: TranscriptHandler, on_state: StateHandler) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._on_transcript = on_transcript
        self._on_state = on_state
        self._stop_event.clear()
        self._first_speech_at = None
        self._first_transcript_logged = False
        self._last_partial = ""
        self._clear_audio_queue()
        self._thread = threading.Thread(target=self._run, name="faster-whisper", daemon=True)
        self._thread.start()

    def send_audio(self, chunk: AudioChunk) -> None:
        try:
            self._audio_queue.put_nowait(chunk)
        except queue.Full:
            logger.error("Audio queue is full; dropping a 50 ms chunk")
            self._emit_state("error", "Local transcription cannot process audio fast enough")

    def finalize(self) -> None:
        try:
            self._audio_queue.put(_FINALIZE, timeout=2.0)
        except queue.Full:
            logger.error("Audio queue is full; could not request finalization")
            self._emit_state("error", "Could not finalize dictation")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10.0)
        self._thread = None

    def _run(self) -> None:
        self._emit_state("loading", f"Loading faster-whisper {self.model_name} on {self.device}…")
        logger.info(
            "Loading faster-whisper model=%s device=%s compute_type=%s",
            self.model_name,
            self.device,
            self.compute_type,
        )

        try:
            self._configure_cuda_dlls()
            from faster_whisper import WhisperModel

            try:
                model = WhisperModel(
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                    local_files_only=True,
                )
                logger.info("Loaded faster-whisper model from the local cache")
            except Exception as cache_error:
                logger.info("Model is not cached; downloading it once: %s", cache_error)
                self._emit_state("loading", f"Downloading faster-whisper {self.model_name} once…")
                model = WhisperModel(
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                )

            self._warm_up(model)
            self._clear_audio_queue()
            logger.info("Discarded microphone audio captured during model startup")
            logger.info("faster-whisper model ready")
            self._emit_state(
                "connected",
                f"Listening locally — faster-whisper {self.model_name} ({self.device})",
            )
            self._process_audio(model)
        except Exception as exc:
            logger.exception("faster-whisper streaming error")
            self._emit_state("error", str(exc))
        finally:
            logger.info("faster-whisper worker stopped")
            self._emit_state("closed", "Stopped")

    def _process_audio(self, model) -> None:
        pre_roll: deque[AudioChunk] = deque(maxlen=6)  # 300 ms
        utterance: list[AudioChunk] = []
        active = False
        silence_ms = 0
        decoded_audio_seconds = 0.0

        while not self._stop_event.is_set() or not self._audio_queue.empty():
            try:
                item = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if item is _FINALIZE:
                if active and utterance:
                    logger.info("Finalizing utterance on push-to-talk release")
                    self._decode(model, utterance, is_final=True)
                pre_roll.clear()
                utterance = []
                active = False
                silence_ms = 0
                decoded_audio_seconds = 0.0
                self._emit_state("capture_finalized", "Dictation finalized")
                continue

            chunk = item
            if not isinstance(chunk, AudioChunk):
                continue

            if not active:
                pre_roll.append(chunk)
                if chunk.rms < self.speech_rms_threshold:
                    continue

                active = True
                utterance = list(pre_roll)
                pre_roll.clear()
                silence_ms = 0
                decoded_audio_seconds = 0.0
                if not self._first_transcript_logged:
                    self._first_speech_at = chunk.captured_at
                logger.info("Speech started (RMS=%d)", chunk.rms)
                continue

            utterance.append(chunk)
            if chunk.rms >= self.speech_rms_threshold:
                silence_ms = 0
            else:
                silence_ms += 50

            duration_seconds = self._duration_seconds(utterance)
            should_finalize = (
                silence_ms >= self.final_silence_ms
                or duration_seconds >= self.max_utterance_seconds
            )

            if should_finalize:
                reason = "silence" if silence_ms >= self.final_silence_ms else "maximum length"
                logger.info("Finalizing utterance after %.1f s (%s)", duration_seconds, reason)
                self._decode(model, utterance, is_final=True)
                pre_roll.extend(utterance[-6:])
                utterance = []
                active = False
                silence_ms = 0
                decoded_audio_seconds = 0.0
                continue

            enough_audio = duration_seconds >= 0.4
            enough_new_audio = duration_seconds - decoded_audio_seconds >= self.decode_interval_seconds
            if enough_audio and enough_new_audio:
                self._decode(model, utterance, is_final=False)
                decoded_audio_seconds = duration_seconds

        if active and utterance:
            logger.info("Finalizing remaining audio during shutdown")
            self._decode(model, utterance, is_final=True)

    def _decode(self, model, chunks: list[AudioChunk], is_final: bool) -> None:
        audio = self._to_float_audio(chunks)
        captured_at = chunks[-1].captured_at
        started_at = time.perf_counter()

        segments, _info = model.transcribe(
            audio,
            language=self.language,
            beam_size=5 if is_final else 1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 500,
                "speech_pad_ms": 200,
            },
        )
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
        inference_ms = (time.perf_counter() - started_at) * 1000.0
        latency_ms = (time.perf_counter() - captured_at) * 1000.0

        if is_final:
            if not text:
                if self._last_partial:
                    logger.warning("Final decode was empty; keeping the last non-empty hypothesis")
                    text = self._last_partial
                else:
                    return
            self._last_partial = ""
        elif text == self._last_partial:
            return
        else:
            self._last_partial = text

        first_transcript_ms = None
        if text and not self._first_transcript_logged:
            self._first_transcript_logged = True
            if self._first_speech_at is not None:
                first_transcript_ms = (time.perf_counter() - self._first_speech_at) * 1000.0
                logger.info("First-transcript latency: approximately %.0f ms", first_transcript_ms)

        label = "FINAL" if is_final else "INTERIM"
        logger.info(
            "%s (inference %.0f ms, capture-to-result %.0f ms): %s",
            label,
            inference_ms,
            latency_ms,
            text,
        )
        if self._on_transcript:
            self._on_transcript(
                TranscriptEvent(
                    text=text,
                    is_final=is_final,
                    latency_ms=latency_ms,
                    first_transcript_ms=first_transcript_ms,
                )
            )

    def _warm_up(self, model) -> None:
        """Pay one-time CUDA/kernel startup cost before reporting ready."""
        started_at = time.perf_counter()
        segments, _info = model.transcribe(
            np.zeros(self.sample_rate, dtype=np.float32),
            language=self.language,
            beam_size=1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        list(segments)
        logger.info("Model warm-up completed in %.0f ms", (time.perf_counter() - started_at) * 1000.0)

    def _configure_cuda_dlls(self) -> None:
        if self.device != "cuda" or os.name != "nt":
            return

        configured: list[Path] = []
        candidates = []
        explicit = os.getenv("STT_CUDA_DLL_DIR")
        if explicit:
            candidates.append(Path(explicit))
        candidates.append(Path(sys.base_prefix) / "Lib" / "site-packages" / "torch" / "lib")

        for directory in candidates:
            if not directory.is_dir() or directory in configured:
                continue
            os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
            if hasattr(os, "add_dll_directory"):
                self._dll_handles.append(os.add_dll_directory(str(directory)))
            configured.append(directory)

        if configured:
            logger.info("CUDA DLL search path: %s", ", ".join(map(str, configured)))
        else:
            logger.warning(
                "No CUDA runtime DLL directory found. Set STT_CUDA_DLL_DIR if model loading fails."
            )

    def _clear_audio_queue(self) -> None:
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                return

    def _duration_seconds(self, chunks: list[AudioChunk]) -> float:
        return sum(len(chunk.data) for chunk in chunks) / 2 / self.sample_rate

    @staticmethod
    def _to_float_audio(chunks: list[AudioChunk]) -> np.ndarray:
        pcm = b"".join(chunk.data for chunk in chunks)
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    def _emit_state(self, state: str, detail: str) -> None:
        if self._on_state:
            self._on_state(state, detail)
