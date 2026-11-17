"""Pure text-buffer matching used by the global keyboard listener.

Keeping this logic separate makes it easy to test without installing a real
keyboard hook or opening a GUI.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from .config import BUFFER_LENGTH, MAX_TRIGGER_LENGTH, MIN_TRIGGER_LENGTH, TRIGGER_PREFIX


class MatchBuffer:
    """A rolling buffer of recently typed characters.

    The buffer holds at most ``maxlen`` characters (8 by default).  After each
    character the end of the buffer is compared against the configured
    triggers.  Matching is case-sensitive and exact; the longest matching
    trigger wins when several triggers end at the same position.  Triggers
    that do not start with ``;`` are ignored, so a HotString can only be fired
    by typing the prefix first.
    """

    def __init__(self, maxlen: int = BUFFER_LENGTH, triggers: Iterable[str] = ()) -> None:
        self._characters: deque[str] = deque(maxlen=maxlen)
        self._triggers: tuple[str, ...] = ()
        self.set_triggers(triggers)

    @property
    def text(self) -> str:
        """Return the current buffer contents."""

        return "".join(self._characters)

    @property
    def triggers(self) -> tuple[str, ...]:
        """Return the active triggers (longest first)."""

        return self._triggers

    def set_triggers(self, triggers: Iterable[str]) -> None:
        """Replace the active triggers and keep them sorted longest-first.

        Triggers without the ``;`` prefix are dropped.  Legacy database rows
        that were not migrated can therefore never fire by accident.
        """

        unique = {
            trigger
            for trigger in triggers
            if trigger
            and trigger.startswith(TRIGGER_PREFIX)
            and TRIGGER_PREFIX not in trigger[len(TRIGGER_PREFIX):]
            and MIN_TRIGGER_LENGTH <= len(trigger) <= MAX_TRIGGER_LENGTH
        }
        self._triggers = tuple(sorted(unique, key=lambda item: (-len(item), item)))

    def clear(self) -> None:
        """Forget all buffered characters."""

        self._characters.clear()

    def push(self, character: str) -> str | None:
        """Add one typed character and return a matching trigger, if any.

        :param character: The character produced by the keyboard layout.  If
            more than one character is supplied, each is processed in order.
        """

        if not character:
            return None

        match: str | None = None
        for item in character:
            self._characters.append(item)
            found = self.match()
            if found is not None:
                match = found
        return match

    def backspace(self) -> None:
        """Remove the most recently typed character, if any."""

        if self._characters:
            self._characters.pop()

    def match(self) -> str | None:
        """Return the longest trigger that is a suffix of the buffer."""

        buffer_text = self.text
        for trigger in self._triggers:
            if buffer_text.endswith(trigger):
                return trigger
        return None


#: Keys that reset the buffer even though they do not produce text.
RESET_KEY_NAMES = frozenset(
    {
        "enter",
        "esc",
        "tab",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "page_up",
        "page_down",
        "delete",
        "insert",
        "f1",
        "f2",
        "f3",
        "f4",
        "f5",
        "f6",
        "f7",
        "f8",
        "f9",
        "f10",
        "f11",
        "f12",
    }
)
