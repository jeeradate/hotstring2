"""Global low-level keyboard hook and trigger matching.

The listener keeps a rolling eight-character buffer and compares its suffix
against enabled triggers after every typed character.  Heavy work (erasing the
trigger, executing the command and querying suggestions) happens on worker
threads so the low-level hook is never blocked.

Typing ``;`` starts a fresh trigger sequence.  As soon as one more non-space
character is typed, an always-on-top popup offers matching HotStrings; typing
more characters filters the list, Up/Down changes the highlight, and Enter (or
a mouse click) runs the selected HotString.

The module imports :mod:`pynput` lazily so the pure matching logic can be
tested in environments without a desktop.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .config import BUFFER_LENGTH, INJECTION_GUARD_SECONDS, TRIGGER_PREFIX
from .matching import RESET_KEY_NAMES, MatchBuffer
from .models import Suggestion
from .suggestions import SuggestionPopup

logger = logging.getLogger(__name__)

SuggestionProvider = Callable[[str], Sequence[Suggestion]]


def _default_suggestion_provider(prefix: str) -> Sequence[Suggestion]:
    from . import db

    return db.suggest_hotstrings(prefix)


class KeyboardListener:
    """Watch global keystrokes and invoke ``on_match`` for trigger matches."""

    def __init__(
        self,
        on_match: Callable[[str], None],
        triggers: Iterable[str] = (),
        *,
        erase_trigger: bool = True,
        suggestion_provider: SuggestionProvider | None = None,
    ) -> None:
        self.on_match = on_match
        self.erase_trigger = erase_trigger
        self.buffer = MatchBuffer(BUFFER_LENGTH, triggers)
        self._suggestion_provider = suggestion_provider or _default_suggestion_provider

        self._lock = threading.RLock()
        self._injection_lock = threading.Lock()
        self._suppress_until = 0.0
        self._paused = False
        self._running = False
        self._suggestion_generation = 0
        self._suggestion_typed = ""

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
        self._enter_key: Any = None
        self._up_key: Any = None
        self._down_key: Any = None

        self._ctrl_down = False
        self._alt_down = False
        self._win_down = False

        self._popup = SuggestionPopup(self._on_suggestion_selected)

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
        self._enter_key = keyboard.Key.enter
        self._up_key = keyboard.Key.up
        self._down_key = keyboard.Key.down

        self._popup.start()
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
        logger.info(
            "Keyboard listener started with %d trigger(s)", len(self.buffer.triggers)
        )

    def stop(self) -> None:
        """Remove the hooks, hide the popup and stop background watchers."""

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
        self._popup.hide()
        self._popup.close()
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
        self._clear_buffer()
        logger.info("Listening paused")

    def resume(self) -> None:
        """Resume matching."""

        with self._lock:
            self._paused = False
        self._clear_buffer()
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

    # -- suggestion helpers ------------------------------------------------

    def _clear_buffer(self) -> None:
        """Forget the pending trigger and dismiss any suggestions."""

        self.buffer.clear()
        with self._lock:
            self._suggestion_generation += 1
        self._popup.hide()

    def _maybe_show_suggestions(self) -> None:
        """Offer matches once the user has typed ``;`` plus one character."""

        text = self.buffer.text
        if (
            not text.startswith(TRIGGER_PREFIX)
            or len(text) <= len(TRIGGER_PREFIX)
            or text[len(TRIGGER_PREFIX)].isspace()
        ):
            self._popup.hide()
            return

        with self._lock:
            self._suggestion_generation += 1
            generation = self._suggestion_generation
        thread = threading.Thread(
            target=self._load_suggestions,
            args=(text, generation),
            name="hotstring-suggestions-query",
            daemon=True,
        )
        thread.start()

    def _load_suggestions(self, prefix: str, generation: int) -> None:
        try:
            items = self._suggestion_provider(prefix)
        except Exception:
            logger.exception("Could not load HotString suggestions")
            return
        with self._lock:
            stale = (
                generation != self._suggestion_generation
                or self._paused
                or time.monotonic() < self._suppress_until
            )
            if not stale:
                self._suggestion_typed = prefix
        if stale:
            return
        if items:
            self._popup.show(prefix, items)
        else:
            self._popup.hide()

    def _run_suggestion(self, trigger: str) -> None:
        """Erase what the user typed and run the chosen HotString."""

        with self._lock:
            typed = self._suggestion_typed or self.buffer.text
        self._clear_buffer()
        logger.info("Matched suggestion %r", trigger)
        thread = threading.Thread(
            target=self._handle_suggestion,
            args=(trigger, typed),
            name="hotstring-suggestion-match",
            daemon=True,
        )
        thread.start()

    def _handle_suggestion(self, trigger: str, typed: str) -> None:
        try:
            if self.erase_trigger and typed:
                self.erase_typed_trigger(typed)
            self.on_match(trigger)
        except Exception:
            logger.exception("Error while running suggestion %r", trigger)

    def _on_suggestion_selected(self, trigger: str) -> None:
        """Callback used by the popup when a row is clicked."""

        if self._paused or not self._running or not self._popup.visible:
            return
        self._run_suggestion(trigger)

    # -- pynput callbacks --------------------------------------------------

    def _on_press(self, key: Any) -> None:
        # Modifier state is always tracked, even while suppressed, so a
        # synthetic Ctrl release cannot leave us thinking Ctrl is still down.
        if key in self._ctrl_keys:
            self._ctrl_down = True
            self._clear_buffer()
            return
        if key in self._alt_keys:
            self._alt_down = True
            self._clear_buffer()
            return
        if key in self._win_keys:
            self._win_down = True
            self._clear_buffer()
            return

        if self._paused or self.suppressed:
            return

        # The popup never takes focus, so navigation keys still arrive here.
        if self._popup.visible:
            if key == self._enter_key:
                trigger = self._popup.current_trigger
                if trigger is not None:
                    self._run_suggestion(trigger)
                return
            if key == self._up_key:
                self._popup.move(-1)
                return
            if key == self._down_key:
                self._popup.move(1)
                return

        if key in self._reset_keys:
            self._clear_buffer()
            return

        if key == self._backspace_key:
            self.buffer.backspace()
            self._maybe_show_suggestions()
            return

        # Shortcuts are not text.
        if self._ctrl_down or self._alt_down or self._win_down:
            return

        character = getattr(key, "char", None)
        if not isinstance(character, str) or not character:
            return

        if character.isspace() or any(ord(item) < 32 for item in character):
            self._clear_buffer()
            return

        if character == TRIGGER_PREFIX:
            # ';' is the start marker, never a continuation of a pending
            # trigger.  This also guarantees suggestions see a clean prefix.
            self._clear_buffer()
            self.buffer.push(character)
            return

        match = self.buffer.push(character)
        if match is not None:
            self._clear_buffer()
            logger.info("Matched trigger %r", match)
            thread = threading.Thread(
                target=self._handle_match,
                args=(match,),
                name="hotstring-match",
                daemon=True,
            )
            thread.start()
            return

        self._maybe_show_suggestions()

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

    def _on_mouse_click(self, x: int, y: int, _button: Any, pressed: bool) -> None:
        if not pressed or self._popup.contains_point(int(x), int(y)):
            return
        self._clear_buffer()

    def _on_mouse_scroll(self, *_args: Any, **_kwargs: Any) -> None:
        self._clear_buffer()

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
            if current == last_window:
                continue
            last_window = current
            # WS_EX_NOACTIVATE normally prevents this, but guard against a
            # window manager that still focuses the popup.
            if self._popup.visible and current == self._popup.handle:
                continue
            self._clear_buffer()


__all__ = ["KeyboardListener"]
