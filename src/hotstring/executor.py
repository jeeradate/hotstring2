"""Parsing and execution of HotString commands.

A command is plain text whose first character selects its type:

``t<text>``
    Type ``<text>`` into the foreground application.
``s<script>``
    Run ``<script>`` with PowerShell (hidden, non-blocking).  HotString
    prefers PowerShell 7+ (``pwsh``) when available and falls back to
    Windows PowerShell (``powershell.exe``).
``a<action>``
    Run an internal HotString action (for example ``aopen``).

One optional separator (a space or a colon) directly after the prefix is
stripped, so ``t Hello``, ``t:Hello`` and ``tHello`` are equivalent.
"""

from __future__ import annotations

import ctypes
import locale
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from .config import INJECTION_GUARD_SECONDS
from .models import HotStringError

logger = logging.getLogger(__name__)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_powershell_executable: str | None = None
_powershell_display_name: str | None = None

#: Internal actions that may follow the ``a`` prefix.
INTERNAL_ACTIONS = {
    "open": "Open the HotString management window",
    "pause": "Pause listening for triggers",
    "resume": "Resume listening for triggers",
    "toggle": "Toggle listening on or off",
    "exit": "Exit HotString",
}

#: How long to wait before restoring the previous clipboard contents.
CLIPBOARD_RESTORE_DELAY = 0.8

TextStrategy = Literal["type", "paste"]


class CommandError(HotStringError, ValueError):
    """Raised when a command cannot be parsed or validated."""


class CommandType(StrEnum):
    """Supported command types."""

    TEXT = "t"
    POWERSHELL = "s"
    ACTION = "a"


@dataclass(frozen=True)
class ParsedCommand:
    """A parsed command with the separator removed from its payload."""

    type: CommandType
    payload: str

    @property
    def prefix(self) -> str:
        """Return the one-character command prefix."""

        return self.type.value


@dataclass(frozen=True)
class PowerShellResult:
    """Captured result of a PowerShell invocation."""

    returncode: int
    stdout: str
    stderr: str


def _strip_separator(payload: str) -> str:
    """Strip exactly one optional ``' '`` or ``':'`` separator."""

    if payload.startswith((" ", ":")):
        return payload[1:]
    return payload


def parse_command(command: str) -> ParsedCommand:
    """Parse ``command`` without validating its payload.

    :raises CommandError: if the command is empty or has an unknown prefix.
    """

    if not isinstance(command, str):
        raise CommandError("Command must be text.")
    if not command:
        raise CommandError("Command is required.")

    prefix = command[0]
    if prefix not in {item.value for item in CommandType}:
        raise CommandError("Command must start with 't' (text), 's' (PowerShell) or 'a' (action).")

    return ParsedCommand(type=CommandType(prefix), payload=_strip_separator(command[1:]))


def validate_command(command: str) -> ParsedCommand:
    """Parse and validate a command, returning its parsed representation.

    :raises CommandError: if the prefix, payload or internal action is invalid.
    """

    parsed = parse_command(command)

    if parsed.type is CommandType.TEXT:
        if parsed.payload == "":
            raise CommandError("The text command needs some text, for example 'tHello'.")
    elif parsed.type is CommandType.POWERSHELL:
        if parsed.payload.strip() == "":
            raise CommandError("The PowerShell command needs a script, for example 'sStart-Process notepad'.")
    elif parsed.type is CommandType.ACTION:
        action = parsed.payload.strip().lower()
        if not action:
            raise CommandError("The action command needs an action, for example 'aopen'.")
        if action not in INTERNAL_ACTIONS:
            available = ", ".join(sorted(INTERNAL_ACTIONS))
            raise CommandError(f"Unknown internal action '{parsed.payload}'. Available actions: {available}.")

    return parsed


def describe_command(command: str) -> str:
    """Return a short, human-readable description of a valid command."""

    parsed = validate_command(command)
    if parsed.type is CommandType.TEXT:
        return "Type text"
    if parsed.type is CommandType.POWERSHELL:
        return "Run PowerShell"
    return f"App action: {parsed.payload.strip().lower()}"


def text_preview(text: str, limit: int = 500) -> str:
    """Return ``text`` truncated for display in the UI."""

    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… ({len(text) - limit} more characters)"


