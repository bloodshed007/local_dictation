import logging
import math
import time
from array import array
from collections.abc import Callable

import sounddevice as sd

from .events import AudioChunk

logger = logging.getLogger(__name__)


class MicrophoneCapture:
    """Continuously captures raw mono PCM from the Windows default microphone."""

    def __init__(
        self,
        sample_rate: int = 16_000,
        chunk_ms: int = 50,
        device: str | int | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.device = self._resolve_device(device)
        self._stream: sd.RawInputStream | None = None
        self._samples_captured = 0
        self._on_audio: Callable[[AudioChunk], None] | None = None

    def start(self, on_audio: Callable[[AudioChunk], None]) -> None:
        if self._stream is not None:
            return

        self._on_audio = on_audio
        self._samples_captured = 0
        blocksize = int(self.sample_rate * self.chunk_ms / 1000)
        self._stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=blocksize,
            device=self.device,
            channels=1,
            dtype="int16",
            latency="low",
            callback=self._callback,
        )
        self._stream.start()
        device = sd.query_devices(self.device, kind="input")
        logger.info(
            "Microphone started: %s, %d Hz mono linear16, %d ms chunks",
            device["name"],
            self.sample_rate,
            self.chunk_ms,
        )

    def switch_device(self, device: str | int) -> str:
        """Switch the open input stream without touching the STT model."""
        resolved = self._resolve_device(device)
        if resolved == self.device:
            return self.current_device_name()

        callback = self._on_audio
        old_device = self.device
        was_running = self._stream is not None
        if was_running:
            self._close_stream(clear_callback=False)

        self.device = resolved
        try:
            if was_running and callback is not None:
                self.start(callback)
        except Exception:
            logger.exception("Could not open microphone %s; restoring prior device", device)
            self.device = old_device
            if was_running and callback is not None:
                self.start(callback)
            raise

        name = self.current_device_name()
        logger.info("Microphone switched to: %s", name)
        return name

    def current_device_name(self) -> str:
        return str(sd.query_devices(self.device, kind="input")["name"])

    def available_input_devices(self) -> list[tuple[int, str]]:
        """Return physical inputs from the default Windows host API."""
        host_api = int(sd.default.hostapi)
        result = []
        for index, candidate in enumerate(sd.query_devices()):
            if candidate["max_input_channels"] <= 0 or candidate["hostapi"] != host_api:
                continue
            name = str(candidate["name"])
            if name.casefold().startswith("microsoft sound mapper"):
                continue
            result.append((index, name))
        return result

    def _callback(self, indata, frames, _time_info, status) -> None:
        if status:
            logger.warning("Microphone status: %s", status)

        data = bytes(indata)
        self._samples_captured += frames
        samples = array("h")
        samples.frombytes(data)
        rms = int(math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples))))

        if self._on_audio is not None:
            self._on_audio(
                AudioChunk(
                    data=data,
                    stream_end_seconds=self._samples_captured / self.sample_rate,
                    captured_at=time.perf_counter(),
                    rms=rms,
                )
            )

    @staticmethod
    def _resolve_device(device: str | int | None) -> int | None:
        if device is None or str(device).strip() == "":
            return None
        if isinstance(device, int) or str(device).strip().isdigit():
            return int(device)

        wanted = str(device).strip().casefold()
        matches = [
            index
            for index, candidate in enumerate(sd.query_devices())
            if candidate["max_input_channels"] > 0
            and wanted in candidate["name"].casefold()
        ]
        if not matches:
            raise RuntimeError(f"No input device name contains: {device}")

        default_input = sd.default.device[0]
        if default_input in matches:
            return int(default_input)
        return matches[0]

    def _close_stream(self, clear_callback: bool) -> None:
        stream, self._stream = self._stream, None
        if clear_callback:
            self._on_audio = None
        if stream is not None:
            stream.stop()
            stream.close()
            logger.info("Microphone stopped")

    def stop(self) -> None:
        self._close_stream(clear_callback=True)
