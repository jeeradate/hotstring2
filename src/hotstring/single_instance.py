"""Single-instance enforcement and "open the UI" signalling on Windows.

A named mutex ensures only one HotString process runs per user session.  A
second process opens a named event before exiting; the first process's waiter
thread then shows the management window.  Both mechanisms are implemented
with the Win32 API through :mod:`ctypes`, so no extra dependency is needed.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from typing import Any

from .config import SHOW_UI_EVENT, SINGLE_INSTANCE_MUTEX

logger = logging.getLogger(__name__)

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

if sys.platform == "win32":  # pragma: no cover - Win32 API wrapper
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.OpenMutexW.restype = wintypes.HANDLE
    _kernel32.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.CreateEventW.restype = wintypes.HANDLE
    _kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.OpenEventW.restype = wintypes.HANDLE
    _kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    _kernel32.SetEvent.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:  # pragma: no cover - development on non-Windows platforms
    _kernel32 = None


class SingleInstance:
    """Coordinate a single running instance of HotString."""

    def __init__(
        self,
        mutex_name: str = SINGLE_INSTANCE_MUTEX,
        event_name: str = SHOW_UI_EVENT,
    ) -> None:
        self.mutex_name = mutex_name
        self.event_name = event_name
        self._mutex: Any = None
        self._event: Any = None
        self._stop = threading.Event()
        self._waiter: threading.Thread | None = None
        self._primary = False

    @property
    def is_primary(self) -> bool:
        """Whether this process acquired the mutex."""

        return self._primary

    def acquire(self) -> bool:
        """Try to become the primary instance.

        :return: ``True`` when this process owns the mutex, ``False`` when
            another instance is already running (after signalling it to show
            its UI).
        """

        if _kernel32 is None:
            # Development fallback; the target platform is Windows.
            self._primary = True
            return True

        # Create the event before the mutex.  A second instance that loses the
        # mutex race can then always find the event and signal it.  If this
        # instance loses the mutex race, the event was still harmless.
        self._event = _kernel32.CreateEventW(None, False, False, self.event_name)
        if not self._event:
            logger.warning("Could not create the show-UI event: %s", ctypes.WinError(ctypes.get_last_error()))

        handle = _kernel32.CreateMutexW(None, False, self.mutex_name)
        if not handle:
            logger.warning(
                "Could not create the single-instance mutex: %s; continuing",
                ctypes.WinError(ctypes.get_last_error()),
            )
            self._primary = True
            return True

        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            logger.info("Another HotString instance is already running; signalling it")
            if self._event:
                _kernel32.SetEvent(self._event)
                _kernel32.CloseHandle(self._event)
                self._event = None
            return False

        self._mutex = handle
        self._primary = True
        logger.info("Acquired single-instance mutex")
        return True

    def signal_show_ui(self) -> None:
        """Ask the primary instance to show its management window."""

        if _kernel32 is None:
            return
        handle = _kernel32.OpenEventW(EVENT_MODIFY_STATE, False, self.event_name)
        if not handle:
            # The primary instance may still be starting up.  Creating the
            # event is fine: its CreateEventW call will then open ours.
            handle = _kernel32.CreateEventW(None, False, False, self.event_name)
        if not handle:
            logger.warning("Could not signal the primary instance")
            return
        try:
            _kernel32.SetEvent(handle)
            logger.info("Signalled the primary instance to show the UI")
        finally:
            _kernel32.CloseHandle(handle)
            # The event object remains alive only while the primary holds it.

    def start_show_ui_waiter(self, callback: Callable[[], None]) -> None:
        """Start a thread that calls ``callback`` whenever the event fires."""

        if _kernel32 is None or not self._event:
            return

        def worker() -> None:
            while not self._stop.is_set():
                result = _kernel32.WaitForSingleObject(self._event, 500)
                if result == WAIT_OBJECT_0:
                    logger.info("Received show-UI signal from a second instance")
                    try:
                        callback()
                    except Exception:
                        logger.exception("Show-UI callback failed")
                elif result not in (WAIT_TIMEOUT,):
                    break

        self._waiter = threading.Thread(target=worker, name="hotstring-instance-waiter", daemon=True)
        self._waiter.start()

    def close(self) -> None:
        """Release the named mutex and event."""

        self._stop.set()
        if self._waiter is not None and self._waiter.is_alive():
            self._waiter.join(timeout=1.0)
        if _kernel32 is not None:
            if self._event:
                _kernel32.CloseHandle(self._event)
            if self._mutex:
                _kernel32.CloseHandle(self._mutex)
        self._event = None
        self._mutex = None
        self._primary = False


__all__ = ["SingleInstance"]
