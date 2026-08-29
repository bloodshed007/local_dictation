import ctypes
import logging
import queue
import time
import tkinter as tk
from tkinter import ttk

from pynput import keyboard

from .events import TranscriptEvent
from .hotkey import GlobalHoldHotkey
from .pipeline import TranscriptionPipeline
from .settings import AppSettings
from .tray import TrayController

logger = logging.getLogger(__name__)


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
        self._animation_frame = 0
        self._bar_ids: list[int] = []

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
        self._draw(260, text, show_bars=False)
        self._show_without_activation()

    def hide(self) -> None:
        self.mode = "hidden"
        ctypes.windll.user32.ShowWindow(self.hwnd, 0)

    def _draw(self, width: int, text: str, show_bars: bool) -> None:
        self.width = width
        self.canvas.configure(width=width, height=self.HEIGHT)
        self.canvas.delete("all")
        self._bar_ids = []

        self._rounded_rectangle(3, 6, width - 3, self.HEIGHT - 6, 25, "#252525", "#696969")
        text_x = (width - 55) / 2 if show_bars else width / 2
        self.canvas.create_text(
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
            patterns = (
                (8, 15, 22, 13, 7),
                (15, 24, 11, 20, 10),
                (22, 10, 18, 8, 19),
                (11, 19, 8, 24, 14),
            )
            heights = patterns[self._animation_frame % len(patterns)]
            center = self.HEIGHT / 2
            for bar_id, height in zip(self._bar_ids, heights):
                coords = self.canvas.coords(bar_id)
                x = coords[0]
                self.canvas.coords(bar_id, x, center - height / 2, x, center + height / 2)
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

    def _show_without_activation(self) -> None:
        user32 = ctypes.windll.user32
        screen_width = user32.GetSystemMetrics(0)
        screen_height = user32.GetSystemMetrics(1)
        x = (screen_width - self.width) // 2
        y = screen_height - self.HEIGHT - 55
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
    """Small controller plus a global hold-to-talk dictation overlay."""

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
        self.microphone_options: dict[str, int] = {}
        for index, name in self.pipeline.microphone.available_input_devices():
            label = name if name not in self.microphone_options else f"{name} [{index}]"
            self.microphone_options[label] = index

        root.title("Local Dictation")
        root.geometry("760x620")
        root.minsize(660, 540)
        root.configure(bg="#111318")

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
        ttk.Label(controls, text="Hold key", style="Card.TLabel").pack(side="left", padx=(0, 8))
        self.shortcut = tk.StringVar(value=self.hold_key.upper())
        shortcut_picker = ttk.Combobox(
            controls,
            textvariable=self.shortcut,
            values=tuple(f"F{number}" for number in range(6, 13)),
            width=6,
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
        self.history.configure(state="disabled")

        buttons = ttk.Frame(frame, style="App.TFrame")
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Minimize to tray", command=self._hide_to_tray, style="Dark.TButton").pack(side="left")
        ttk.Button(buttons, text="Clear history", command=self._clear_history, style="Dark.TButton").pack(side="left", padx=8)
        ttk.Button(buttons, text="Exit", command=self.close, style="Dark.TButton").pack(side="right")

        self.overlay = DictationOverlay(root)
        self.hotkey = self._new_hotkey(self.hold_key)
        self.hotkey.start()
        self.tray = TrayController(
            lambda: self.messages.put(("tray_show", None)),
            lambda: self.messages.put(("tray_exit", None)),
        )
        self.tray.start()

        root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        root.bind("<Unmap>", self._on_unmap)
        root.after(30, self._drain_messages)
        root.after(100, self._start)

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
        )

    def _apply_shortcut(self) -> None:
        if self.mode != "idle":
            self.status.set("Finish the current dictation before changing the shortcut")
            return
        new_key = self.shortcut.get().strip().lower()
        if new_key == self.hold_key:
            self.status.set(f"Ready — hold {self.hold_key.upper()} in any application")
            return

        old_key = self.hold_key
        try:
            replacement = self._new_hotkey(new_key)
            self.hotkey.stop()
            replacement.start()
        except Exception as exc:
            logger.exception("Could not change hold-to-talk shortcut")
            self.shortcut.set(old_key.upper())
            self.hotkey = self._new_hotkey(old_key)
            self.hotkey.start()
            self.status.set(f"Shortcut error: {exc}")
            return

        self.hotkey = replacement
        self.hold_key = new_key
        self._update_instruction()
        try:
            if self.settings is not None:
                self.settings.set("hold_key", new_key)
            self.status.set(f"Shortcut saved — hold {new_key.upper()} in any application")
            logger.info("Changed and saved hold-to-talk shortcut: %s", new_key.upper())
        except OSError as exc:
            logger.exception("Shortcut changed but could not be saved")
            self.status.set(f"Using {new_key.upper()}, but could not save it: {exc}")

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

    def _update_instruction(self) -> None:
        self.instruction.set(
            f"Hold {self.hold_key.upper()} while speaking. Release to finalize and paste into the focused application."
        )

    def _clear_history(self) -> None:
        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        self.history.configure(state="disabled")

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

            if kind == "transcript":
                self._apply_transcript(payload)  # type: ignore[arg-type]
            elif kind == "state":
                state, detail = payload  # type: ignore[misc]
                self._apply_state(state, detail)
            elif kind == "hotkey_down":
                self._begin_dictation(int(payload))
            elif kind == "hotkey_up":
                self._end_dictation()
            elif kind == "tray_show":
                self._show_from_tray()
            elif kind == "tray_exit":
                self.close()

        self.root.after(30, self._drain_messages)

    def _apply_state(self, state: str, detail: str) -> None:
        if state == "connected":
            self.ready = True
            self.status.set(f"Ready — hold {self.hold_key.upper()} in any application")
        elif state == "capture_finalized":
            self._finish_dictation()
        elif state == "error":
            self.ready = False
            self.mode = "idle"
            self.overlay.show_message("Transcription error")
            self.root.after(1200, self.overlay.hide)
            self.status.set(f"Error: {detail}")
        elif state == "closed":
            self.ready = False
        elif not self.ready:
            self.status.set(detail)

    def _begin_dictation(self, target_window: int) -> None:
        if not self.ready or self.mode != "idle":
            return
        if not self.pipeline.begin_capture():
            return
        self.mode = "listening"
        self.target_window = target_window
        self.final_segments = []
        self.partial_text = ""
        self.status.set("Listening… release the key to paste")
        self.overlay.show_listening()

    def _end_dictation(self) -> None:
        if self.mode != "listening":
            return
        self.mode = "thinking"
        self.thinking_started_at = time.perf_counter()
        self.status.set("Thinking…")
        self.overlay.show_thinking()
        self.pipeline.end_capture()

    def _apply_transcript(self, event: TranscriptEvent) -> None:
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
        if not text:
            self.overlay.show_message("No speech detected")
            self.status.set(f"Ready — hold {self.hold_key.upper()} in any application")
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
            if self.target_window and ctypes.windll.user32.IsWindow(self.target_window):
                ctypes.windll.user32.SetForegroundWindow(self.target_window)
            self.root.after(60, self._send_paste)
        except Exception as exc:
            logger.exception("Could not place dictation on the clipboard")
            self.status.set(f"Paste error: {exc}")
            self.overlay.show_message("Copied text unavailable")
            self.mode = "idle"
            self.root.after(1200, self.overlay.hide)

    def _send_paste(self) -> None:
        try:
            self._paste_controller.press(keyboard.Key.ctrl_l)
            self._paste_controller.press("v")
            self._paste_controller.release("v")
            self._paste_controller.release(keyboard.Key.ctrl_l)
            logger.info("Pasted finalized dictation into HWND=%s", self.target_window)
            self.status.set(f"Ready — hold {self.hold_key.upper()} in any application")
        except Exception as exc:
            logger.exception("Could not send Ctrl+V")
            self.status.set(f"Paste error: {exc}; text remains on the clipboard")
        finally:
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
        self.history.insert("end", text)
        self.history.see("end")
        self.history.configure(state="disabled")

    def close(self) -> None:
        self.ready = False
        self.overlay.hide()
        self.hotkey.stop()
        self.tray.stop()
        try:
            self.pipeline.stop()
        finally:
            self.root.destroy()
