from pathlib import Path
import urllib.request
import zipfile

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODELS_DIR = Path(__file__).resolve().parent / "models"
TARGET = MODELS_DIR / MODEL_NAME
ARCHIVE = MODELS_DIR / f"{MODEL_NAME}.zip"

if TARGET.is_dir():
    print(f"Model already installed: {TARGET}")
else:
    MODELS_DIR.mkdir(exist_ok=True)
    print("Downloading the 40 MB local English model…")
    urllib.request.urlretrieve(MODEL_URL, ARCHIVE)
    print("Extracting…")
    with zipfile.ZipFile(ARCHIVE) as archive:
        archive.extractall(MODELS_DIR)
    ARCHIVE.unlink()
    print(f"Installed: {TARGET}")
