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
        on_cancel: Callable[[], None],
    ) -> None:
        self.key_name = key_name.lower().strip()
        self._target_key = self._parse_key(self.key_name)
        self._target_vk = getattr(self._target_key, "vk", None)
        if self._target_vk is None:
            self._target_vk = self._target_key.value.vk
        self._on_down = on_down
        self._on_up = on_up
        self._on_cancel = on_cancel
        self._escape_vk = keyboard.Key.esc.value.vk
        self._held = False
        self._escape_held = False
        self._cancel_enabled = False
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
        self._escape_held = False
        self._cancel_enabled = False

    def set_cancel_enabled(self, enabled: bool) -> None:
        """Intercept Escape only while a dictation can still be cancelled."""
        self._cancel_enabled = enabled

    def _suppress_target_key(self, message, data) -> None:
        if self._listener is None:
            return

        is_down = message in (0x0100, 0x0104)  # WM_KEYDOWN / WM_SYSKEYDOWN
        is_up = message in (0x0101, 0x0105)  # WM_KEYUP / WM_SYSKEYUP

        if data.vkCode == self._target_vk:
            # A suppressed Windows event never reaches pynput's normal
            # callbacks, so dispatch before swallowing it.
            if is_down:
                self._handle_press(self._target_key)
            elif is_up:
                self._handle_release(self._target_key)
            self._listener.suppress_event()
            return

        if data.vkCode == self._escape_vk and (self._cancel_enabled or self._escape_held):
            if is_down and not self._escape_held:
                self._escape_held = True
                logger.info("Escape pressed; cancelling active dictation")
                self._on_cancel()
            elif is_up:
                self._escape_held = False
            # Escape is swallowed only for the active cancel gesture. At idle,
            # it passes through to the focused application unchanged.
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
