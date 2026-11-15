"""System tray icon and menu for HotString."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from .config import APP_NAME, get_icon_path

logger = logging.getLogger(__name__)


def create_tray_image() -> Image.Image:
    """Load the packaged tray icon, generating a fallback if necessary."""

    icon_path = get_icon_path("hotstring.png")
    if icon_path.exists():
        try:
            return Image.open(icon_path).convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        except Exception:
            logger.warning("Could not load the tray icon", exc_info=True)

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 61, 61), radius=14, fill=(37, 99, 235, 255))
    draw.polygon([(35, 10), (19, 36), (30, 36), (27, 54), (45, 27), (34, 27)], fill=(255, 255, 255, 255))
    return image


class TrayIcon:
    """A tray icon whose menu reflects live application state."""

    def __init__(
        self,
        *,
        on_open: Callable[[], None],
        on_toggle_pause: Callable[[], None],
        is_paused: Callable[[], bool],
        on_toggle_autostart: Callable[[], None],
        is_autostart_enabled: Callable[[], bool],
        on_exit: Callable[[], None],
    ) -> None:
        self._on_open = on_open
        self._on_toggle_pause = on_toggle_pause
        self._is_paused = is_paused
        self._on_toggle_autostart = on_toggle_autostart
        self._is_autostart_enabled = is_autostart_enabled
        self._on_exit = on_exit

        self._thread: threading.Thread | None = None
        self._notify_lock = threading.Lock()
        self._image = create_tray_image()
        self._icon = Icon(
            APP_NAME,
            self._image,
            title=f"{APP_NAME} — text expansion & command launcher",
            menu=Menu(
                MenuItem("Open HotString", self._handle_open, default=True),
                MenuItem(self._pause_text, self._handle_toggle_pause),
                MenuItem("Start with Windows", self._handle_toggle_autostart, checked=self._autostart_checked),
                Menu.SEPARATOR,
                MenuItem("Exit", self._handle_exit),
            ),
        )

    # -- menu state --------------------------------------------------------

    def _pause_text(self, _item: MenuItem) -> str:
        return "Resume listening" if self._is_paused() else "Pause listening"

    def _autostart_checked(self, _item: MenuItem) -> bool:
        try:
            return self._is_autostart_enabled()
        except Exception:
            return False

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether the tray thread is alive."""

        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Show the tray icon in a background thread."""

        if self.running:
            return
        self._thread = threading.Thread(target=self._run, name="hotstring-tray", daemon=True)
        self._thread.start()
        logger.info("Tray icon started")

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:
            logger.exception("Tray icon stopped unexpectedly")

    def stop(self) -> None:
        """Hide the icon and stop its message loop."""

        try:
            self._icon.stop()
        except Exception:
            logger.debug("Could not stop the tray icon", exc_info=True)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        logger.info("Tray icon stopped")

    def update_menu(self) -> None:
        """Refresh dynamic menu text and check marks."""

        try:
            self._icon.update_menu()
        except Exception:
            logger.debug("Could not update the tray menu", exc_info=True)

    def notify(self, message: str, title: str | None = None) -> None:
        """Show a native notification, ignoring platforms without support."""

        if not self.running:
            return
        try:
            with self._notify_lock:
                self._icon.notify(message, title or APP_NAME)
        except Exception:
            logger.debug("Could not show a tray notification", exc_info=True)

    # -- callbacks ---------------------------------------------------------

    def _handle_open(self, _icon: Icon, _item: MenuItem) -> None:
        self._safe(self._on_open, "open UI")

    def _handle_toggle_pause(self, _icon: Icon, _item: MenuItem) -> None:
        self._safe(self._on_toggle_pause, "toggle pause")
        self.update_menu()

    def _handle_toggle_autostart(self, _icon: Icon, _item: MenuItem) -> None:
        self._safe(self._on_toggle_autostart, "toggle autostart")
        self.update_menu()

    def _handle_exit(self, _icon: Icon, _item: MenuItem) -> None:
        self._safe(self._on_exit, "exit")

    def _safe(self, callback: Callable[[], None], description: str) -> None:
        try:
            callback()
        except Exception:
            logger.exception("Tray action failed: %s", description)


__all__ = ["TrayIcon", "create_tray_image"]
