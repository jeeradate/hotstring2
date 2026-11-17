"""Tests for prefix handling and suggestion flow without a real keyboard hook."""

from __future__ import annotations

import threading

from hotstring.listener import KeyboardListener
from hotstring.models import Suggestion


class CharKey:
    """Minimal stand-in for a pynput key that produced text."""

    def __init__(self, character: str) -> None:
        self.char = character


class SpecialKey:
    """Minimal stand-in for a non-text pynput key."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SpecialKey) and self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)


class FakePopup:
    """Records popup commands instead of opening a window."""

    def __init__(self) -> None:
        self.visible = False
        self.current_trigger: str | None = None
        self.shown: list[tuple[str, list[Suggestion]]] = []
        self.hides = 0
        self.moves: list[int] = []

    def show(self, prefix: str, items) -> None:  # type: ignore[no-untyped-def]
        self.visible = True
        self.current_trigger = items[0].trigger if items else None
        self.shown.append((prefix, list(items)))

    def hide(self) -> None:
        self.visible = False
        self.current_trigger = None
        self.hides += 1

    def move(self, delta: int) -> None:
        self.moves.append(delta)

    def contains_point(self, x: int, y: int) -> bool:
        return False


def make_listener(
    on_match,
    suggestions: list[Suggestion] | None = None,
    triggers: list[str] | None = None,
):
    """Create an unstarted listener with injectable keys and popup."""

    listener = KeyboardListener(
        on_match,
        triggers or [],
        suggestion_provider=lambda _prefix: suggestions or [],
    )
    popup = FakePopup()
    listener._popup = popup  # type: ignore[assignment]
    listener._backspace_key = SpecialKey("backspace")
    listener._enter_key = SpecialKey("enter")
    listener._up_key = SpecialKey("up")
    listener._down_key = SpecialKey("down")
    listener._reset_keys = {SpecialKey("space"), SpecialKey("esc")}
    return listener, popup


def test_semicolon_starts_a_fresh_sequence_and_opens_suggestions() -> None:
    shown = threading.Event()
    items = [Suggestion(";em", "Email", "tme@example.com")]

    def provider(prefix: str) -> list[Suggestion]:
        if prefix == ";e":
            shown.set()
        return items

    listener, popup = make_listener(lambda _trigger: None)
    listener._suggestion_provider = provider

    listener._on_press(CharKey("x"))
    listener._on_press(CharKey(";"))
    assert listener.buffer.text == ";"
    assert popup.shown == []

    listener._on_press(CharKey("e"))
    assert shown.wait(2.0)
    assert popup.visible is True
    assert popup.shown[-1][0] == ";e"
    assert popup.shown[-1][1] == items


def test_space_dismisses_suggestions_and_clears_buffer() -> None:
    listener, popup = make_listener(
        lambda _trigger: None,
        suggestions=[Suggestion(";em", "Email", "tme@example.com")],
    )
    listener._on_press(CharKey(";"))
    listener._on_press(CharKey("e"))
    for _ in range(100):
        if popup.visible:
            break
        threading.Event().wait(0.01)
    assert popup.visible is True

    listener._on_press(CharKey(" "))
    assert listener.buffer.text == ""
    assert popup.visible is False


def test_enter_runs_the_highlighted_suggestion() -> None:
    fired = threading.Event()
    matches: list[str] = []
    listener, popup = make_listener(
        lambda trigger: (matches.append(trigger), fired.set()),
        suggestions=[Suggestion(";em", "Email", "tme@example.com")],
    )
    listener._on_press(CharKey(";"))
    listener._on_press(CharKey("e"))
    for _ in range(100):
        if popup.visible:
            break
        threading.Event().wait(0.01)

    listener._on_press(SpecialKey("enter"))

    assert fired.wait(2.0)
    assert matches == [";em"]
    assert listener.buffer.text == ""


def test_up_and_down_move_the_popup_highlight() -> None:
    listener, popup = make_listener(
        lambda _trigger: None,
        suggestions=[
            Suggestion(";em", "Email", "tme@example.com"),
            Suggestion(";email", "Work email", "twork@example.com"),
        ],
    )
    listener._on_press(CharKey(";"))
    listener._on_press(CharKey("e"))
    for _ in range(100):
        if popup.visible:
            break
        threading.Event().wait(0.01)

    listener._on_press(SpecialKey("down"))
    listener._on_press(SpecialKey("up"))

    assert popup.moves == [1, -1]


def test_selection_uses_the_captured_prefix_even_if_buffer_was_cleared() -> None:
    fired = threading.Event()
    erased: list[str] = []
    listener, popup = make_listener(
        lambda _trigger: fired.set(),
        suggestions=[Suggestion(";em", "Email", "tme@example.com")],
    )
    listener.erase_typed_trigger = erased.append  # type: ignore[method-assign]
    listener._running = True
    listener._on_press(CharKey(";"))
    listener._on_press(CharKey("e"))
    for _ in range(100):
        if popup.visible:
            break
        threading.Event().wait(0.01)

    listener.buffer.clear()
    listener._on_suggestion_selected(";em")

    assert fired.wait(2.0)
    assert erased == [";e"]


def test_full_trigger_still_fires_when_no_suggestion_is_chosen() -> None:
    fired = threading.Event()
    matches: list[str] = []
    listener, _popup = make_listener(
        lambda trigger: (matches.append(trigger), fired.set()),
        suggestions=[],
        triggers=[";hs"],
    )

    for character in ";hs":
        listener._on_press(CharKey(character))

    assert fired.wait(2.0)
    assert matches == [";hs"]
