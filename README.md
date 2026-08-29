# Local Push-to-Talk Dictation

A small Windows dictation app modeled on the Speakly interaction:

1. Focus any text field.
2. Hold **F8** and speak.
3. Release **F8**.
4. The bottom overlay shows **Thinking**, then the finalized local transcript is pasted into the previously focused application.

The default engine is **faster-whisper Small on CUDA**. Inference is local; no API key or cloud transcription is used.

## Known-good snapshot — 2026-08-29

Verified on an RTX 4060 Laptop GPU with `Headset (OnePlus Buds 4)`:

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

For first-time setup from PowerShell:

```powershell
git clone https://github.com/bloodshed007/local_dictation.git
cd local_dictation
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

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

Choose F6–F12 directly in the controller and click **Apply key**. The key changes immediately and is saved in `settings.json` for future launches. The selected hold key is suppressed system-wide while used for dictation, so it does not also trigger the focused application's normal function-key action.

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
| `settings.json` | User-selected hold key; created after Apply |
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

## CUDA note

On Windows, CTranslate2 needs CUDA 12 cuBLAS and cuDNN 9 DLLs. This app automatically detects the base Python installation's `torch/lib` folder on this PC. A different folder can be supplied with:

```dotenv
STT_CUDA_DLL_DIR=C:\path\to\cuda\dlls
```

## Current limitations

- Windows blocks simulated paste into an application running as Administrator when this app is not also elevated.
- Final raw Whisper text is pasted without LLM cleanup or rewriting.
- The clipboard is intentionally left containing the dictated text after paste.
