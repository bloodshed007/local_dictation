import ctypes
import logging
from collections.abc import Callable

from pynput import keyboard

logger = logging.getLogger(__name__)


class GlobalHoldHotkey:
    """Reports global key-down/key-up transitions for one hold-to-talk key."""

    def __init__(
        self,
        key_name: str,
        on_down: Callable[[int], None],
        on_up: Callable[[], None],
    ) -> None:
        self.key_name = key_name.lower().strip()
        self._target_key = self._parse_key(self.key_name)
        self._target_vk = getattr(self._target_key, "vk", None)
        if self._target_vk is None:
            self._target_vk = self._target_key.value.vk
        self._on_down = on_down
        self._on_up = on_up
        self._held = False
        self._listener: keyboard.Listener | None = None

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(
            on_press=self._handle_press,
            on_release=self._handle_release,
            win32_event_filter=self._suppress_target_key,
        )
        self._listener.start()
        logger.info("Global hold-to-talk hotkey ready: %s", self.key_name.upper())

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.stop()
        self._held = False

    def _suppress_target_key(self, message, data) -> None:
        if data.vkCode != self._target_vk or self._listener is None:
            return

        # A suppressed Windows event never reaches pynput's normal on_press /
        # on_release callbacks, so dispatch our transition before swallowing it.
        if message in (0x0100, 0x0104):  # WM_KEYDOWN / WM_SYSKEYDOWN
            self._handle_press(self._target_key)
        elif message in (0x0101, 0x0105):  # WM_KEYUP / WM_SYSKEYUP
            self._handle_release(self._target_key)

        # Only the configured dictation key is swallowed; all other global
        # keyboard input is allowed through unchanged.
        self._listener.suppress_event()

    def _handle_press(self, key) -> None:
        if key != self._target_key or self._held:
            return
        self._held = True
        target_window = int(ctypes.windll.user32.GetForegroundWindow())
        logger.info("Hold hotkey pressed; target HWND=%s", target_window)
        self._on_down(target_window)

    def _handle_release(self, key) -> None:
        if key != self._target_key or not self._held:
            return
        self._held = False
        logger.info("Hold hotkey released")
        self._on_up()

    @staticmethod
    def _parse_key(name: str):
        if hasattr(keyboard.Key, name):
            return getattr(keyboard.Key, name)
        if len(name) == 1:
            return keyboard.KeyCode.from_char(name)
        valid = "F1-F20, ctrl_r, ctrl_l, alt_r, alt_l, shift_r, shift_l"
        raise ValueError(f"Unsupported STT_HOLD_KEY={name!r}. Use {valid}.")
