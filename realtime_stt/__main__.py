import logging
import os
import tkinter as tk
from pathlib import Path

from dotenv import load_dotenv

from .audio import MicrophoneCapture
from .pipeline import TranscriptionPipeline
from .settings import AppSettings
from .ui import TranscriptWindow

PROJECT_DIR = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_DIR / "settings.json"
VOSK_MODEL_DIR = PROJECT_DIR / "models" / "vosk-model-small-en-us-0.15"


def build_provider(sample_rate: int):
    provider = os.getenv("STT_PROVIDER", "faster-whisper").strip().lower()
    if provider == "faster-whisper":
        from .stt.faster_whisper_local import FasterWhisperStreamingSTT

        return FasterWhisperStreamingSTT(
            sample_rate=sample_rate,
            model_name=os.getenv("STT_MODEL", "small"),
            device=os.getenv("STT_DEVICE", "cuda"),
            compute_type=os.getenv("STT_COMPUTE_TYPE", "float16"),
            language=os.getenv("STT_LANGUAGE", "en"),
            speech_rms_threshold=int(os.getenv("STT_SPEECH_RMS_THRESHOLD", "650")),
        )
    if provider == "vosk":
        from .stt.vosk_local import VoskStreamingSTT

        return VoskStreamingSTT(model_path=VOSK_MODEL_DIR, sample_rate=sample_rate)
    if provider == "deepgram":
        from .stt.deepgram import DeepgramStreamingSTT

        return DeepgramStreamingSTT(sample_rate=sample_rate)
    raise RuntimeError(f"Unsupported STT_PROVIDER: {provider}")


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    settings = AppSettings(SETTINGS_PATH)
    saved_settings = settings.load()

    sample_rate = 16_000
    microphone = MicrophoneCapture(
        sample_rate=sample_rate,
        chunk_ms=50,
        device=saved_settings.get("microphone", os.getenv("STT_MIC_DEVICE", "")) or None,
    )
    pipeline = TranscriptionPipeline(microphone, build_provider(sample_rate))

    root = tk.Tk()
    hold_key = saved_settings.get("hold_key", os.getenv("STT_HOLD_KEY", "f8"))
    TranscriptWindow(root, pipeline, hold_key=hold_key, settings=settings)
    root.mainloop()


if __name__ == "__main__":
    main()
