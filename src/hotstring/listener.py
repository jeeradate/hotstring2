"""Global low-level keyboard hook and trigger matching.

The listener keeps a rolling eight-character buffer and compares its suffix
against enabled triggers after every typed character.  Heavy work (erasing the
trigger and executing the command) happens on a worker thread so the
low-level hook is never blocked.

The module imports :mod:`pynput` lazily so the pure matching logic can be
tested in environments without a desktop.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from .config import BUFFER_LENGTH, INJECTION_GUARD_SECONDS
from .matching import RESET_KEY_NAMES, MatchBuffer

logger = logging.getLogger(__name__)


class KeyboardListener:
    """Watch global keystrokes and invoke ``on_match`` for trigger matches."""

    def __init__(
        self,
        on_match: Callable[[str], None],
        triggers: Iterable[str] = (),
        *,
        erase_trigger: bool = True,
    ) -> None:
        self.on_match = on_match
        self.erase_trigger = erase_trigger
        self.buffer = MatchBuffer(BUFFER_LENGTH, triggers)

        self._lock = threading.RLock()
        self._injection_lock = threading.Lock()
        self._suppress_until = 0.0
        self._paused = False
        self._running = False

        self._keyboard_listener: Any = None
        self._mouse_listener: Any = None
        self._focus_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._controller: Any = None
        self._keyboard_module: Any = None

        self._ctrl_keys: set[Any] = set()
        self._alt_keys: set[Any] = set()
        self._win_keys: set[Any] = set()
        self._reset_keys: set[Any] = set()
        self._backspace_key: Any = None

        self._ctrl_down = False
        self._alt_down = False
        self._win_down = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether the underlying hook is active."""

        return self._running

    @property
    def paused(self) -> bool:
        """Whether matching is temporarily disabled."""

        return self._paused

    def start(self) -> None:
        """Install the global keyboard and mouse hooks."""

        if self._running:
            return

        from pynput import keyboard, mouse

        self._keyboard_module = keyboard
        self._controller = keyboard.Controller()
        self._ctrl_keys = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}
        self._alt_keys = {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr}
        self._win_keys = {
            key
            for key in (getattr(keyboard.Key, "cmd", None), getattr(keyboard.Key, "cmd_l", None),
                        getattr(keyboard.Key, "cmd_r", None))
            if key is not None
        }
        self._reset_keys = {
            getattr(keyboard.Key, name)
            for name in RESET_KEY_NAMES
            if hasattr(keyboard.Key, name)
        }
        self._backspace_key = keyboard.Key.backspace

        self._stop_event.clear()
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=False,
        )
        self._keyboard_listener.daemon = True
        self._keyboard_listener.start()

        self._mouse_listener = mouse.Listener(
            on_click=self._on_mouse_click,
            on_scroll=self._on_mouse_scroll,
        )
        self._mouse_listener.daemon = True
        self._mouse_listener.start()

        if sys.platform == "win32":
            self._focus_thread = threading.Thread(
                target=self._watch_foreground_window,
                name="hotstring-focus-watcher",
                daemon=True,
            )
            self._focus_thread.start()

        self._running = True
        logger.info("Keyboard listener started with %d trigger(s)", len(self.buffer.triggers))

    def stop(self) -> None:
        """Remove the hooks and stop background watchers."""

        self._stop_event.set()
        for listener in (self._keyboard_listener, self._mouse_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    logger.debug("Could not stop listener", exc_info=True)
        if self._focus_thread is not None and self._focus_thread.is_alive():
            self._focus_thread.join(timeout=1.0)
        self._keyboard_listener = None
        self._mouse_listener = None
        self._focus_thread = None
        self._running = False
        logger.info("Keyboard listener stopped")

    # -- state -------------------------------------------------------------

    def set_triggers(self, triggers: Iterable[str]) -> None:
        """Replace the active trigger list immediately."""

        self.buffer.set_triggers(triggers)
        logger.info("Listener now watching %d trigger(s)", len(self.buffer.triggers))

    def pause(self) -> None:
        """Stop matching until :meth:`resume` is called."""

        with self._lock:
            self._paused = True
            self.buffer.clear()
        logger.info("Listening paused")

    def resume(self) -> None:
        """Resume matching."""

        with self._lock:
            self._paused = False
            self.buffer.clear()
        logger.info("Listening resumed")

    def toggle_pause(self) -> bool:
        """Toggle the paused state and return the new paused value."""

        if self._paused:
            self.resume()
        else:
            self.pause()
        return self._paused

    def suppress(self, seconds: float = INJECTION_GUARD_SECONDS) -> None:
        """Ignore keyboard events for a short period after synthetic input."""

        with self._lock:
            self._suppress_until = max(self._suppress_until, time.monotonic() + seconds)

    @property
    def suppressed(self) -> bool:
        """Whether synthetic input is currently being ignored."""

        with self._lock:
            return time.monotonic() < self._suppress_until

    def erase_typed_trigger(self, trigger: str) -> None:
        """Send one Backspace per character of ``trigger``."""

        if self._controller is None or not trigger:
            return
        with self._injection_lock:
            self.suppress(INJECTION_GUARD_SECONDS)
            backspace = self._backspace_key
            for _ in range(len(trigger)):
                self._controller.press(backspace)
                self._controller.release(backspace)
                time.sleep(0.008)

    # -- pynput callbacks --------------------------------------------------

    def _on_press(self, key: Any) -> None:
        # Modifier state is always tracked, even while suppressed, so a
        # synthetic Ctrl release cannot leave us thinking Ctrl is still down.
        if key in self._ctrl_keys:
            self._ctrl_down = True
            self.buffer.clear()
            return
        if key in self._alt_keys:
            self._alt_down = True
            self.buffer.clear()
            return
        if key in self._win_keys:
            self._win_down = True
            self.buffer.clear()
            return

        if self._paused or self.suppressed:
            return

        if key in self._reset_keys:
            self.buffer.clear()
            return

        if key == self._backspace_key:
            self.buffer.backspace()
            return

        # Shortcuts are not text.
        if self._ctrl_down or self._alt_down or self._win_down:
            return

        character = getattr(key, "char", None)
        if not isinstance(character, str) or not character:
            return

        if character in {"\r", "\n", "\t"} or any(ord(item) < 32 for item in character):
            self.buffer.clear()
            return

        match = self.buffer.push(character)
        if match is not None:
            self.buffer.clear()
            logger.info("Matched trigger %r", match)
            thread = threading.Thread(
                target=self._handle_match,
                args=(match,),
                name="hotstring-match",
                daemon=True,
            )
            thread.start()

    def _on_release(self, key: Any) -> None:
        if key in self._ctrl_keys:
            self._ctrl_down = False
        elif key in self._alt_keys:
            self._alt_down = False
        elif key in self._win_keys:
            self._win_down = False

    def _handle_match(self, trigger: str) -> None:
        try:
            if self.erase_trigger:
                self.erase_typed_trigger(trigger)
            self.on_match(trigger)
        except Exception:
            logger.exception("Error while handling trigger %r", trigger)

    def _on_mouse_click(self, *_args: Any, **_kwargs: Any) -> None:
        self.buffer.clear()

    def _on_mouse_scroll(self, *_args: Any, **_kwargs: Any) -> None:
        self.buffer.clear()

    def _watch_foreground_window(self) -> None:
        """Reset the buffer whenever the active window changes."""

        try:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetForegroundWindow.restype = wintypes.HWND
            last_window = user32.GetForegroundWindow()
        except Exception:
            logger.debug("Foreground window watcher unavailable", exc_info=True)
            return

        while not self._stop_event.wait(0.25):
            try:
                current = user32.GetForegroundWindow()
            except Exception:
                continue
            if current != last_window:
                last_window = current
                self.buffer.clear()


__all__ = ["KeyboardListener"]
