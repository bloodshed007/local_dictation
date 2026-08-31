import ctypes
import ctypes.wintypes
import logging
import os
import queue
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import ttk

from pynput import keyboard

from .events import TranscriptEvent
from .hotkey import GlobalHoldHotkey
from .pipeline import TranscriptionPipeline
from .settings import AppSettings
from .tray import TrayController

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
VOCABULARY_PATH = PROJECT_DIR / "vocabulary.txt"

DICTATION_MODES = {
    "Hold to talk": "hold",
    "Toggle": "toggle",
    "Smart (tap toggles)": "smart",
}
OVERLAY_POSITIONS = {"Bottom": "bottom", "Top": "top"}
SHORTCUT_OPTIONS = {
    **{f"F{number}": f"f{number}" for number in range(6, 13)},
    "Right Ctrl": "ctrl_r",
    "Right Alt": "alt_r",
    "Right Shift": "shift_r",
}

# A tap shorter than this in Smart mode latches dictation on instead of ending it.
SMART_TAP_SECONDS = 0.35


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


def enable_dark_title_bar(window: tk.Misc) -> None:
    """Ask DWM for a dark title bar on Windows 10/11; harmless elsewhere."""
    try:
        window.update_idletasks()
        raw_hwnd = int(window.winfo_id())
        hwnd = int(ctypes.windll.user32.GetParent(raw_hwnd)) or raw_hwnd
        value = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20; 19 pre-20H1)
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                break
    except Exception:
        logger.debug("Could not enable dark title bar", exc_info=True)


