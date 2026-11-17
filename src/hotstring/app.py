"""Application startup, lifecycle, single-instance handling and tray wiring."""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import queue
import sys
import threading
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import autostart, config, db
from .executor import CommandExecutor
from .listener import KeyboardListener
from .models import TriggerValidationError, validate_trigger
from .native_window import configure as configure_native_window
from .single_instance import SingleInstance
from .ui import HotStringUI

if TYPE_CHECKING:  # pragma: no cover
    from .models import HotString
    from .tray import TrayIcon

logger = logging.getLogger(__name__)


@dataclass
class AppOptions:
    """Command-line options controlling startup and debugging."""

    no_native: bool = False
    no_tray: bool = False
    no_hook: bool = False
    show: bool = False
    port: int | None = None
    log_level: str | None = None


def _enable_dpi_awareness() -> None:
    """Make the native window crisp on high-DPI displays."""

    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            logger.debug("Could not enable DPI awareness", exc_info=True)


def _install_exception_hooks() -> None:
    """Log otherwise-unseen exceptions from threads and the main thread."""

    def handle_exception(exc_type, exc_value, exc_traceback):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = handle_exception

    def handle_thread_exception(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "Uncaught exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = handle_thread_exception


def _message_box(message: str, title: str = config.APP_NAME) -> None:
    """Show a last-resort native error dialog when the GUI cannot start."""

    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
            return
        except Exception:
            pass
    print(f"{title}: {message}", file=sys.stderr)


class HotStringApp:
    """Coordinate the database, keyboard listener, executor, tray and UI."""

    def __init__(self, options: AppOptions | None = None) -> None:
        self.options = options or AppOptions()
        self._single_instance = SingleInstance()
        self.listener = KeyboardListener(self._on_trigger)
        self.executor = CommandExecutor(
            on_action=self._on_action,
            on_error=self._on_error,
            suppress=self.listener.suppress,
        )
        self.ui = HotStringUI(self)
        self.tray: TrayIcon | None = None
        self._shutting_down = False
        self._shutdown_complete = False
        self._pending_show = False
        self._first_run = False
        self._port = self.options.port or 8080
        self._ui_errors: queue.Queue[str] = queue.Queue()
        self._listener_error: str | None = None

    # -- public state used by the UI --------------------------------------

    @property
    def listener_error(self) -> str | None:
        """Return the listener startup error, if any."""

        return self._listener_error

    def needs_onboarding(self) -> bool:
        """Whether the welcome screen should be shown."""

        try:
            return db.is_first_run()
        except Exception:
            logger.exception("Could not determine whether onboarding is needed")
            return False

    def is_paused(self) -> bool:
        """Whether listening is currently paused."""

        return self.listener.paused

    def autostart_enabled(self) -> bool:
        """Return the current start-with-Windows state."""

        try:
            return autostart.is_enabled()
        except Exception:
            logger.debug("Could not read autostart state", exc_info=True)
            return False

    # -- startup -----------------------------------------------------------

    def start(self) -> int:
        """Start the application.  Blocks until NiceGUI shuts down."""

        from nicegui.native import find_open_port

        self._port = self.options.port or find_open_port()

        if not self._single_instance.acquire():
            logger.info("Exiting because another instance is running")
            return 0

        self._single_instance.start_show_ui_waiter(self.show_window)
        db.init_engine()
        db.migrate_triggers_to_semicolon()
        invalid: list[str] = []
        for row in db.list_hotstrings():
            try:
                validate_trigger(row.trigger)
            except TriggerValidationError:
                invalid.append(row.trigger)
        if invalid:
            message = (
                "Some triggers cannot be used yet "
                f"({', '.join(invalid)}). Edit them in the dashboard so they start with "
                "';' and follow the 2–8 character rule."
            )
            logger.warning(message)
            self._report_error(message)
        self._first_run = db.is_first_run()
        logger.info("Startup mode: %s", "first run" if self._first_run else "background")

        if not self.options.no_tray:
            self._start_tray()

        if not self.options.no_hook:
            self._start_listener()

        self.reload_triggers()

        show_at_start = self._first_run or self.options.show
        if self.options.no_native:
            show_at_start = True

        self._configure_native(show_at_start)

        from nicegui import app as nicegui_app

        @nicegui_app.on_startup
        def on_startup() -> None:
            logger.info("NiceGUI started on port %s", self._port)
            if self._pending_show:
                self.show_window()

        @nicegui_app.on_shutdown
        def on_shutdown() -> None:
            self.shutdown()

        self.ui.build()

        try:
            from nicegui import ui

            if not self.options.no_hook:
                pass  # listener already started above

            logger.info("Starting %s UI (native=%s)", config.APP_NAME, not self.options.no_native)
            ui.run(
                host="127.0.0.1",
                port=self._port,
                title=config.APP_NAME,
                favicon=str(config.get_icon_path("hotstring.ico")),
                native=not self.options.no_native,
                window_size=(1100, 780),
                show=self.options.no_native,
                reload=False,
                dark=None,
                show_welcome_message=False,
                uvicorn_logging_level="warning",
            )
        except Exception as error:
            logger.exception("Could not start the UI")
            self._report_error(f"HotString could not start its window: {error}")
            if not self.tray:
                _message_box(f"HotString could not start:\n\n{error}")
            return 1
        finally:
            self.shutdown()
        return 0

    def _configure_native(self, show_at_start: bool) -> None:
        """Set native window options before ``ui.run`` starts pywebview."""

        from nicegui import app as nicegui_app

        # The startup function runs inside pywebview's window process.
        nicegui_app.native.start_args["func"] = configure_native_window
        nicegui_app.native.start_args.setdefault("debug", False)
        nicegui_app.native.window_args.update(
            {
                "hidden": not show_at_start,
                "resizable": True,
                "min_size": (900, 620),
                "confirm_close": False,
                "text_select": True,
                "background_color": "#f8fafc",
                "easy_drag": False,
            }
        )

    def _start_tray(self) -> None:
        try:
            from .tray import TrayIcon

            self.tray = TrayIcon(
                on_open=self.show_window,
                on_toggle_pause=self.toggle_pause,
                is_paused=self.is_paused,
                on_toggle_autostart=self.toggle_autostart,
                is_autostart_enabled=self.autostart_enabled,
                on_exit=self.request_exit,
            )
            self.tray.start()
        except Exception as error:
            logger.exception("Could not start the system tray")
            self.tray = None
            self._report_error(f"HotString could not start the system tray: {error}")

    def _start_listener(self) -> None:
        try:
            self.listener.start()
        except Exception as error:
            logger.exception("Could not start the keyboard listener")
            self._listener_error = (
                f"The global keyboard hook could not start: {error}. "
                "HotStrings will not expand until HotString is restarted."
            )
            self._report_error(self._listener_error)

    # -- database-backed operations used by the UI ------------------------

    def reload_triggers(self) -> None:
        """Reload enabled triggers from the database immediately."""

        try:
            self.listener.set_triggers(db.enabled_triggers())
        except Exception:
            logger.exception("Could not reload triggers")

    def create_hotstring(
        self,
        trigger: str,
        description: str,
        command: str,
        *,
        enabled: bool = True,
    ) -> HotString:
        """Create a HotString and update the live listener."""

        hotstring = db.create_hotstring(trigger, description, command, enabled=enabled)
        self.reload_triggers()
        logger.info("Created HotString %r", hotstring.trigger)
        return hotstring

    def update_hotstring(
        self,
        hotstring_id: int,
        *,
        trigger: str,
        description: str,
        command: str,
        enabled: bool,
    ) -> HotString:
        """Update a HotString and update the live listener."""

        hotstring = db.update_hotstring(
            hotstring_id,
            trigger=trigger,
            description=description,
            command=command,
            enabled=enabled,
        )
        self.reload_triggers()
        logger.info("Updated HotString %r", hotstring.trigger)
        return hotstring

    def set_enabled(self, hotstring_id: int, enabled: bool) -> HotString:
        """Enable/disable a HotString immediately."""

        hotstring = db.set_enabled(hotstring_id, enabled)
        self.reload_triggers()
        return hotstring

    def delete_hotstring(self, hotstring_id: int) -> bool:
        """Delete a HotString and update the live listener."""

        deleted = db.delete_hotstring(hotstring_id)
        self.reload_triggers()
        if deleted:
            logger.info("Deleted HotString %s", hotstring_id)
        return deleted

    def test_command(self, command: str) -> str:
        """Run the dialog's Test action (PowerShell runs synchronously)."""

        return self.executor.test(command)

    # -- tray / UI commands ------------------------------------------------

    def show_window(self) -> None:
        """Show and focus the management window from any thread."""

        if self._shutting_down:
            return

        if self.options.no_native:
            try:
                webbrowser.open(f"http://127.0.0.1:{self._port}/")
            except Exception:
                logger.exception("Could not open the browser UI")
            return

        try:
            from nicegui import app as nicegui_app

            window = nicegui_app.native.main_window
        except Exception:
            logger.exception("Could not access the native window")
            return

        if window is None:
            self._pending_show = True
            logger.debug("UI not ready yet; will show it after startup")
            return

        try:
            window.show()
            window.restore()
            self._pending_show = False
            logger.info("Showing management window")
        except Exception:
            logger.exception("Could not show the management window")
            self._pending_show = True

    def toggle_pause(self) -> None:
        """Pause or resume listening (used by the tray)."""

        self.set_paused(not self.listener.paused)

    def set_paused(self, paused: bool) -> None:
        """Pause or resume listening and refresh the tray menu."""

        if paused:
            self.listener.pause()
        else:
            self.listener.resume()
        self.refresh_tray_menu()

    def refresh_tray_menu(self) -> None:
        """Refresh dynamic tray menu text after state changes."""

        if self.tray is not None:
            self.tray.update_menu()

    def toggle_autostart(self) -> None:
        """Toggle the Windows startup registration (used by the tray)."""

        try:
            self.set_autostart(not self.autostart_enabled())
        except Exception as error:
            self._report_error(f"Could not change start with Windows: {error}")

    def set_autostart(self, enabled: bool) -> None:
        """Enable or disable the startup registration and refresh the tray."""

        autostart.set_enabled(enabled)
        self.refresh_tray_menu()

    def request_exit(self) -> None:
        """Exit cleanly from any thread."""

        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Exit requested")
        try:
            from nicegui import core

            if core.loop is not None and core.loop.is_running():
                core.loop.call_soon_threadsafe(core.stop_and_exit)
                return
        except Exception:
            logger.exception("Could not stop NiceGUI gracefully")

        self.shutdown()
        os._exit(0)

    def shutdown(self) -> None:
        """Release the hook, tray, mutex and database engine (idempotent)."""

        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        logger.info("Shutting down")
        try:
            self.listener.stop()
        except Exception:
            logger.exception("Could not stop the keyboard listener")
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                logger.exception("Could not stop the tray icon")
            self.tray = None
        try:
            self._single_instance.close()
        except Exception:
            logger.exception("Could not release the single-instance lock")
        try:
            db.reset_engine()
        except Exception:
            logger.exception("Could not close the database")
        logger.info("Shutdown complete")

    # -- trigger and command handling -------------------------------------

    def _on_trigger(self, trigger: str) -> None:
        """Handle a matched trigger on a listener worker thread."""

        try:
            hotstring = db.get_by_trigger(trigger)
        except Exception:
            logger.exception("Could not look up trigger %r", trigger)
            self._report_error("Could not read the database for a trigger.")
            return

        if hotstring is None:
            logger.warning("Trigger %r is no longer in the database; reloading", trigger)
            self.reload_triggers()
            return
        if not hotstring.enabled:
            logger.info("Trigger %r matched but is disabled", trigger)
            self.reload_triggers()
            return

        logger.info("Trigger %r matched (%s)", trigger, hotstring.description or "no description")
        self.executor.execute(hotstring.command)

    def _on_action(self, action: str) -> None:
        """Handle an ``a`` command action."""

        if action == "open":
            self.show_window()
        elif action == "pause":
            self.set_paused(True)
        elif action == "resume":
            self.set_paused(False)
        elif action == "toggle":
            self.toggle_pause()
        elif action == "exit":
            self.request_exit()
        else:  # pragma: no cover - validation catches this first
            logger.warning("Unknown internal action %r", action)

    def _on_error(self, message: str) -> None:
        """Log an error and notify the user through the tray and UI."""

        logger.error(message)
        self._ui_errors.put(message)
        if self.tray is not None:
            self.tray.notify(message, title=config.APP_NAME)

    def pop_ui_error(self) -> str | None:
        """Return a pending error message for the dashboard's toast queue."""

        try:
            return self._ui_errors.get_nowait()
        except queue.Empty:
            return None

    def _report_error(self, message: str) -> None:
        """Report a startup or lifecycle error."""

        self._on_error(message)


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""

    parser = argparse.ArgumentParser(
        prog="hotstring",
        description="Windows text expansion and command launcher.",
    )
    parser.add_argument("--version", action="store_true", help="show the version and exit")
    parser.add_argument(
        "--show",
        action="store_true",
        help="show the management window on startup even in background mode",
    )
    parser.add_argument(
        "--no-native",
        action="store_true",
        help="run the UI in a web browser instead of a native window (debugging)",
    )
    parser.add_argument("--no-tray", action="store_true", help="disable the system tray icon (debugging)")
    parser.add_argument(
        "--no-hook", action="store_true", help="disable the global keyboard hook (debugging)"
    )
    parser.add_argument("--port", type=int, default=None, help="HTTP port for the UI server")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="override the log level",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """Application entry point."""

    args = build_parser().parse_args(argv)
    if args.version:
        print(f"{config.APP_NAME} {config.VERSION}")
        return 0

    config.setup_logging(args.log_level)
    _install_exception_hooks()
    _enable_dpi_awareness()
    options = AppOptions(
        no_native=args.no_native,
        no_tray=args.no_tray,
        no_hook=args.no_hook,
        show=args.show,
        port=args.port,
        log_level=args.log_level,
    )
    application = HotStringApp(options)
    try:
        return application.start()
    except Exception as error:
        logger.exception("Fatal startup error")
        _message_box(f"HotString could not start:\n\n{error}")
        return 1


__all__ = ["AppOptions", "HotStringApp", "build_parser", "run"]
