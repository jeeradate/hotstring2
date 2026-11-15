"""Tests for the pure rolling-buffer trigger matcher."""

from __future__ import annotations

from hotstring.matching import RESET_KEY_NAMES, MatchBuffer


def test_match_is_an_exact_case_sensitive_suffix() -> None:
    buffer = MatchBuffer(triggers=["em", "EM"])

    assert buffer.push("xE") is None
    assert buffer.push("M") == "EM"

    buffer.clear()
    assert buffer.push("xe") is None
    assert buffer.push("m") == "em"


def test_longest_trigger_wins() -> None:
    buffer = MatchBuffer(triggers=["em", ";em", "x;em"])

    for character in ";em":
        result = buffer.push(character)

    assert result == ";em"
    assert buffer.match() == ";em"


def test_partial_trigger_does_not_fire() -> None:
    buffer = MatchBuffer(triggers=[";hs"])

    assert buffer.push(";") is None
    assert buffer.push("h") is None
    assert buffer.match() is None


def test_buffer_keeps_only_the_last_eight_characters() -> None:
    buffer = MatchBuffer(triggers=["12345678"])
    buffer.push("abcdefgh")

    assert len(buffer.text) == 8
    assert buffer.text == "abcdefgh"

    buffer.push("9")
    assert buffer.text == "bcdefgh9"
    assert buffer.match() is None


def test_backspace_removes_the_previous_character() -> None:
    buffer = MatchBuffer(triggers=["ab"])
    buffer.push("a")
    buffer.push("x")
    buffer.backspace()
    assert buffer.push("b") == "ab"


def test_backspace_on_empty_buffer_is_safe() -> None:
    buffer = MatchBuffer(triggers=["ab"])
    buffer.backspace()
    assert buffer.text == ""


def test_clear_forgets_everything() -> None:
    buffer = MatchBuffer(triggers=["ab"])
    buffer.push("ab")
    buffer.clear()
    assert buffer.text == ""
    assert buffer.match() is None


def test_push_accepts_multiple_characters_in_order() -> None:
    buffer = MatchBuffer(triggers=["ab", "cd"])

    assert buffer.push("xab") == "ab"
    assert buffer.text == "xab"

    buffer.clear()
    assert buffer.push("xcd") == "cd"


def test_push_ignores_empty_string() -> None:
    buffer = MatchBuffer(triggers=["ab"])
    assert buffer.push("") is None
    assert buffer.text == ""


def test_set_triggers_replaces_and_deduplicates() -> None:
    buffer = MatchBuffer(triggers=["ab", "abc", "ab", ""])
    assert buffer.triggers == ("abc", "ab")

    buffer.set_triggers(["zz"])
    assert buffer.triggers == ("zz",)
    assert buffer.push("ab") is None


def test_trigger_containing_itself_still_matches() -> None:
    buffer = MatchBuffer(triggers=["aa"])
    assert buffer.push("a") is None
    assert buffer.push("a") == "aa"


def test_reset_keys_cover_navigation_and_function_keys() -> None:
    assert "backspace" not in RESET_KEY_NAMES  # Backspace edits the buffer instead
    for name in ("enter", "esc", "tab", "left", "right", "delete", "insert", "f1", "f12"):
        assert name in RESET_KEY_NAMES
