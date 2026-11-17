"""Always-on-top popup that suggests HotStrings for a typed ``;`` prefix.

The popup runs Tk in its own thread and communicates with the keyboard hook
through a small command queue.  It is created with ``WS_EX_NOACTIVATE`` so the
application you are typing in keeps keyboard focus: you continue typing to
filter the list, use Up/Down to choose, and press Enter to run the selected
HotString.  Clicking a row runs it too.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from collections.abc import Callable, Sequence
from typing import Any

from .models import Suggestion

logger = logging.getLogger(__name__)

POPUP_WIDTH = 420
HEADER_HEIGHT = 34
ROW_HEIGHT = 24


class SuggestionPopup:
    """A focus-preserving suggestion list driven by the keyboard listener."""

    def __init__(self, on_select: Callable[[str], None]) -> None:
        self.on_select = on_select
        self._queue: queue.Queue[tuple[Any, list[Any], threading.Event]] = queue.Queue()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

        self._lock = threading.RLock()
        self._items: tuple[Suggestion, ...] = ()
        self._selected = 0
        self._selected_trigger: str | None = None
        self._visible = False
        self._geometry: tuple[int, int, int, int] = (0, 0, 0, 0)

        self._root: Any = None
        self._listbox: Any = None
        self._header: Any = None
        self._handle: int | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the Tk thread.  Calling this twice is harmless."""

        if self._thread is not None or self._closed.is_set():
            return
        self._thread = threading.Thread(
            target=self._run, name="hotstring-suggestions", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(2.0):
            logger.warning("Suggestion popup did not become ready in time")

    def close(self) -> None:
        """Close the popup and stop its thread (idempotent)."""

        thread = self._thread
        if thread is None:
            return
        event = threading.Event()
        self._queue.put((self._do_close, [], event))
        event.wait(1.0)
        thread.join(timeout=2.0)
        self._thread = None

    # -- thread-safe state read by the keyboard hook ------------------------

    @property
    def visible(self) -> bool:
        with self._lock:
            return self._visible and not self._closed.is_set()

    @property
    def current_trigger(self) -> str | None:
        with self._lock:
            return self._selected_trigger if self._visible else None

    @property
    def handle(self) -> int | None:
        """Windows HWND of the popup, when available."""

        with self._lock:
            return self._handle

    def contains_point(self, x: int, y: int) -> bool:
        """Whether a screen point lies inside the popup (for mouse clicks)."""

        with self._lock:
            left, top, width, height = self._geometry
            if not self._visible or not width or not height:
                return False
            return left <= x <= left + width and top <= y <= top + height

    # -- commands used by the keyboard hook --------------------------------

    def show(self, prefix: str, items: Sequence[Suggestion]) -> None:
        """Show or refresh the list for ``prefix``."""

        self._enqueue(lambda: self._do_show(prefix, tuple(items)))

    def hide(self) -> None:
        """Hide the popup without changing its data."""

        if self.visible:
            self._enqueue(self._do_hide)

    def move(self, delta: int) -> None:
        """Move the highlighted row by ``delta`` positions."""

        if self.visible:
            self._enqueue(lambda: self._do_move(delta))

    # -- internal queue plumbing -------------------------------------------

    def _enqueue(self, action: Callable[[], Any], *, wait: bool = False) -> Any:
        if self._closed.is_set() or self._thread is None:
            return None
        results: list[Any] = []
        event = threading.Event()
        self._queue.put((action, results, event))
        if wait:
            event.wait(1.0)
            return results[0] if results else None
        return None

    def _run(self) -> None:
        import tkinter as tk

        try:
            root = tk.Tk()
        except Exception:
            logger.exception("Could not create the suggestion popup")
            self._closed.set()
            self._ready.set()
            return

        self._root = root
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg="#0f172a")

        frame = tk.Frame(root, bg="#0f172a", highlightbackground="#334155", highlightthickness=1)
        frame.pack(fill="both", expand=True, padx=1, pady=1)

        self._header = tk.Label(
            frame,
            text="HotStrings",
            bg="#1e293b",
            fg="#e2e8f0",
            font=("Segoe UI", 9),
            anchor="w",
            padx=10,
            pady=6,
        )
        self._header.pack(fill="x")

        self._listbox = tk.Listbox(
            frame,
            height=1,
            activestyle="none",
            borderwidth=0,
            highlightthickness=0,
            bg="#0f172a",
            fg="#e2e8f0",
            selectbackground="#2563eb",
            selectforeground="#ffffff",
            font=("Consolas", 10),
        )
        self._listbox.pack(fill="both", expand=True, padx=4, pady=4)
        self._listbox.bind("<<ListboxSelect>>", self._on_list_select)
        self._listbox.bind("<ButtonRelease-1>", self._on_click)

        root.update_idletasks()
        self._handle = self._disable_activation(root)
        self._ready.set()
        root.after(25, self._drain_queue)
        try:
            root.mainloop()
        except Exception:
            logger.exception("Suggestion popup main loop failed")
        finally:
            with self._lock:
                self._visible = False
                self._selected_trigger = None
                self._geometry = (0, 0, 0, 0)
            self._closed.set()
            self._ready.set()

    @staticmethod
    def _disable_activation(root: Any) -> int | None:
        """Keep the popup from taking focus away from the active application."""

        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
            user32.SetWindowLongW.restype = ctypes.c_long
            handle = int(root.winfo_id())
            style = user32.GetWindowLongW(handle, -20)  # GWL_EXSTYLE
            # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            user32.SetWindowLongW(handle, -20, style | 0x08000000 | 0x00000080)
            return handle
        except Exception:
            logger.debug("Could not mark the suggestion popup as non-activating", exc_info=True)
            return None

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            try:
                action, results, event = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                value = action()
            except Exception:
                logger.exception("Suggestion popup command failed")
                value = None
            if results is not None:
                results.append(value)
            event.set()
        if not self._closed.is_set() and self._root is not None:
            self._root.after(25, self._drain_queue)

    # -- UI operations (run on the Tk thread) -------------------------------

    def _do_show(self, prefix: str, items: tuple[Suggestion, ...]) -> None:
        root = self._root
        if root is None or self._listbox is None or self._header is None:
            return
        if not items:
            self._do_hide()
            return

        self._items = items
        self._selected = 0
        self._listbox.delete(0, "end")
        for item in items:
            label = f"{item.trigger:<9} {item.description or '—'}"
            self._listbox.insert("end", label)
        self._select_row(0)

        count = len(items)
        noun = "match" if count == 1 else "matches"
        self._header.configure(text=f" {prefix}  ·  {count} {noun}  ·  ↑↓ Enter · Esc to close")

        height = HEADER_HEIGHT + count * ROW_HEIGHT + 10
        left, top, _, _ = self._geometry
        pointer_x, pointer_y = root.winfo_pointerxy()
        if not self.contains_point(pointer_x, pointer_y):
            left, top = pointer_x + 12, pointer_y + 12
        max_left = max(0, root.winfo_screenwidth() - POPUP_WIDTH - 8)
        max_top = max(0, root.winfo_screenheight() - height - 8)
        left = min(max(0, left), max_left)
        top = min(max(0, top), max_top)

        root.geometry(f"{POPUP_WIDTH}x{height}+{left}+{top}")
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        with self._lock:
            self._visible = True
            self._geometry = (left, top, POPUP_WIDTH, height)

    def _do_hide(self) -> None:
        if self._root is not None:
            try:
                self._root.withdraw()
            except Exception:
                logger.debug("Could not hide the suggestion popup", exc_info=True)
        with self._lock:
            self._visible = False
            self._selected_trigger = None
            self._geometry = (0, 0, 0, 0)

    def _do_move(self, delta: int) -> None:
        if not self._items:
            return
        index = min(max(0, self._selected + delta), len(self._items) - 1)
        self._select_row(index)

    def _select_row(self, index: int) -> None:
        self._selected = index
        if self._listbox is not None:
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(index)
            self._listbox.activate(index)
            self._listbox.see(index)
        with self._lock:
            self._selected_trigger = self._items[index].trigger if self._items else None

    def _on_list_select(self, _event: Any) -> None:
        if self._listbox is None:
            return
        selected = self._listbox.curselection()
        if selected:
            self._select_row(int(selected[0]))

    def _on_click(self, event: Any) -> None:
        if self._listbox is None or not self._items:
            return
        index = self._listbox.nearest(event.y)
        if 0 <= index < len(self._items):
            self._select_row(index)
            self._notify(self._items[index].trigger)

    def _notify(self, trigger: str) -> None:
        """Call the selection handler off the Tk thread."""

        def run() -> None:
            try:
                self.on_select(trigger)
            except Exception:
                logger.exception("Could not run the selected suggestion %r", trigger)

        threading.Thread(target=run, name="hotstring-suggestion-select", daemon=True).start()

    def _do_close(self) -> None:
        self._do_hide()
        root = self._root
        self._closed.set()
        if root is not None:
            try:
                # Quit the Tk loop but leave the interpreter alive.  Destroying
                # a Tk interpreter created on a worker thread during shutdown
                # makes CPython emit "Tcl_AsyncDelete" errors at process exit.
                root.quit()
            except Exception:
                logger.debug("Could not stop the suggestion popup", exc_info=True)


__all__ = ["SuggestionPopup"]
