"""SQLModel table and validation helpers for HotStrings."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel

from .config import MAX_TRIGGER_LENGTH, MIN_TRIGGER_LENGTH


class HotStringError(Exception):
    """Base class for user-facing HotString errors."""


class TriggerValidationError(HotStringError, ValueError):
    """Raised when a trigger is not valid."""


class DuplicateTriggerError(HotStringError):
    """Raised when a trigger already exists."""


def utcnow() -> datetime:
    """Return the current UTC time (timezone-aware)."""

    return datetime.now(UTC)


def validate_trigger(trigger: str) -> str:
    """Validate a trigger and return it unchanged.

    Triggers are 2-8 visible characters and may contain letters, digits and
    symbols.  Whitespace is not allowed because it makes matching ambiguous.
    Matching is case-sensitive.

    :raises TriggerValidationError: if the trigger is invalid.
    """

    if not isinstance(trigger, str):
        raise TriggerValidationError("Trigger must be text.")
    if not trigger:
        raise TriggerValidationError("Trigger is required.")
    if any(character.isspace() for character in trigger):
        raise TriggerValidationError("Trigger cannot contain spaces or tabs.")
    if not MIN_TRIGGER_LENGTH <= len(trigger) <= MAX_TRIGGER_LENGTH:
        raise TriggerValidationError(
            f"Trigger must be {MIN_TRIGGER_LENGTH}-{MAX_TRIGGER_LENGTH} characters long."
        )
    if any(ord(character) < 32 for character in trigger):
        raise TriggerValidationError("Trigger cannot contain control characters.")
    return trigger


def validate_description(description: str) -> str:
    """Normalize and validate a human-readable description."""

    if not isinstance(description, str):
        raise TriggerValidationError("Description must be text.")
    description = description.strip()
    if len(description) > 500:
        raise TriggerValidationError("Description must be 500 characters or fewer.")
    return description


def suffix_conflicts(trigger: str, other_triggers: Iterable[str]) -> list[str]:
    """Return triggers that have a suffix relationship with ``trigger``.

    Two triggers conflict when one is a suffix of the other.  With the shorter
    trigger ``em`` and the longer trigger ``;em``, typing ``;em`` fires ``em``
    as soon as its last two characters have been typed.  The UI warns about
    this because the longer trigger would effectively be unreachable.

    :param trigger: The trigger being created or edited.
    :param other_triggers: All other triggers currently in the database.
    :return: Sorted list of conflicting triggers (excluding ``trigger``).
    """

    conflicts = {
        other
        for other in other_triggers
        if other and other != trigger and (other.endswith(trigger) or trigger.endswith(other))
    }
    return sorted(conflicts)


class HotString(SQLModel, table=True):
    """A trigger/command pair stored in SQLite.

    The command's first character determines how it is executed:

    * ``t`` - type text into the focused application
    * ``s`` - run a PowerShell script
    * ``a`` - run an internal application action
    """

    __tablename__ = "hotstring"

    id: int | None = Field(default=None, primary_key=True)
    trigger: str = Field(index=True, unique=True, min_length=MIN_TRIGGER_LENGTH, max_length=MAX_TRIGGER_LENGTH)
    description: str = Field(default="", max_length=500)
    command: str = Field(min_length=1)
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
