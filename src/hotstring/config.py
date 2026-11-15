"""Application-wide paths, constants and logging configuration.

HotString stores all of its user data below ``%APPDATA%\\HotString`` on Windows.
The location can be overridden with the ``HOTSTRING_DATA_DIR`` environment
variable, which is primarily useful for tests and portable installs.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "HotString"
APP_SLUG = "hotstring"
VERSION = "0.1.0"

MIN_TRIGGER_LENGTH = 2
MAX_TRIGGER_LENGTH = 8
BUFFER_LENGTH = 8

#: How long (seconds) to ignore keyboard events after synthetic input.
INJECTION_GUARD_SECONDS = 0.35

#: Registry location used for "Start with Windows".
RUN_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "HotString"

#: Names of the single-instance mutex and the "show UI" signal event.
SINGLE_INSTANCE_MUTEX = "Local\\HotString.SingleInstance"
SHOW_UI_EVENT = "Local\\HotString.ShowUI"

LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3

_LOGGING_CONFIGURED = False


def get_data_dir() -> Path:
    """Return the directory containing the database, log and icons.

    ``%APPDATA%`` is used on Windows.  When it is not available (for example
    in a minimal test environment) a sensible per-user fallback is returned.
    """

    override = os.getenv("HOTSTRING_DATA_DIR")
    if override:
        return Path(override).expanduser()

    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME

    if sys.platform == "win32":
        return Path.home() / "AppData" / "Roaming" / APP_NAME
    return Path.home() / ".local" / "share" / APP_SLUG


def get_db_path() -> Path:
    """Return the SQLite database path, creating its directory if needed."""

    path = get_data_dir() / "hotstring.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_log_path() -> Path:
    """Return the rotating log file path, creating its directory if needed."""

    path = get_data_dir() / "hotstring.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_icon_path(*names: str) -> Path:
    """Return a path inside the package's ``assets`` directory."""

    base = Path(__file__).resolve().parent / "assets"
    return base.joinpath(*names)


def get_pythonw_path() -> Path:
    """Return ``pythonw.exe`` when running from a normal CPython install.

    ``pythonw.exe`` is the windowless Python launcher, so the process started
    at login does not flash a console window.  Falls back to the current
    executable for frozen builds or installations without ``pythonw.exe``.
    """

    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        candidate = executable.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return executable


def setup_logging(level: int | str | None = None) -> logging.Logger:
    """Configure console (when present) and rotating-file logging.

    Calling this function more than once is harmless.  Logging to a file is
    essential because the application normally runs without a console.
    """

    global _LOGGING_CONFIGURED
    logger = logging.getLogger()
    if _LOGGING_CONFIGURED:
        return logging.getLogger(APP_SLUG)

    if level is None:
        level = os.getenv("HOTSTRING_LOG_LEVEL", "INFO")

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        file_handler = RotatingFileHandler(
            get_log_path(),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # Logging must never prevent the application from starting.
        pass

    if sys.stderr is not None and not getattr(sys, "frozen", False):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    # Third-party libraries are noisy at INFO level.
    for noisy in ("uvicorn.access", "nicegui", "webview", "pynput", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _LOGGING_CONFIGURED = True
    logger.info("%s %s starting; data directory: %s", APP_NAME, VERSION, get_data_dir())
    return logging.getLogger(APP_SLUG)


def get_logger(name: str = APP_SLUG) -> logging.Logger:
    """Return a namespaced logger."""

    return logging.getLogger(name)
