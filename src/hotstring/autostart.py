"""Register HotString in the current user's Windows startup registry key.

The entry is written to::

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run

with the value name ``HotString``.  It uses ``pythonw.exe`` (when available)
so Windows does not display a console window at login.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .config import APP_NAME, AUTOSTART_VALUE_NAME, RUN_REGISTRY_KEY, get_pythonw_path

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised on Windows only
    import winreg
except ImportError:  # pragma: no cover - non-Windows development
    winreg = None  # type: ignore[assignment]


class AutostartError(RuntimeError):
    """Raised when the startup registry entry cannot be changed."""


def is_supported() -> bool:
    """Return whether the current platform supports the Run key."""

    return sys.platform == "win32" and winreg is not None


def build_autostart_command(executable: str | Path | None = None) -> str:
    """Build the command line stored in the registry.

    ``pythonw.exe -m hotstring`` runs the installed package without a console
    window.  Frozen executables are launched directly.
    """

    if executable is None:
        executable = sys.executable if getattr(sys, "frozen", False) else get_pythonw_path()
    path = Path(executable)
    if getattr(sys, "frozen", False):
        return f'"{path}"'
    return f'"{path}" -m hotstring'


def get_autostart_command() -> str | None:
    """Return the registered command, or ``None`` when not enabled."""

    if not is_supported():
        return None
    assert winreg is not None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REGISTRY_KEY, 0, winreg.KEY_READ) as key:
            value, _value_type = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("Could not read the Windows startup registry key", exc_info=True)
        return None


def is_enabled() -> bool:
    """Return whether HotString is registered to start with Windows."""

    return get_autostart_command() is not None


def enable(command: str | None = None) -> str:
    """Register HotString in the Run key and return the command used.

    :raises AutostartError: on unsupported platforms or registry failures.
    """

    if not is_supported():
        raise AutostartError("Start with Windows is only supported on Windows.")
    assert winreg is not None
    command = command or build_autostart_command()
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            RUN_REGISTRY_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, command)
    except OSError as error:
        raise AutostartError(f"Could not enable start with Windows: {error}") from error
    logger.info("Registered %s to start with Windows: %s", APP_NAME, command)
    return command


def disable() -> None:
    """Remove HotString from the Run key.

    :raises AutostartError: on unsupported platforms or registry failures.
    """

    if not is_supported():
        raise AutostartError("Start with Windows is only supported on Windows.")
    assert winreg is not None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_REGISTRY_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
    except FileNotFoundError:
        return
    except OSError as error:
        raise AutostartError(f"Could not disable start with Windows: {error}") from error
    logger.info("Removed %s from the Windows startup registry key", APP_NAME)


def set_enabled(enabled: bool) -> None:
    """Enable or disable the startup registration."""

    if enabled:
        enable()
    else:
        disable()


__all__ = [
    "AutostartError",
    "build_autostart_command",
    "disable",
    "enable",
    "get_autostart_command",
    "is_enabled",
    "is_supported",
    "set_enabled",
]