def choose_text_strategy(text: str) -> TextStrategy:
    """Pick the most reliable way to type ``text``.

    Direct Unicode injection is fast and does not touch the clipboard, but a
    few applications ignore synthetic Unicode input.  Clipboard paste is
    therefore used for Thai/other non-ASCII text, multi-line text and long
    strings.  The ``HOTSTRING_TEXT_MODE`` environment variable (``auto``,
    ``type`` or ``paste``) can override the choice.
    """

    override = os.getenv("HOTSTRING_TEXT_MODE", "auto").strip().lower()
    if override in {"type", "paste"}:
        return override  # type: ignore[return-value]

    if "\n" in text or "\r" in text or "\t" in text:
        return "paste"
    if any(ord(character) > 127 for character in text):
        return "paste"
    if len(text) > 100:
        return "paste"
    return "type"


def _detect_powershell() -> tuple[str, str]:
    """Return the preferred PowerShell executable and a UI-friendly name.

    PowerShell 7+ (``pwsh``) is preferred when it is on PATH.  Windows
    PowerShell (``powershell.exe``) is used as the fallback because it ships
    with Windows.
    """

    for candidate in ("pwsh.exe", "pwsh"):
        path = shutil.which(candidate)
        if path:
            return path, f"PowerShell 7+ (pwsh) — {path}"
    for candidate in ("powershell.exe", "powershell"):
        path = shutil.which(candidate)
        if path:
            return path, f"Windows PowerShell — {path}"
    return "powershell.exe", "Windows PowerShell (powershell.exe)"


def get_powershell_executable() -> str:
    """Return the executable used for ``s`` commands."""

    global _powershell_executable, _powershell_display_name
    if _powershell_executable is None:
        _powershell_executable, _powershell_display_name = _detect_powershell()
        logger.info("Using PowerShell executable: %s", _powershell_display_name)
    return _powershell_executable


def get_powershell_display_name() -> str:
    """Return a human-readable description of the PowerShell in use."""

    global _powershell_executable, _powershell_display_name
    if _powershell_display_name is None:
        _powershell_executable, _powershell_display_name = _detect_powershell()
        logger.info("Using PowerShell executable: %s", _powershell_display_name)
    return _powershell_display_name


def build_powershell_arguments(script: str) -> list[str]:
    """Build the exact argument list used to run a PowerShell command."""

    return [
        get_powershell_executable(),
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]


