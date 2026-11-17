"""Tests for trigger validation, migration, suggestions and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from hotstring import db
from hotstring.executor import CommandError
from hotstring.models import (
    DuplicateTriggerError,
    HotString,
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


def insert_legacy(trigger: str, description: str, command: str, *, enabled: bool = True) -> None:
    """Insert a row that predates the mandatory ';' prefix rule."""

    with db.session_scope() as session:
        session.add(
            HotString(
                trigger=trigger,
                description=description,
                command=command,
                enabled=enabled,
            )
        )


@pytest.mark.parametrize("trigger", [";hs", ";em", ";ab", ";abcdefg", ";Hi!", ";01"])
def test_validate_trigger_accepts_valid_values(trigger: str) -> None:
    assert validate_trigger(trigger) == trigger


@pytest.mark.parametrize(
    "trigger",
    [
        "",
        ";",
        "em",
        "abcdefghi",
        ";abcdefghi",
        ";a b",
        ";a\tb",
        ";line\nbreak",
        ";nul\x00",
        ";;em",
        ";em;hs",
    ],
)
def test_validate_trigger_rejects_invalid_values(trigger: str) -> None:
    with pytest.raises(TriggerValidationError):
        validate_trigger(trigger)


def test_validate_trigger_requires_text() -> None:
    with pytest.raises(TriggerValidationError):
        validate_trigger(None)  # type: ignore[arg-type]


def test_validate_trigger_explains_the_prefix() -> None:
    with pytest.raises(TriggerValidationError, match="must start with"):
        validate_trigger("em")
    with pytest.raises(TriggerValidationError, match="only allowed at the start"):
        validate_trigger(";em;hs")


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
    assert suffix_conflicts(";EM", [";em", "m"]) == []
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
    db.create_hotstring(";em", "lower", "tone")
    db.create_hotstring(";EM", "upper", "ttwo")

    assert db.get_by_trigger(";em") is not None
    assert db.get_by_trigger(";EM") is not None
    assert db.enabled_triggers() == [";EM", ";em"]

    with pytest.raises(DuplicateTriggerError):
        db.create_hotstring(";em", "duplicate", "tthree")


def test_create_validates_all_inputs(database: None) -> None:
    with pytest.raises(TriggerValidationError):
        db.create_hotstring(" ", "bad trigger", "ttext")
    with pytest.raises(TriggerValidationError):
        db.create_hotstring("ok", "x" * 501, "ttext")
    with pytest.raises(CommandError):
        db.create_hotstring(";ok", "bad command", "xtext")
    assert db.count_hotstrings() == 0


def test_update_validates_and_reports_missing_rows(database: None) -> None:
    first = db.create_hotstring(";aa", "First", "tone")
    db.create_hotstring(";bb", "Second", "ttwo")

    with pytest.raises(DuplicateTriggerError):
        db.update_hotstring(first.id, trigger=";bb")
    with pytest.raises(CommandError):
        db.update_hotstring(first.id, command="?nope")
    with pytest.raises(db.NotFoundError):
        db.update_hotstring(9999, description="missing")

    assert db.get_by_trigger(";aa") is not None


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
    db.create_hotstring(";zed", "Z", "tz")
    db.create_hotstring(";Alpha", "A", "ta")
    db.create_hotstring(";beta", "B", "tb", enabled=False)

    rows = db.list_hotstrings()
    assert [row.trigger for row in rows] == [";Alpha", ";beta", ";zed"]
    assert db.enabled_triggers() == [";Alpha", ";zed"]
    assert [row.trigger for row in db.list_hotstrings(enabled_only=True)] == [";Alpha", ";zed"]


def test_migration_prefixes_legacy_triggers(database: None) -> None:
    insert_legacy("em", "Email", "tmail")
    insert_legacy("np", "Notepad", "sStart-Process notepad")
    db.create_hotstring(";hs", "Open", "aopen")

    assert db.migrate_triggers_to_semicolon() == 2
    assert sorted(db.enabled_triggers()) == [";em", ";hs", ";np"]
    assert db.migrate_triggers_to_semicolon() == 0


def test_migration_skips_collisions_and_overlong_triggers(database: None) -> None:
    insert_legacy("em", "Legacy email", "tlegacy")
    db.create_hotstring(";em", "Current email", "tcurrent")
    insert_legacy("12345678", "Too long", "tlong")

    assert db.migrate_triggers_to_semicolon() == 0
    assert sorted(row.trigger for row in db.list_hotstrings()) == [
        "12345678",
        ";em",
        "em",
    ]
    assert db.get_by_trigger(";em").description == "Current email"  # type: ignore[union-attr]


def test_suggestions_are_enabled_prefix_matches(database: None) -> None:
    db.create_hotstring(";email", "Personal email", "tme@example.com")
    db.create_hotstring(";emerg", "Emergency", "t911")
    db.create_hotstring(";np", "Notepad", "sStart-Process notepad", enabled=False)
    db.create_hotstring(";other", "Other", "tx")

    exact = db.suggest_hotstrings(";em")
    assert [item.trigger for item in exact] == [";email", ";emerg"]
    assert exact[0].description == "Personal email"
    assert exact[0].command == "tme@example.com"

    # A capital-letter typo still finds the lower-case trigger.
    assert [item.trigger for item in db.suggest_hotstrings(";EM")] == [";email", ";emerg"]

    assert db.suggest_hotstrings(";np") == []  # disabled entries are hidden
    assert db.suggest_hotstrings("np") == []
    assert [item.trigger for item in db.suggest_hotstrings(";e", limit=1)] == [";email"]