class DictationOverlay:
    """Bottom-center status pill that never takes focus from the target app."""

    TRANSPARENT = "#010101"
    HEIGHT = 66

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.configure(bg=self.TRANSPARENT)
        self.window.attributes("-topmost", True)
        try:
            self.window.wm_attributes("-transparentcolor", self.TRANSPARENT)
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(
            self.window,
            width=680,
            height=self.HEIGHT,
            bg=self.TRANSPARENT,
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack()
        self.mode = "hidden"
        self.width = 680
        self.position = "bottom"
        self.target_hwnd = 0
        self._animation_frame = 0
        self._bar_ids: list[int] = []
        self._text_id: int | None = None
        self._levels: deque[int] = deque(maxlen=5)

        self.window.update_idletasks()
        raw_hwnd = int(self.window.winfo_id())
        parent_hwnd = int(ctypes.windll.user32.GetParent(raw_hwnd))
        self.hwnd = parent_hwnd or raw_hwnd
        self._configure_no_activate()
        ctypes.windll.user32.ShowWindow(self.hwnd, 0)
        self.window.after(90, self._animate)

    def show_listening(self, text: str = "") -> None:
        self.mode = "listening"
        display = text[-82:].strip() if text else "Listening…"
        self._draw(680, display, show_bars=True)
        self._show_without_activation()

    def show_thinking(self) -> None:
        self.mode = "thinking"
        self._draw(160, "Thinking", show_bars=False)
        self._show_without_activation()

    def show_message(self, text: str) -> None:
        self.mode = "message"
        self._draw(320, text, show_bars=False)
        self._show_without_activation()

    def set_level(self, rms: int) -> None:
        """Feed a live microphone RMS sample to drive the VU bars."""
        self._levels.append(int(rms))

    def hide(self) -> None:
        self.mode = "hidden"
        self._levels.clear()
        ctypes.windll.user32.ShowWindow(self.hwnd, 0)

    def _draw(self, width: int, text: str, show_bars: bool) -> None:
        self.width = width
        self.canvas.configure(width=width, height=self.HEIGHT)
        self.canvas.delete("all")
        self._bar_ids = []

        self._rounded_rectangle(3, 6, width - 3, self.HEIGHT - 6, 25, "#252525", "#696969")
        text_x = (width - 55) / 2 if show_bars else width / 2
        self._text_id = self.canvas.create_text(
            text_x,
            self.HEIGHT / 2,
            text=text,
            fill="#f1f1f1" if self.mode == "listening" else "#c7c7c7",
            font=("Segoe UI", 12, "normal"),
            width=width - (115 if show_bars else 30),
            anchor="center",
        )

        if show_bars:
            for index in range(5):
                x = width - 54 + index * 7
                self._bar_ids.append(
                    self.canvas.create_line(
                        x,
                        25,
                        x,
                        41,
                        fill="#ffffff",
                        width=3,
                        capstyle="round",
                    )
                )

    def _rounded_rectangle(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        radius: int,
        fill: str,
        outline: str,
    ) -> None:
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        self.canvas.create_polygon(
            points,
            smooth=True,
            splinesteps=24,
            fill=fill,
            outline=outline,
            width=1,
        )

    def _animate(self) -> None:
        if self.mode == "listening" and self._bar_ids:
            center = self.HEIGHT / 2
            levels = list(self._levels)
            if levels:
                # Real VU meter: newest sample on the right, 5 px floor.
                heights = [5 + min(21, level // 150) for level in levels]
                heights = [5] * (5 - len(heights)) + heights
            else:
                patterns = (
                    (8, 15, 22, 13, 7),
                    (15, 24, 11, 20, 10),
                    (22, 10, 18, 8, 19),
                    (11, 19, 8, 24, 14),
                )
                heights = list(patterns[self._animation_frame % len(patterns)])
            for bar_id, height in zip(self._bar_ids, heights):
                coords = self.canvas.coords(bar_id)
                x = coords[0]
                self.canvas.coords(bar_id, x, center - height / 2, x, center + height / 2)
            self._animation_frame += 1
        elif self.mode == "thinking" and self._text_id is not None:
            dots = "." * (self._animation_frame % 4)
            self.canvas.itemconfigure(self._text_id, text=f"Thinking{dots}")
            self._animation_frame += 1
        self.window.after(90, self._animate)

    def _configure_no_activate(self) -> None:
        user32 = ctypes.windll.user32
        gwl_exstyle = -20
        ws_ex_toolwindow = 0x00000080
        ws_ex_noactivate = 0x08000000
        style = user32.GetWindowLongW(self.hwnd, gwl_exstyle)
        user32.SetWindowLongW(
            self.hwnd,
            gwl_exstyle,
            style | ws_ex_toolwindow | ws_ex_noactivate,
        )

    def _work_area(self) -> tuple[int, int, int, int]:
        """Work area of the monitor hosting the target window (or primary)."""
        user32 = ctypes.windll.user32
        if self.target_hwnd and user32.IsWindow(self.target_hwnd):
            monitor_default_to_nearest = 2
            monitor = user32.MonitorFromWindow(self.target_hwnd, monitor_default_to_nearest)
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                work = info.rcWork
                return work.left, work.top, work.right, work.bottom
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

    def _show_without_activation(self) -> None:
        user32 = ctypes.windll.user32
        left, top, right, bottom = self._work_area()
        x = left + (right - left - self.width) // 2
        if self.position == "top":
            y = top + 55
        else:
            y = bottom - self.HEIGHT - 55
        # Keep Tk's own requested geometry aligned with the native HWND so its
        # geometry manager does not move the restored overlay back to (0, 0).
        self.window.geometry(f"{self.width}x{self.HEIGHT}+{x}+{y}")
        self.window.update_idletasks()
        hwnd_topmost = -1
        swp_noactivate = 0x0010
        swp_showwindow = 0x0040
        # Restores this top-level if Windows minimized it with the controller,
        # while WS_EX_NOACTIVATE preserves focus in the target application.
        user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        user32.SetWindowPos(
            self.hwnd,
            hwnd_topmost,
            x,
            y,
            self.width,
            self.HEIGHT,
            swp_noactivate | swp_showwindow,
        )


class TranscriptWindow:
    """Small controller plus a global hold/toggle dictation overlay."""

    def __init__(
        self,
        root: tk.Tk,
        pipeline: TranscriptionPipeline,
        hold_key: str = "f8",
        settings: AppSettings | None = None,
    ) -> None:
        self.root = root
        self.pipeline = pipeline
        self.settings = settings
        self.hold_key = hold_key.lower()
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.final_segments: list[str] = []
        self.partial_text = ""
        self.ready = False
        self.mode = "idle"
        self.target_window = 0
        self.thinking_started_at = 0.0
        self._paste_controller = keyboard.Controller()
        self._latched = False
        self._key_down_at = 0.0
        self._entry_count = 0
        self._entries: dict[str, str] = {}
        self.microphone_options: dict[str, int] = {}
        for index, name in self.pipeline.microphone.available_input_devices():
            label = name if name not in self.microphone_options else f"{name} [{index}]"
            self.microphone_options[label] = index

        saved = settings.load() if settings is not None else {}
        self.dictation_mode = saved.get("dictation_mode", "hold")
        if self.dictation_mode not in DICTATION_MODES.values():
            self.dictation_mode = "hold"
        overlay_position = saved.get("overlay_position", "bottom")
        if overlay_position not in OVERLAY_POSITIONS.values():
            overlay_position = "bottom"

        root.title("Local Dictation")
        root.geometry("760x680")
        root.minsize(660, 590)
        root.configure(bg="#111318")
        enable_dark_title_bar(root)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#111318")
        style.configure("Card.TFrame", background="#1c2028")
        style.configure("Title.TLabel", background="#111318", foreground="#f4f7fb", font=("Segoe UI", 22, "bold"))
        style.configure("Body.TLabel", background="#111318", foreground="#aeb6c2", font=("Segoe UI", 10))
        style.configure("Card.TLabel", background="#1c2028", foreground="#dfe5ed", font=("Segoe UI", 10))
        style.configure("Status.TLabel", background="#1c2028", foreground="#72e0a8", font=("Segoe UI", 11, "bold"))
        style.configure(
            "Dark.TButton",
            font=("Segoe UI", 10),
            padding=(12, 7),
            background="#2b3340",
            foreground="#f0f3f7",
            bordercolor="#3c4656",
        )
        style.map("Dark.TButton", background=[("active", "#39465a")])
        style.configure(
            "Dark.TCheckbutton",
            background="#1c2028",
            foreground="#dfe5ed",
            font=("Segoe UI", 10),
        )
        style.map(
            "Dark.TCheckbutton",
            background=[("active", "#1c2028")],
            foreground=[("active", "#f4f7fb")],
        )
        style.configure("Dark.TCombobox", fieldbackground="#282e39", foreground="#f4f7fb", padding=5)
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", "#2b3340")],
            foreground=[("readonly", "#f4f7fb")],
            selectbackground=[("readonly", "#2b3340")],
            selectforeground=[("readonly", "#f4f7fb")],
        )

        frame = ttk.Frame(root, padding=22, style="App.TFrame")
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Local Dictation", style="Title.TLabel").pack(anchor="w")
        self.instruction = tk.StringVar()
        self._update_instruction()
        ttk.Label(
            frame,
            textvariable=self.instruction,
            wraplength=660,
            style="Body.TLabel",
        ).pack(anchor="w", pady=(5, 16))

        card = ttk.Frame(frame, padding=14, style="Card.TFrame")
        card.pack(fill="x")
        self.status = tk.StringVar(value="Starting local model…")
        self.status_label = ttk.Label(card, textvariable=self.status, style="Status.TLabel")
        self.status_label.pack(anchor="w", fill="x")

        controls = ttk.Frame(card, style="Card.TFrame")
        controls.pack(fill="x", pady=(12, 0))
        ttk.Label(controls, text="Shortcut", style="Card.TLabel").pack(side="left", padx=(0, 8))
        self.shortcut = tk.StringVar(value=self._shortcut_label())
        shortcut_picker = ttk.Combobox(
            controls,
            textvariable=self.shortcut,
            values=tuple(SHORTCUT_OPTIONS),
            width=11,
            state="readonly",
            style="Dark.TCombobox",
        )
        shortcut_picker.pack(side="left")
        ttk.Button(
            controls,
            text="Apply key",
            command=self._apply_shortcut,
            style="Dark.TButton",
        ).pack(side="left", padx=(8, 20))

        ttk.Label(controls, text="Microphone", style="Card.TLabel").pack(side="left", padx=(0, 8))
        current_microphone = self.pipeline.microphone.current_device_name()
        selected_label = next(
            (label for label in self.microphone_options if label.startswith(current_microphone)),
            current_microphone,
        )
        self.microphone = tk.StringVar(value=selected_label)
        microphone_picker = ttk.Combobox(
            controls,
            textvariable=self.microphone,
            values=tuple(self.microphone_options),
            width=29,
            state="readonly",
            style="Dark.TCombobox",
        )
        microphone_picker.pack(side="left", fill="x", expand=True)
        ttk.Button(
            controls,
            text="Apply mic",
            command=self._apply_microphone,
            style="Dark.TButton",
        ).pack(side="left", padx=(8, 0))

        modes = ttk.Frame(card, style="Card.TFrame")
        modes.pack(fill="x", pady=(10, 0))
        ttk.Label(modes, text="Mode", style="Card.TLabel").pack(side="left", padx=(0, 8))
        mode_label = next(
            (label for label, value in DICTATION_MODES.items() if value == self.dictation_mode),
            "Hold to talk",
        )
        self.mode_choice = tk.StringVar(value=mode_label)
        mode_picker = ttk.Combobox(
            modes,
            textvariable=self.mode_choice,
            values=tuple(DICTATION_MODES),
            width=18,
            state="readonly",
            style="Dark.TCombobox",
        )
        mode_picker.pack(side="left")
        mode_picker.bind("<<ComboboxSelected>>", self._apply_mode)

        ttk.Label(modes, text="Overlay", style="Card.TLabel").pack(side="left", padx=(20, 8))
        position_label = next(
            (label for label, value in OVERLAY_POSITIONS.items() if value == overlay_position),
            "Bottom",
        )
        self.position_choice = tk.StringVar(value=position_label)
        position_picker = ttk.Combobox(
            modes,
            textvariable=self.position_choice,
            values=tuple(OVERLAY_POSITIONS),
            width=8,
            state="readonly",
            style="Dark.TCombobox",
        )
        position_picker.pack(side="left")
        position_picker.bind("<<ComboboxSelected>>", self._apply_overlay_position)

        ttk.Button(
            modes,
            text="Edit vocabulary",
            command=self._edit_vocabulary,
            style="Dark.TButton",
        ).pack(side="right")
        self.release_mic_var = tk.BooleanVar(
            value=self.pipeline.release_microphone_when_idle
        )
        ttk.Checkbutton(
            modes,
            text="Release mic while idle",
            variable=self.release_mic_var,
            command=self._apply_mic_behavior,
            style="Dark.TCheckbutton",
        ).pack(side="right", padx=(8, 16))

        tuning = ttk.Frame(card, style="Card.TFrame")
        tuning.pack(fill="x", pady=(10, 0))
        ttk.Label(tuning, text="Speech threshold", style="Card.TLabel").pack(side="left", padx=(0, 8))
        self.threshold_var = tk.StringVar(
            value=str(getattr(self.pipeline.stt, "speech_rms_threshold", 650))
        )
        self._dark_entry(tuning, self.threshold_var, width=7).pack(side="left")
        ttk.Label(tuning, text="Silence (ms)", style="Card.TLabel").pack(side="left", padx=(20, 8))
        self.silence_var = tk.StringVar(
            value=str(getattr(self.pipeline.stt, "final_silence_ms", 700))
        )
        self._dark_entry(tuning, self.silence_var, width=7).pack(side="left")
        ttk.Button(
            tuning,
            text="Apply tuning",
            command=self._apply_tuning,
            style="Dark.TButton",
        ).pack(side="left", padx=(12, 0))

        ttk.Label(frame, text="Recent dictation", style="Body.TLabel").pack(anchor="w", pady=(18, 6))
        self.history = tk.Text(
            frame,
            wrap="word",
            height=11,
            font=("Segoe UI", 12),
            padx=13,
            pady=12,
            borderwidth=0,
            bg="#1c2028",
            fg="#e6ebf2",
            insertbackground="#ffffff",
            selectbackground="#365f9d",
        )
        self.history.pack(fill="both", expand=True)
        self.history.tag_configure("ts", foreground="#6a7382", font=("Segoe UI", 9))
        self.history.configure(state="disabled")
        self.history.bind("<Button-3>", self._show_history_menu)

        buttons = ttk.Frame(frame, style="App.TFrame")
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Minimize to tray", command=self._hide_to_tray, style="Dark.TButton").pack(side="left")
        ttk.Button(buttons, text="Clear history", command=self._clear_history, style="Dark.TButton").pack(side="left", padx=8)
        ttk.Button(buttons, text="Exit", command=self.close, style="Dark.TButton").pack(side="right")

        self.overlay = DictationOverlay(root)
        self.overlay.position = overlay_position
        self.pipeline.on_level = lambda rms: self.messages.put(("level", rms))
        self.hotkey = self._new_hotkey(self.hold_key)
        self.hotkey.start()
        self.tray = TrayController(
            lambda: self.messages.put(("tray_show", None)),
            lambda: self.messages.put(("tray_exit", None)),
            tooltip=f"Local Dictation — {self._ready_hint()}",
        )
        self.tray.start()

        root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        root.bind("<Unmap>", self._on_unmap)
        root.after(30, self._drain_messages)
        root.after(100, self._start)

    @staticmethod
    def _dark_entry(parent: tk.Misc, variable: tk.StringVar, width: int) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            width=width,
            font=("Segoe UI", 10),
            bg="#282e39",
            fg="#f4f7fb",
            insertbackground="#f4f7fb",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#3c4656",
            highlightcolor="#72e0a8",
        )

    def _start(self) -> None:
        try:
            self.pipeline.start(self.on_transcript, self.on_state)
        except Exception as exc:
            logger.exception("Could not start transcription")
            self.status.set(f"Error: {exc}")

    def _new_hotkey(self, key_name: str) -> GlobalHoldHotkey:
        return GlobalHoldHotkey(
            key_name,
            lambda hwnd: self.messages.put(("hotkey_down", hwnd)),
            lambda: self.messages.put(("hotkey_up", None)),
            lambda: self.messages.put(("cancel", None)),
        )

    def _shortcut_label(self, key_name: str | None = None) -> str:
        key = key_name or self.hold_key
        return next(
            (label for label, value in SHORTCUT_OPTIONS.items() if value == key),
            key.upper(),
        )

    def _ready_hint(self) -> str:
        key = self._shortcut_label()
        if self.dictation_mode == "toggle":
            return f"press {key} to start/stop"
        if self.dictation_mode == "smart":
            return f"tap or hold {key}"
        return f"hold {key} in any application"

    def _set_ready_status(self) -> None:
        self.status.set(f"Ready — {self._ready_hint()}")

    def _update_tray_tooltip(self) -> None:
        self.tray.set_tooltip(f"Local Dictation — {self._ready_hint()}")

    def _apply_shortcut(self) -> None:
        if self.mode != "idle":
            self.status.set("Finish the current dictation before changing the shortcut")
            return
        selected = self.shortcut.get().strip()
        new_key = SHORTCUT_OPTIONS.get(selected, selected.lower())
        if new_key == self.hold_key:
            self._set_ready_status()
            return

        old_key = self.hold_key
        try:
            replacement = self._new_hotkey(new_key)
            self.hotkey.stop()
            replacement.start()
        except Exception as exc:
            logger.exception("Could not change hold-to-talk shortcut")
            self.shortcut.set(self._shortcut_label(old_key))
            self.hotkey = self._new_hotkey(old_key)
            self.hotkey.start()
            self.status.set(f"Shortcut error: {exc}")
            return

        self.hotkey = replacement
        self.hold_key = new_key
        self._update_instruction()
        self._update_tray_tooltip()
        try:
            if self.settings is not None:
                self.settings.set("hold_key", new_key)
            self.status.set(f"Shortcut saved — {self._ready_hint()}")
            logger.info("Changed and saved dictation shortcut: %s", self._shortcut_label(new_key))
        except OSError as exc:
            logger.exception("Shortcut changed but could not be saved")
            self.status.set(f"Using {self._shortcut_label(new_key)}, but could not save it: {exc}")

    def _apply_microphone(self) -> None:
        if self.mode != "idle":
            self.status.set("Release the dictation key before changing microphone")
            return
        label = self.microphone.get()
        device_index = self.microphone_options.get(label)
        if device_index is None:
            self.status.set("Select a valid microphone")
            return

        try:
            name = self.pipeline.change_microphone(device_index)
            if self.settings is not None:
                self.settings.set("microphone", name)
            self.status.set(f"Microphone saved — {name}")
            logger.info("Changed and saved microphone: %s", name)
        except Exception as exc:
            logger.exception("Could not change microphone")
            current = self.pipeline.microphone.current_device_name()
            current_label = next(
                (option for option in self.microphone_options if option.startswith(current)),
                current,
            )
            self.microphone.set(current_label)
            self.status.set(f"Microphone error: {exc}")

    def _apply_mic_behavior(self) -> None:
        enabled = bool(self.release_mic_var.get())
        if self.mode != "idle":
            self.release_mic_var.set(self.pipeline.release_microphone_when_idle)
            self.status.set("Finish the current dictation before changing mic behavior")
            return
        try:
            self.pipeline.set_release_microphone_when_idle(enabled)
            if self.settings is not None:
                self.settings.set(
                    "release_microphone_when_idle",
                    "true" if enabled else "false",
                )
            if enabled:
                self.status.set("Microphone will be released while idle")
            else:
                self.status.set("Microphone will stay active for lowest latency")
        except Exception as exc:
            logger.exception("Could not change microphone idle behavior")
            self.release_mic_var.set(self.pipeline.release_microphone_when_idle)
            self.status.set(f"Microphone behavior error: {exc}")

    def _apply_mode(self, _event=None) -> None:
        value = DICTATION_MODES.get(self.mode_choice.get(), "hold")
        if value == self.dictation_mode:
            return
        self.dictation_mode = value
        self._latched = False
        self._update_instruction()
        self._update_tray_tooltip()
        if self.settings is not None:
            try:
                self.settings.set("dictation_mode", value)
            except OSError:
                logger.exception("Could not save dictation mode")
        if self.ready and self.mode == "idle":
            self._set_ready_status()
        logger.info("Dictation mode set to: %s", value)

    def _apply_overlay_position(self, _event=None) -> None:
        value = OVERLAY_POSITIONS.get(self.position_choice.get(), "bottom")
        self.overlay.position = value
        if self.settings is not None:
            try:
                self.settings.set("overlay_position", value)
            except OSError:
                logger.exception("Could not save overlay position")
        logger.info("Overlay position set to: %s", value)

    def _apply_tuning(self) -> None:
        try:
            threshold = int(self.threshold_var.get().strip())
            silence = int(self.silence_var.get().strip())
            if threshold < 50 or silence < 200:
                raise ValueError("out of range")
        except ValueError:
            self.status.set("Tuning must be numbers — threshold ≥ 50, silence ≥ 200 ms")
            return

        stt = self.pipeline.stt
        if hasattr(stt, "speech_rms_threshold"):
            stt.speech_rms_threshold = threshold
        if hasattr(stt, "final_silence_ms"):
            stt.final_silence_ms = silence
        if self.settings is not None:
            try:
                self.settings.set("speech_rms_threshold", str(threshold))
                self.settings.set("final_silence_ms", str(silence))
            except OSError:
                logger.exception("Could not save tuning")
        self.status.set(f"Tuning saved — threshold {threshold}, silence {silence} ms")
        logger.info("Tuning applied: threshold=%d silence_ms=%d", threshold, silence)

    def _edit_vocabulary(self) -> None:
        try:
            if not VOCABULARY_PATH.exists():
                VOCABULARY_PATH.write_text(
                    "# Custom vocabulary for dictation.\n"
                    "# Add one term per line, or separate terms with commas.\n"
                    "# Lines starting with # are examples/comments and are ignored.\n"
                    "# Changes apply automatically on the next dictation.\n"
                    "#\n"
                    "# Examples — remove the leading # and edit for your own vocabulary:\n"
                    "# NVIDIA\n"
                    "# CUDA, EBITDA, PostgreSQL\n"
                    "# Acme Corporation\n"
                    "# Jane Doe\n",
                    encoding="utf-8",
                )
            os.startfile(VOCABULARY_PATH)
            self.status.set("Vocabulary opened — edits apply on the next dictation")
        except Exception as exc:
            logger.exception("Could not open vocabulary file")
            self.status.set(f"Vocabulary error: {exc}")

    def _update_instruction(self) -> None:
        key = self._shortcut_label()
        if self.dictation_mode == "toggle":
            text = (
                f"Press {key} to start dictating; press it again to finalize "
                "and paste into the focused application."
            )
        elif self.dictation_mode == "smart":
            text = (
                f"Tap {key} to toggle dictation on and off, or hold it like "
                "push-to-talk. Text pastes into the focused application."
            )
        else:
            text = (
                f"Hold {key} while speaking. Release to finalize and paste "
                "into the focused application."
            )
        self.instruction.set(f"{text} Press Escape to cancel an active dictation.")

    def _clear_history(self) -> None:
        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        self.history.configure(state="disabled")
        self._entries.clear()
        self._entry_count = 0

    def on_transcript(self, event: TranscriptEvent) -> None:
        self.messages.put(("transcript", event))

    def on_state(self, state: str, detail: str) -> None:
        self.messages.put(("state", (state, detail)))

    def _drain_messages(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break

            try:
                if kind == "transcript":
                    self._apply_transcript(payload)  # type: ignore[arg-type]
                elif kind == "state":
                    state, detail = payload  # type: ignore[misc]
                    self._apply_state(state, detail)
                elif kind == "hotkey_down":
                    self._on_hotkey_down(int(payload))
                elif kind == "hotkey_up":
                    self._on_hotkey_up()
                elif kind == "cancel":
                    self._cancel_dictation()
                elif kind == "level":
                    self.overlay.set_level(int(payload))
                elif kind == "tray_show":
                    self._show_from_tray()
                elif kind == "tray_exit":
                    self.close()
            except Exception:
                # One bad event must never kill the UI loop.
                logger.exception("UI message handler failed for %r", kind)

        self.root.after(30, self._drain_messages)

    def _apply_state(self, state: str, detail: str) -> None:
        if state == "connected":
            self.ready = True
            self._set_ready_status()
        elif state == "capture_finalized":
            if self.mode == "cancelled":
                self._complete_cancel()
            else:
                self._finish_dictation()
        elif state == "capture_cancelled":
            if self.mode == "cancelled":
                self._complete_cancel()
        elif state == "error":
            self.ready = False
            self.mode = "idle"
            self._latched = False
            self.hotkey.set_cancel_enabled(False)
            self.overlay.show_message("Transcription error")
            self.root.after(1200, self.overlay.hide)
            self.status.set(f"Error: {detail}")
        elif state == "closed":
            self.ready = False
            self.hotkey.set_cancel_enabled(False)
        elif not self.ready:
            self.status.set(detail)

    def _on_hotkey_down(self, target_window: int) -> None:
        if self.mode == "listening":
            # Second press ends a toggled/latched dictation.
            if self.dictation_mode == "toggle" or (
                self.dictation_mode == "smart" and self._latched
            ):
                self._latched = False
                self._end_dictation()
            return
        if self.mode != "idle":
            return
        self._key_down_at = time.perf_counter()
        self._latched = False
        self._begin_dictation(target_window)

    def _on_hotkey_up(self) -> None:
        if self.mode != "listening":
            return
        if self.dictation_mode == "hold":
            self._end_dictation()
        elif self.dictation_mode == "smart":
            held_seconds = time.perf_counter() - self._key_down_at
            if held_seconds < SMART_TAP_SECONDS:
                self._latched = True
                self.status.set(f"Listening… tap {self._shortcut_label()} to finish")
            else:
                self._end_dictation()
        # Toggle mode ignores key-up entirely.

    def _begin_dictation(self, target_window: int) -> None:
        if not self.ready or self.mode != "idle":
            return
        try:
            if not self.pipeline.begin_capture():
                return
        except Exception as exc:
            logger.exception("Could not start microphone capture")
            self.status.set(f"Microphone reconnect failed: {exc}")
            self.overlay.show_message("Microphone unavailable")
            self.root.after(1600, self.overlay.hide)
            return
        self.mode = "listening"
        self.hotkey.set_cancel_enabled(True)
        self.target_window = target_window
        self.overlay.target_hwnd = target_window
        self.final_segments = []
        self.partial_text = ""
        key = self._shortcut_label()
        if self.dictation_mode == "toggle":
            self.status.set(f"Listening… press {key} again to paste")
        elif self.dictation_mode == "smart":
            self.status.set(f"Listening… release or tap {key} again")
        else:
            self.status.set("Listening… release the key to paste")
        self.overlay.show_listening()

    def _end_dictation(self) -> None:
        if self.mode != "listening":
            return
        self.mode = "thinking"
        self._latched = False
        self.thinking_started_at = time.perf_counter()
        self.status.set("Thinking…")
        self.overlay.show_thinking()
        self.pipeline.end_capture()

    def _cancel_dictation(self) -> None:
        if self.mode not in ("listening", "thinking"):
            return
        was_listening = self.mode == "listening"
        self.mode = "cancelled"
        self._latched = False
        self.hotkey.set_cancel_enabled(False)
        self.final_segments = []
        self.partial_text = ""
        self.overlay.show_message("Dictation cancelled")
        self.status.set("Cancelling dictation…")
        if was_listening:
            if not self.pipeline.cancel_capture():
                self._complete_cancel()
        else:
            # Finalization may already have posted its completion state. The
            # state normally completes cancellation; this prevents a race from
            # leaving the UI stuck if it arrived just before Escape.
            self.root.after(
                1000,
                lambda: self._complete_cancel() if self.mode == "cancelled" else None,
            )
        logger.info("Active dictation cancelled by Escape")

    def _complete_cancel(self) -> None:
        if self.mode != "cancelled":
            return
        self.mode = "idle"
        self._set_ready_status()
        self.root.after(700, self.overlay.hide)

    def _apply_transcript(self, event: TranscriptEvent) -> None:
        if self.mode not in ("listening", "thinking"):
            return
        if event.is_final:
            if event.text.strip():
                self.final_segments.append(event.text.strip())
            self.partial_text = ""
        else:
            self.partial_text = event.text.strip()

        if self.mode == "listening":
            self.overlay.show_listening(self._visible_text())

    def _finish_dictation(self) -> None:
        if self.mode != "thinking":
            return
        text = self._visible_text().strip()
        minimum_thinking_ms = 250
        elapsed_ms = (time.perf_counter() - self.thinking_started_at) * 1000.0
        delay_ms = max(0, int(minimum_thinking_ms - elapsed_ms))
        self.root.after(delay_ms, lambda: self._paste_or_report(text))

    def _paste_or_report(self, text: str) -> None:
        # Escape may cancel during the minimum Thinking display delay.
        if self.mode != "thinking":
            return
        # From here the paste is committed; the remaining focus/paste window is
        # only ~60 ms and Escape should return to its normal application use.
        self.hotkey.set_cancel_enabled(False)
        if not text:
            self.overlay.show_message("No speech detected")
            self._set_ready_status()
            self.mode = "idle"
            self.root.after(900, self.overlay.hide)
            return

        self._append_history(text)
        try:
            self.root.clipboard_clear()
            # Leave the caret ready for the next push-to-talk phrase; without
            # this, consecutive dictations become "coolIt" or "fastOkay".
            self.root.clipboard_append(text.rstrip() + " ")
            self.root.update_idletasks()
        except Exception as exc:
            logger.exception("Could not place dictation on the clipboard")
            self.status.set(f"Paste error: {exc}")
            self.overlay.show_message("Copied text unavailable")
            self.mode = "idle"
            self.root.after(1200, self.overlay.hide)
            return

        user32 = ctypes.windll.user32
        focused = False
        if self.target_window and user32.IsWindow(self.target_window):
            focused = bool(user32.SetForegroundWindow(self.target_window))
        if not focused:
            # Elevated (admin) apps and closed windows land here; don't paste
            # blind into whatever happens to have focus.
            logger.warning("Could not focus target HWND=%s; paste skipped", self.target_window)
            self.overlay.show_message("Paste blocked — text is on the clipboard")
            self.status.set(
                "Could not focus the target window (admin app?). "
                "Text is on the clipboard — paste it manually."
            )
            self.mode = "idle"
            self.root.after(1600, self.overlay.hide)
            return

        self.root.after(60, self._send_paste)

    def _send_paste(self) -> None:
        try:
            self._paste_controller.press(keyboard.Key.ctrl_l)
            self._paste_controller.press("v")
            self._paste_controller.release("v")
            self._paste_controller.release(keyboard.Key.ctrl_l)
            logger.info("Pasted finalized dictation into HWND=%s", self.target_window)
            self._set_ready_status()
        except Exception as exc:
            logger.exception("Could not send Ctrl+V")
            self.status.set(f"Paste error: {exc}; text remains on the clipboard")
        finally:
            self.hotkey.set_cancel_enabled(False)
            self.mode = "idle"
            self.overlay.hide()

    def _visible_text(self) -> str:
        text = " ".join(self.final_segments)
        if self.partial_text:
            text = f"{text} {self.partial_text}".strip()
        return text

    def _on_unmap(self, event) -> None:
        if event.widget is self.root:
            self.root.after(10, self._hide_if_iconic)

    def _hide_if_iconic(self) -> None:
        if self.root.state() == "iconic":
            self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        self.root.withdraw()
        logger.info("Controller hidden to system tray")

    def _show_from_tray(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.after(50, self.root.focus_force)
        logger.info("Controller restored from system tray")

    def _append_history(self, text: str) -> None:
        self.history.configure(state="normal")
        if self.history.get("1.0", "end-1c"):
            self.history.insert("end", "\n\n")
        tag = f"entry{self._entry_count}"
        self._entry_count += 1
        self._entries[tag] = text
        self.history.insert("end", time.strftime("%H:%M") + "  ", ("ts",))
        self.history.insert("end", text, (tag,))
        self.history.see("end")
        self.history.configure(state="disabled")

    def _show_history_menu(self, event) -> None:
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg="#1c2028",
            fg="#e6ebf2",
            activebackground="#365f9d",
            activeforeground="#ffffff",
            relief="flat",
        )
        index = self.history.index(f"@{event.x},{event.y}")
        entry_tags = [t for t in self.history.tag_names(index) if t.startswith("entry")]
        if entry_tags:
            entry_text = self._entries.get(entry_tags[0], "")
            if entry_text:
                menu.add_command(
                    label="Copy this entry",
                    command=lambda t=entry_text: self._copy_text(t),
                )
        if self._entries:
            menu.add_command(
                label="Copy all",
                command=lambda: self._copy_text("\n\n".join(self._entries.values())),
            )
        if menu.index("end") is not None:
            menu.tk_popup(event.x_root, event.y_root)

    def _copy_text(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status.set("Copied to clipboard")
        except Exception as exc:
            logger.exception("Could not copy history text")
            self.status.set(f"Copy error: {exc}")

    def close(self) -> None:
        self.ready = False
        self.overlay.hide()
        self.hotkey.stop()
        self.tray.stop()
        try:
            self.pipeline.stop()
        finally:
            self.root.destroy()
