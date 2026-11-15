"""Tests for trigger validation, suffix warnings and SQLite persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from hotstring import db
from hotstring.executor import CommandError
from hotstring.models import (
    DuplicateTriggerError,
    TriggerValidationError,
    suffix_conflicts,
    validate_description,
    validate_trigger,
)


@pytest.fixture()
def database(tmp_path: Path):
    """Provide an isolated database for every test."""

    db.reset_engine()
    db.init_engine(tmp_path / "test.db")
    try:
        yield
    finally:
        db.reset_engine()


@pytest.mark.parametrize("trigger", ["em", ";hs", "ab", "abcdefgh", "Hi!", "01"])
def test_validate_trigger_accepts_valid_values(trigger: str) -> None:
    assert validate_trigger(trigger) == trigger


@pytest.mark.parametrize(
    "trigger",
    ["", "a", "abcdefghi", "a b", "a\tb", "line\nbreak", "nul\x00"],
)
def test_validate_trigger_rejects_invalid_values(trigger: str) -> None:
    with pytest.raises(TriggerValidationError):
        validate_trigger(trigger)


def test_validate_trigger_requires_text() -> None:
    with pytest.raises(TriggerValidationError):
        validate_trigger(None)  # type: ignore[arg-type]


def test_validate_description_trims_and_limits() -> None:
    assert validate_description("  Open HotString  ") == "Open HotString"
    assert validate_description("") == ""
    with pytest.raises(TriggerValidationError):
        validate_description("x" * 501)


def test_suffix_conflicts_detects_both_directions() -> None:
    assert suffix_conflicts(";em", ["em", ";np", "x;em", "EM"]) == ["em", "x;em"]
    assert suffix_conflicts("em", [";em", "stem", "EM"]) == [";em", "stem"]
    assert suffix_conflicts("abc", ["abc", "xabc"]) == ["xabc"]


def test_suffix_conflicts_is_case_sensitive() -> None:
    assert suffix_conflicts("EM", ["em", "m"]) == []
    assert suffix_conflicts("abc", ["ABC"]) == []
    assert suffix_conflicts("abc", ["abc!"]) == []


def test_crud_round_trip(database: None) -> None:
    created = db.create_hotstring(";em", "Email", "tme@example.com")
    assert created.id is not None
    assert created.enabled is True

    found = db.get_by_trigger(";em")
    assert found is not None
    assert found.description == "Email"

    updated = db.update_hotstring(
        created.id,
        trigger=";mail",
        description="Work email",
        command="twork@example.com",
        enabled=False,
    )
    assert updated.trigger == ";mail"
    assert updated.enabled is False
    assert db.get_by_trigger(";em") is None

    assert db.enabled_triggers() == []
    assert db.count_hotstrings() == 1

    assert db.delete_hotstring(created.id) is True
    assert db.delete_hotstring(created.id) is False
    assert db.count_hotstrings() == 0


def test_triggers_are_case_sensitive(database: None) -> None:
    db.create_hotstring("em", "lower", "tone")
    db.create_hotstring("EM", "upper", "ttwo")

    assert db.get_by_trigger("em") is not None
    assert db.get_by_trigger("EM") is not None
    assert db.enabled_triggers() == ["EM", "em"]

    with pytest.raises(DuplicateTriggerError):
        db.create_hotstring("em", "duplicate", "tthree")


def test_create_validates_all_inputs(database: None) -> None:
    with pytest.raises(TriggerValidationError):
        db.create_hotstring(" ", "bad trigger", "ttext")
    with pytest.raises(TriggerValidationError):
        db.create_hotstring("ok", "x" * 501, "ttext")
    with pytest.raises(CommandError):
        db.create_hotstring("ok", "bad command", "xtext")
    assert db.count_hotstrings() == 0


def test_update_validates_and_reports_missing_rows(database: None) -> None:
    first = db.create_hotstring("aa", "First", "tone")
    db.create_hotstring("bb", "Second", "ttwo")

    with pytest.raises(DuplicateTriggerError):
        db.update_hotstring(first.id, trigger="bb")
    with pytest.raises(CommandError):
        db.update_hotstring(first.id, command="?nope")
    with pytest.raises(db.NotFoundError):
        db.update_hotstring(9999, description="missing")

    assert db.get_by_trigger("aa") is not None


def test_first_run_requires_an_open_action(database: None) -> None:
    assert db.is_first_run() is True
    assert db.has_open_action() is False

    db.create_hotstring(";em", "Email", "tme@example.com")
    assert db.is_first_run() is True

    db.create_hotstring(";hs", "Open", "aopen")
    assert db.has_open_action() is True
    assert db.is_first_run() is False

    db.create_hotstring(";stop", "Stop", "aexit")
    db.delete_hotstring(db.get_by_trigger(";hs").id)  # type: ignore[union-attr]
    assert db.is_first_run() is True


def test_list_and_enabled_triggers_are_sorted(database: None) -> None:
    db.create_hotstring("zed", "Z", "tz")
    db.create_hotstring("Alpha", "A", "ta")
    db.create_hotstring("beta", "B", "tb", enabled=False)

    rows = db.list_hotstrings()
    assert [row.trigger for row in rows] == ["Alpha", "beta", "zed"]
    assert db.enabled_triggers() == ["Alpha", "zed"]
    assert [row.trigger for row in db.list_hotstrings(enabled_only=True)] == ["Alpha", "zed"]
