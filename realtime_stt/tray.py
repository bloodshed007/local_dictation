import logging
from collections.abc import Callable

import pystray
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)


class TrayController:
    """Windows tray icon with thread-safe callbacks supplied by the Tk UI."""

    def __init__(
        self,
        on_show: Callable[[], None],
        on_exit: Callable[[], None],
        tooltip: str = "Local Dictation",
    ) -> None:
        self._on_show = on_show
        self._on_exit = on_exit
        self._icon = pystray.Icon(
            "local_dictation",
            self._make_icon(),
            tooltip,
            menu=pystray.Menu(
                pystray.MenuItem("Show Local Dictation", self._show, default=True),
                pystray.MenuItem("Exit", self._exit),
            ),
        )

    def start(self) -> None:
        self._icon.run_detached()
        logger.info("System tray icon started")

    def set_tooltip(self, text: str) -> None:
        try:
            self._icon.title = text[:127]
        except Exception:
            logger.debug("Could not update tray tooltip", exc_info=True)

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            logger.debug("Tray icon was already stopped", exc_info=True)

    def _show(self, _icon=None, _item=None) -> None:
        self._on_show()

    def _exit(self, _icon=None, _item=None) -> None:
        self._on_exit()

    @staticmethod
    def _make_icon() -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((2, 2, 62, 62), fill="#1c2028", outline="#72e0a8", width=3)
        draw.rounded_rectangle((24, 13, 40, 38), radius=8, fill="#72e0a8")
        draw.arc((17, 24, 47, 49), 0, 180, fill="#f4f7fb", width=4)
        draw.line((32, 48, 32, 54), fill="#f4f7fb", width=4)
        draw.line((24, 54, 40, 54), fill="#f4f7fb", width=4)
        return image
