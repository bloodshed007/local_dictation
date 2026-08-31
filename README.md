# Local Push-to-Talk Dictation

A small Windows dictation app modeled on the Speakly interaction:

1. Focus any text field.
2. Hold **F8** and speak.
3. Release **F8**.
4. The bottom overlay shows **Thinking**, then the finalized local transcript is pasted into the previously focused application.

The default engine is **faster-whisper Small on CUDA**. Inference is local; no API key or cloud transcription is used.

## Dictation modes — 2026-08-30

The **Mode** dropdown in the controller selects how the dictation key behaves (saved to `settings.json`):

- **Hold to talk** — the original behavior: hold the key while speaking, release to paste.
- **Toggle** — press once to start, press again to finalize and paste. Best for long dictations.
- **Smart (tap toggles)** — a quick tap (< 350 ms) latches dictation on until the next tap; holding the key still works exactly like push-to-talk.

Other additions from the same session:

- **Live VU meter** — the overlay bars now show real microphone level (RMS) instead of a canned animation, so a silent mic is instantly visible.
- **Animated Thinking** indicator, dark title bar, tray tooltip that shows the active key/mode.
- **Overlay position** — Bottom or Top of the screen, and the overlay follows the monitor of the target window on multi-monitor setups.
- **Paste-failure feedback** — if the target window cannot be focused (admin app, closed window), the app no longer pastes blind; the overlay says the text is on the clipboard.
- **History timestamps + right-click copy** — each entry is time-stamped; right-click for "Copy this entry" / "Copy all".
- **Live tuning** — Speech threshold (RMS) and Silence (ms) can be changed in the UI without restart; saved to `settings.json`, which overrides `.env`.
- **Custom vocabulary** — click **Edit vocabulary** to open the local, git-ignored `vocabulary.txt` (one term per line or comma separated, `#` comments). The generated file includes commented examples; `vocabulary.example.txt` is also included in the repo. Terms are fed to Whisper as an initial prompt and hot-reload on the next dictation. Use it for domain terms, names, acronyms, product names, and tickers.

## Known-good snapshot — 2026-08-29

Verified on a mid-range laptop NVIDIA GPU with a Bluetooth headset microphone:

- F8 press starts capture; release finalizes and pastes.
- First useful partial normally appears in about **0.3–0.5 seconds**.
- Warm inference updates typically take **80–170 ms**.
- Technical test correctly captured NVIDIA CUDA, WebSocket, 320 milliseconds, EBITDA, 250 basis points, `uh`, and INT8; one observed error was `revise` instead of `revised`.
- The recording and Thinking overlay stays bottom-centered and does not take focus.
- Consecutive dictations receive a trailing space so phrases do not join as `coolIt`.
- The app is launched with `pythonw.exe`; no persistent console remains open.
- The controller hides to the Windows system tray while hotkey dictation remains active.
- The microphone can be changed at runtime without unloading the Whisper model.

## Run

Double-click:

```text
Start Local Dictation.vbs
```

This invokes `run.ps1`, which starts `pythonw.exe` without leaving Command Prompt or PowerShell open. Logs are written to `logs/app.log`. Launching it again restores the existing controller instead of starting a second copy.

For first-time setup from PowerShell, **uv is recommended**. It uses the committed lockfile and creates the `.venv` expected by the existing launcher:

```powershell
# Install uv once if needed:
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

git clone https://github.com/bloodshed007/local_dictation.git
cd local_dictation
uv sync --locked
Copy-Item .env.example .env
```

Standard `venv` + pip remains supported as a fallback:

```powershell
git clone https://github.com/bloodshed007/local_dictation.git
cd local_dictation
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

## Running on a new machine

What a fresh clone does and does not set up automatically:

- **Whisper model — automatic.** The first launch downloads `faster-whisper small`
  (~464 MB) once from Hugging Face and caches it. Every later run is fully offline.
  On Windows, HTTPS verification uses the native certificate store so managed or
  corporate root certificates work without disabling SSL.
- **Python packages — one command.** `uv sync --locked` installs the exact
  versions in `uv.lock` and creates `.venv`. The traditional
  `pip install -r requirements.txt` path remains supported.
- **GPU (CUDA) — the one manual step.** CTranslate2 needs CUDA 12 cuBLAS and
  cuDNN 9 DLLs, which are *not* pip-installed by this project. The app looks for
  them in the base Python's `torch/lib` folder, so any of these works:
  - install PyTorch with CUDA in the base Python (`pip install torch --index-url
    https://download.pytorch.org/whl/cu121`), or
  - point `STT_CUDA_DLL_DIR` in `.env` at any folder containing the DLLs, or
  - do nothing: **the app automatically falls back to CPU** (`int8`) with a status
    message if CUDA cannot start. CPU is slower but fully functional. To skip the
    CUDA attempt entirely, set `STT_DEVICE=cpu` and `STT_COMPUTE_TYPE=int8`.
- **Not carried over by design:** `.env` (copy from `.env.example`),
  `settings.json` (created when you click Apply), `vocabulary.txt` (created by
  **Edit vocabulary**), and the optional Vosk model (`download_vosk_model.py`,
  only needed for the experimental Vosk provider).

Wait until the controller says **Ready**, then choose **Minimize to tray** or use the window's minimize/close button. F8 dictation remains active while the controller is hidden. Double-click the tray icon or use **Show Local Dictation** to restore it; the tray menu also provides **Exit**. The recording/Thinking pill does not take focus from the application receiving the text.

## Configuration

The application defaults are:

```dotenv
STT_PROVIDER=faster-whisper
STT_MODEL=small
STT_DEVICE=cuda
STT_COMPUTE_TYPE=float16
STT_LANGUAGE=en
STT_SPEECH_RMS_THRESHOLD=650
STT_HOLD_KEY=f8
```

Choose **F6–F12**, **Right Ctrl**, **Right Alt**, or **Right Shift** in the controller and click **Apply key**. The shortcut changes immediately and is saved in `settings.json` for future launches. The selected key is suppressed system-wide while used for dictation, so it does not also trigger the focused application's normal key action.

> **Keyboard-layout note:** on many non-US layouts, Right Alt is also **AltGr**. Using it as the dictation shortcut prevents AltGr character combinations while the app is running; choose Right Ctrl instead if you rely on AltGr.

Choose a Windows input from the **Microphone** dropdown and click **Apply mic**. The stream switches immediately without unloading faster-whisper, and the device name is saved in `settings.json`. Alternatively, set `STT_MIC_DEVICE` in `.env` to a distinctive device-name substring; leave it blank to use the Windows default at startup.

The first faster-whisper launch downloads the model once. Later runs use the local cache without contacting Hugging Face.

## Architecture

```text
Global F8 key-down
    -> remember the currently focused Windows application
    -> begin forwarding 16 kHz microphone chunks
    -> update non-activating bottom overlay with revisable partial text

Global F8 key-up
    -> stop forwarding microphone audio
    -> request a final decode without unloading the model
    -> show Thinking
    -> copy final text to the Windows clipboard
    -> restore the target window and send Ctrl+V
```

The CUDA model stays loaded between dictations, so each key press does not pay model startup cost. Natural pauses may finalize internal segments, but all segments from one key hold are pasted together only after release.

Provider implementations remain isolated under `realtime_stt/stt/`. Faster-whisper implements the push-to-talk finalization path used by this version. Vosk and Deepgram are retained as earlier experimental adapters, not as the default dictation engine.

### Important hotkey invariant

On Windows, pynput's `suppress_event()` prevents its normal `on_press` and `on_release` callbacks from running. `hotkey.py` therefore dispatches the matching press/release transition inside `win32_event_filter` **before** suppressing the function key. Do not reverse this ordering: doing so makes the UI appear ready while F8 silently starts no recording. This regression was reproduced and fixed on 2026-08-29.

## File map

| Path | Purpose |
|---|---|
| `Start Local Dictation.vbs` | One-click, console-free entry point |
| `run.ps1` | PowerShell launcher, duplicate detection, file-log redirection |
| `pyproject.toml` | Project metadata and dependency declarations for uv |
| `uv.lock` | Reproducible dependency lockfile |
| `.python-version` | Python 3.11 selection for uv |
| `settings.json` | Hold key, mode, overlay position, tuning, microphone; created after Apply |
| `vocabulary.example.txt` | Commented public example for custom vocabulary |
| `vocabulary.txt` | Local, git-ignored vocabulary created by **Edit vocabulary** |
| `.env` | Local engine/CUDA/microphone overrides; ignored by Git |
| `realtime_stt/ui.py` | Controller, microphone/key settings, overlay, clipboard paste |
| `realtime_stt/tray.py` | System-tray icon with Show and Exit actions |
| `realtime_stt/hotkey.py` | Global press-and-hold listener and selective suppression |
| `realtime_stt/pipeline.py` | Microphone gating between key-down and key-up |
| `realtime_stt/stt/faster_whisper_local.py` | Warm CUDA model, partial revisions, forced finalization |
| `logs/app.log` | Primary runtime diagnostic log |

## Troubleshooting

1. **Nothing happens on the hold key:** inspect `logs/app.log`. A healthy press shows `Hold hotkey pressed` followed by `Push-to-talk capture started`.
2. **Press/release appears, but no transcript:** if there is no `Speech started`, check the microphone line at startup. Pin the intended device with `STT_MIC_DEVICE` if Windows changes its default input.
3. **A final transcript appears but is not inserted:** the target may be elevated. Windows blocks a normal process from simulating paste into an Administrator process.
4. **Controller disappeared:** it is probably in the Windows tray overflow (`^`). Double-click the microphone icon or choose **Show Local Dictation**.
5. **App already seems open:** use the tray icon to restore it; the launcher will not start a duplicate.
6. **Clean restart:** choose **Exit** from the tray or controller, then double-click `Start Local Dictation.vbs`.
7. **`uv sync` reports an SSL/certificate error:** retry with the Windows certificate store:

   ```powershell
   uv sync --locked --native-tls
   ```

   This keeps TLS verification enabled. Do not disable SSL globally. On a managed corporate network, ensure the organization's certificate is installed in the Windows trusted certificate store.
8. **The first Whisper model download reports an SSL/certificate error:** the app automatically uses the Windows certificate store through `truststore`. Install the required organizational/root certificate in Windows and retry. The app deliberately never disables certificate verification.

## CUDA note

On Windows, CTranslate2 needs CUDA 12 cuBLAS and cuDNN 9 DLLs. This app automatically detects the base Python installation's `torch/lib` folder. A different folder can be supplied with:

```dotenv
STT_CUDA_DLL_DIR=C:\path\to\cuda\dlls
```

If CUDA startup fails for any reason (missing DLLs, no NVIDIA GPU), the app logs the error and **automatically retries on CPU with `int8`**, so it still works — just with higher latency.

## Current limitations

- Windows blocks simulated paste into an application running as Administrator when this app is not also elevated.
- Final raw Whisper text is pasted without LLM cleanup or rewriting.
- The clipboard is intentionally left containing the dictated text after paste.