def run_powershell(script: str, *, timeout: float | None = None) -> PowerShellResult:
    """Run a PowerShell script synchronously and capture its output.

    No console window is shown, even when HotString is running without a
    console of its own.  Callers that need non-blocking behavior should use
    :meth:`CommandExecutor.execute` or run this function in a thread.
    """

    creationflags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    encoding = locale.getpreferredencoding(False) or "utf-8"
    logger.debug("Running PowerShell script (%d chars)", len(script))
    process = subprocess.Popen(
        build_powershell_arguments(script),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding=encoding,
        errors="replace",
        creationflags=creationflags,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise CommandError(f"PowerShell script timed out after {timeout} seconds.") from None
    return PowerShellResult(returncode=process.returncode, stdout=stdout or "", stderr=stderr or "")


# ---------------------------------------------------------------------------
# Clipboard and Unicode text injection
# ---------------------------------------------------------------------------


def _clipboard_copy(text: str, *, attempts: int = 5) -> None:
    """Copy text to the Windows clipboard, retrying while another app holds it."""

    import pyperclip

    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            pyperclip.copy(text)
            return
        except Exception as error:  # pyperclip raises platform-specific errors
            last_error = error
            time.sleep(0.05)
    assert last_error is not None
    raise last_error


def _clipboard_paste() -> str | None:
    """Return the clipboard text, or ``None`` when it cannot be read."""

    try:
        import pyperclip

        return pyperclip.paste()
    except Exception:
        logger.debug("Could not read the clipboard", exc_info=True)
        return None


def _paste_text(text: str) -> None:
    """Put text on the clipboard, send Ctrl+V and restore the old clipboard.

    The restore happens in a short-lived timer thread so the target
    application has time to read the clipboard.  Only text clipboard contents
    can be preserved; images and other formats are replaced by the text.
    """

    from pynput.keyboard import Controller, Key

    original = _clipboard_paste()
    _clipboard_copy(text)
    try:
        keyboard = Controller()
        with keyboard.pressed(Key.ctrl):
            keyboard.press("v")
            keyboard.release("v")
    finally:
        if original is not None:
            def restore() -> None:
                try:
                    _clipboard_copy(original)
                except Exception:
                    logger.debug("Could not restore clipboard", exc_info=True)

            timer = threading.Timer(CLIPBOARD_RESTORE_DELAY, restore)
            timer.daemon = True
            timer.start()


def _send_unicode_text(text: str) -> None:
    """Inject Unicode text with ``SendInput`` and the ``KEYEVENTF_UNICODE`` flag.

    Using Unicode scan codes avoids dependence on the active keyboard layout
    and supports Thai and other non-Latin scripts.  Newlines and tabs are sent
    as virtual key presses because applications do not consistently accept
    them as Unicode characters.
    """

    if sys.platform != "win32":
        raise CommandError("Direct typing is only supported on Windows.")

    from ctypes import wintypes

    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [
            ("type", wintypes.DWORD),
            ("u", INPUT_UNION),
        ]

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1
    VK_RETURN = 0x0D
    VK_TAB = 0x09

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    def send_keyboard_input(vk: int = 0, scan: int = 0, flags: int = 0) -> None:
        item = INPUT(
            type=INPUT_KEYBOARD,
            u=INPUT_UNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)),
        )
        if user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())

    def send_virtual_key(vk: int) -> None:
        send_keyboard_input(vk=vk)
        send_keyboard_input(vk=vk, flags=KEYEVENTF_KEYUP)

    try:
        for index, character in enumerate(text):
            if character == "\r":
                continue
            if character == "\n":
                send_virtual_key(VK_RETURN)
            elif character == "\t":
                send_virtual_key(VK_TAB)
            else:
                units = character.encode("utf-16-le")
                for offset in range(0, len(units), 2):
                    code_unit = int.from_bytes(units[offset:offset + 2], "little")
                    send_keyboard_input(scan=code_unit, flags=KEYEVENTF_UNICODE)
                    send_keyboard_input(scan=code_unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
            if index and index % 40 == 0:
                time.sleep(0.005)
    except OSError as error:
        raise CommandError(f"Could not type text: {error}") from error


def type_text(
    text: str,
    *,
    suppress: Callable[[float], None] | None = None,
    strategy: TextStrategy | None = None,
) -> None:
    """Type ``text`` into the foreground application.

    :param text: Text to type.  May be Unicode and may contain newlines.
    :param suppress: Optional callback that tells the keyboard listener to
        ignore synthetic keystrokes for the given number of seconds.
    :param strategy: Force ``"type"`` or ``"paste"`` instead of automatic
        selection.
    """

    if not text:
        return

    chosen = strategy or choose_text_strategy(text)
    guard = max(INJECTION_GUARD_SECONDS, 0.15 + len(text) * 0.01)
    if suppress is not None:
        suppress(guard)

    logger.info("Typing %d characters using %s", len(text), chosen)
    if chosen == "paste":
        try:
            _paste_text(text)
            return
        except Exception:
            logger.warning("Clipboard paste failed; trying direct typing", exc_info=True)

    _send_unicode_text(text)


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


class TextWriter(Protocol):
    """Callable used to type text; injectable for tests."""

    def __call__(self, text: str) -> None: ...


class CommandExecutor:
    """Execute parsed commands without ever blocking the keyboard hook.

    Errors are logged and reported through ``on_error`` instead of escaping,
    so a broken command can never take down the listener.
    """

    def __init__(
        self,
        *,
        on_action: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_notify: Callable[[str], None] | None = None,
        text_writer: TextWriter | None = None,
        powershell_runner: Callable[..., PowerShellResult] | None = None,
        suppress: Callable[[float], None] | None = None,
    ) -> None:
        self.on_action = on_action
        self.on_error = on_error
        self.on_notify = on_notify
        self._powershell_runner = powershell_runner or run_powershell
        self._text_writer = text_writer or (lambda text: type_text(text, suppress=suppress))

    def execute(self, command: str) -> None:
        """Validate and execute a command, catching and reporting all errors."""

        try:
            parsed = validate_command(command)
        except CommandError as error:
            self._report_error(f"Invalid command: {error}")
            return

        logger.info("Executing command type=%s payload_length=%d", parsed.type.value, len(parsed.payload))
        try:
            if parsed.type is CommandType.TEXT:
                self._text_writer(parsed.payload)
            elif parsed.type is CommandType.POWERSHELL:
                self._run_powershell_async(parsed.payload)
            else:
                self._run_action(parsed.payload.strip().lower())
        except Exception as error:
            logger.exception("Command execution failed")
            self._report_error(f"{describe_command_safe(command)} failed: {error}")

    def _run_action(self, action: str) -> None:
        if action not in INTERNAL_ACTIONS:
            raise CommandError(f"Unknown internal action '{action}'.")
        if self.on_action is None:
            logger.warning("Internal action '%s' has no handler", action)
            return
        self.on_action(action)

    def _run_powershell_async(self, script: str) -> None:
        def worker() -> None:
            try:
                result = self._powershell_runner(script)
            except Exception as error:
                logger.exception("PowerShell script failed to start")
                self._report_error(f"PowerShell failed to start: {error}")
                return

            if result.stdout.strip():
                logger.info("PowerShell stdout:\n%s", result.stdout.rstrip())
            if result.stderr.strip():
                logger.warning("PowerShell stderr:\n%s", result.stderr.rstrip())
            logger.info("PowerShell exited with code %s", result.returncode)

            if result.returncode != 0:
                details = result.stderr.strip() or result.stdout.strip() or "no output"
                self._report_error(
                    f"PowerShell exited with code {result.returncode}: {text_preview(details, 300)}"
                )
            elif self.on_notify is not None:
                self.on_notify("PowerShell command finished successfully.")

        thread = threading.Thread(target=worker, name="hotstring-powershell", daemon=True)
        thread.start()

    def test(self, command: str) -> str:
        """Run a command for the UI's Test button and return a message.

        Text commands are previewed instead of typed.  PowerShell commands are
        run synchronously by the caller (use ``run.io_bound`` in NiceGUI) so
        their output can be shown.  Internal actions are validated but not
        performed.
        """

        parsed = validate_command(command)
        if parsed.type is CommandType.TEXT:
            return f"Preview — this would type {len(parsed.payload)} characters:\n\n{text_preview(parsed.payload)}"
        if parsed.type is CommandType.POWERSHELL:
            result = self._powershell_runner(parsed.payload)
            parts = [f"Exit code: {result.returncode}"]
            if result.stdout.strip():
                parts.append(f"Output:\n{result.stdout.rstrip()}")
            if result.stderr.strip():
                parts.append(f"Errors:\n{result.stderr.rstrip()}")
            return "\n\n".join(parts)
        action = parsed.payload.strip().lower()
        return f"Recognised internal action '{action}': {INTERNAL_ACTIONS[action]}.\n\n(Not executed by Test.)"

    def _report_error(self, message: str) -> None:
        logger.error(message)
        if self.on_error is not None:
            try:
                self.on_error(message)
            except Exception:
                logger.exception("Error notification handler failed")


def describe_command_safe(command: str) -> str:
    """Describe a command without raising for malformed input."""

    try:
        return describe_command(command)
    except CommandError:
        return "Command"


def format_result(result: PowerShellResult) -> str:
    """Format a PowerShell result for logs or the UI."""

    sections = [f"Exit code: {result.returncode}"]
    if result.stdout.strip():
        sections.append(f"Output:\n{result.stdout.rstrip()}")
    if result.stderr.strip():
        sections.append(f"Errors:\n{result.stderr.rstrip()}")
    return "\n\n".join(sections)


__all__ = [
    "INTERNAL_ACTIONS",
    "CommandError",
    "CommandExecutor",
    "CommandType",
    "ParsedCommand",
    "PowerShellResult",
    "build_powershell_arguments",
    "choose_text_strategy",
    "describe_command",
    "format_result",
    "get_powershell_display_name",
    "get_powershell_executable",
    "parse_command",
    "run_powershell",
    "text_preview",
    "type_text",
    "validate_command",
]
