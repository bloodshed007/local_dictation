from abc import ABC, abstractmethod
from collections.abc import Callable

from ..events import AudioChunk, TranscriptEvent

TranscriptHandler = Callable[[TranscriptEvent], None]
StateHandler = Callable[[str, str], None]


class StreamingSTT(ABC):
    """Provider boundary used by the audio pipeline and UI."""

    @abstractmethod
    def start(self, on_transcript: TranscriptHandler, on_state: StateHandler) -> None:
        pass

    @abstractmethod
    def send_audio(self, chunk: AudioChunk) -> None:
        pass

    def finalize(self) -> None:
        """Finish the current push-to-talk capture without unloading the model."""
        pass

    @abstractmethod
    def stop(self) -> None:
        pass
