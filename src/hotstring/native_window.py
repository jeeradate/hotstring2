"""Hooks that run inside NiceGUI's pywebview window process.

NiceGUI 4 starts the native window in a separate process.  This module must
therefore only contain picklable, module-level functions.  ``configure`` is
passed through ``app.native.start_args['func']`` and runs inside that process.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def configure() -> None:
    """Make the window's close button hide it instead of quitting.

    HotString is a tray application: closing the management window should leave
    the keyboard listener running.  The user quits from the tray menu or the
    application's Exit button.
    """

    try:
        import webview

        window = webview.windows[0]
    except (ImportError, IndexError, AttributeError):
        logger.warning("Could not configure native window close behaviour", exc_info=True)
        return

    def on_closing() -> bool:
        try:
            window.hide()
        except Exception:
            logger.exception("Could not hide the native window")
        return False  # cancel the close and keep the window alive

    window.events.closing += on_closing
    logger.debug("Native window configured to hide on close")


__all__ = ["configure"]
