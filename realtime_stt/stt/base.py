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
        """Finish the current capture without unloading the model."""
        pass

    def cancel(self) -> None:
        """Discard the current capture without producing a final transcript."""
        pass

    @abstractmethod
    def stop(self) -> None:
        pass
