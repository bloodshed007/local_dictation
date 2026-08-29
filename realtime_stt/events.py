from dataclasses import dataclass


@dataclass(frozen=True)
class AudioChunk:
    data: bytes
    stream_end_seconds: float
    captured_at: float
    rms: int


@dataclass(frozen=True)
class TranscriptEvent:
    text: str
    is_final: bool
    latency_ms: float | None = None
    first_transcript_ms: float | None = None
