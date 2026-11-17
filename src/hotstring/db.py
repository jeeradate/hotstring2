"""SQLite persistence for HotStrings.

The database engine is created lazily and can be redirected to a temporary
file for tests with :func:`init_engine`.  Every operation uses a short-lived
session so worker, UI and tray threads can safely share the same engine.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from . import config
from .executor import CommandError, CommandType, parse_command
from .models import (
    DuplicateTriggerError,
    HotString,
    HotStringError,
    Suggestion,
    utcnow,
    validate_description,
    validate_trigger,
)

logger = logging.getLogger(__name__)

_engine = None
_engine_path: Path | None = None
_UNSET = object()


class NotFoundError(HotStringError):
    """Raised when a HotString ID does not exist."""


def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    """Enable WAL mode and sensible timeouts on every new connection."""

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def init_engine(db_path: str | Path | None = None, *, echo: bool = False):  # type: ignore[no-untyped-def]
    """Create (or reuse) the global engine and create tables if needed.

    :param db_path: Database file path; defaults to
        ``%APPDATA%\\HotString\\hotstring.db``.
    :param echo: Echo SQL statements (useful while debugging).
    """

    global _engine, _engine_path

    path = Path(db_path).expanduser().resolve() if db_path else config.get_db_path()
    if _engine is not None and _engine_path == path:
        return _engine

    if _engine is not None:
        _engine.dispose()

    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(
        url,
        echo=echo,
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    SQLModel.metadata.create_all(engine)
    _engine = engine
    _engine_path = path
    logger.info("Database ready at %s", path)
    return engine


def get_engine():  # type: ignore[no-untyped-def]
    """Return the global engine, creating it on first use."""

    if _engine is None:
        return init_engine()
    return _engine


def reset_engine() -> None:
    """Dispose the global engine (used by tests and on shutdown)."""

    global _engine, _engine_path
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_path = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional session that is always closed."""

    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def list_hotstrings(*, enabled_only: bool = False) -> list[HotString]:
    """Return all HotStrings sorted case-insensitively by trigger."""

    with session_scope() as session:
        statement = select(HotString)
        if enabled_only:
            statement = statement.where(HotString.enabled.is_(True))
        statement = statement.order_by(func.lower(HotString.trigger), HotString.trigger)
        return list(session.exec(statement).all())


def get_hotstring(hotstring_id: int) -> HotString | None:
    """Return one HotString by ID, or ``None``."""

    with session_scope() as session:
        return session.get(HotString, hotstring_id)


def get_by_trigger(trigger: str) -> HotString | None:
    """Return one HotString by its exact, case-sensitive trigger."""

    with session_scope() as session:
        statement = select(HotString).where(HotString.trigger == trigger)
        return session.exec(statement).first()


def count_hotstrings() -> int:
    """Return the total number of stored HotStrings."""

    with session_scope() as session:
        return int(session.exec(select(func.count()).select_from(HotString)).one())


def enabled_triggers() -> list[str]:
    """Return the triggers the listener should currently watch for."""

    with session_scope() as session:
        statement = (
            select(HotString.trigger)
            .where(HotString.enabled.is_(True))
            .order_by(func.lower(HotString.trigger), HotString.trigger)
        )
        return list(session.exec(statement).all())


def migrate_triggers_to_semicolon() -> int:
    """Add the mandatory ``;`` prefix to legacy triggers, once.

    Rows that already start with ``;`` are untouched.  A row is skipped (and
    logged) when adding the prefix would exceed the length limit or collide
    with another trigger; it stays in the database so the user can edit it.
    Skipped rows cannot fire, because the matcher only accepts ``;`` triggers.

    :return: The number of triggers that were updated.
    """

    changed = 0
    with session_scope() as session:
        rows = list(session.exec(select(HotString)).all())
        existing = {row.trigger for row in rows}
        for row in rows:
            if row.trigger.startswith(config.TRIGGER_PREFIX):
                continue
            candidate = config.TRIGGER_PREFIX + row.trigger
            if len(candidate) > config.MAX_TRIGGER_LENGTH:
                logger.warning(
                    "Cannot migrate trigger %r: '%s' would be too long", row.trigger, candidate
                )
                continue
            if candidate in existing:
                logger.warning(
                    "Cannot migrate trigger %r: '%s' already exists", row.trigger, candidate
                )
                continue
            row.trigger = candidate
            row.updated_at = utcnow()
            existing.add(candidate)
            changed += 1
    if changed:
        logger.info("Migrated %d legacy trigger(s) to the ';' prefix", changed)
    return changed


def suggest_hotstrings(prefix: str, limit: int | None = None) -> list[Suggestion]:
    """Return enabled HotStrings that start with ``prefix``.

    Exact-case matches come first, followed by case-insensitive matches so a
    mistyped capital letter still finds the intended HotString.  Only enabled
    entries are offered.
    """

    if not prefix.startswith(config.TRIGGER_PREFIX):
        return []

    max_results = config.SUGGESTION_LIMIT if limit is None else limit
    if max_results <= 0:
        return []

    with session_scope() as session:
        statement = select(HotString).where(HotString.enabled.is_(True))
        rows = list(session.exec(statement).all())

    def sort_key(row: HotString) -> tuple[str, str]:
        return row.trigger.casefold(), row.trigger

    exact = sorted(
        (row for row in rows if row.trigger.startswith(prefix)), key=sort_key
    )
    fold = prefix.casefold()
    loose = sorted(
        (
            row
            for row in rows
            if not row.trigger.startswith(prefix) and row.trigger.casefold().startswith(fold)
        ),
        key=sort_key,
    )
    chosen = (exact + loose)[:max_results]
    return [
        Suggestion(
            trigger=row.trigger,
            description=row.description,
            command=row.command,
        )
        for row in chosen
    ]


def has_open_action() -> bool:
    """Return whether any HotString runs the ``aopen`` internal action."""

    for hotstring in list_hotstrings():
        try:
            parsed = parse_command(hotstring.command)
        except CommandError:
            continue
        if parsed.type is CommandType.ACTION and parsed.payload.strip().lower() == "open":
            return True
    return False


def is_first_run() -> bool:
    """Return ``True`` when the database is empty or has no ``aopen`` entry."""

    return not has_open_action()


def create_hotstring(
    trigger: str,
    description: str,
    command: str,
    *,
    enabled: bool = True,
) -> HotString:
    """Validate and insert a new HotString.

    :raises TriggerValidationError: for an invalid trigger or description.
    :raises DuplicateTriggerError: when the trigger already exists.
    :raises CommandError: when the command is invalid.
    """

    trigger = validate_trigger(trigger.strip())
    description = validate_description(description)
    from .executor import validate_command

    validate_command(command)

    with session_scope() as session:
        if session.exec(select(HotString).where(HotString.trigger == trigger)).first() is not None:
            raise DuplicateTriggerError(f"Trigger '{trigger}' already exists.")
        hotstring = HotString(
            trigger=trigger,
            description=description,
            command=command,
            enabled=enabled,
        )
        session.add(hotstring)
        try:
            session.flush()
        except IntegrityError as error:
            raise DuplicateTriggerError(f"Trigger '{trigger}' already exists.") from error
        session.refresh(hotstring)
        return hotstring


def update_hotstring(
    hotstring_id: int,
    *,
    trigger: str | object = _UNSET,
    description: str | object = _UNSET,
    command: str | object = _UNSET,
    enabled: bool | object = _UNSET,
) -> HotString:
    """Update selected fields of an existing HotString.

    :raises NotFoundError: if the ID does not exist.
    :raises DuplicateTriggerError: if the new trigger belongs to another row.
    """

    from .executor import validate_command

    with session_scope() as session:
        hotstring = session.get(HotString, hotstring_id)
        if hotstring is None:
            raise NotFoundError(f"HotString {hotstring_id} was not found.")

        if trigger is not _UNSET:
            new_trigger = validate_trigger(str(trigger).strip())
            existing = session.exec(
                select(HotString).where(HotString.trigger == new_trigger, HotString.id != hotstring_id)
            ).first()
            if existing is not None:
                raise DuplicateTriggerError(f"Trigger '{new_trigger}' already exists.")
            hotstring.trigger = new_trigger

        if description is not _UNSET:
            hotstring.description = validate_description(str(description))

        if command is not _UNSET:
            new_command = str(command)
            validate_command(new_command)
            hotstring.command = new_command

        if enabled is not _UNSET:
            hotstring.enabled = bool(enabled)

        hotstring.updated_at = utcnow()
        session.add(hotstring)
        try:
            session.flush()
        except IntegrityError as error:
            raise DuplicateTriggerError(f"Trigger '{hotstring.trigger}' already exists.") from error
        session.refresh(hotstring)
        return hotstring


def set_enabled(hotstring_id: int, enabled: bool) -> HotString:
    """Enable or disable one HotString."""

    return update_hotstring(hotstring_id, enabled=enabled)


def delete_hotstring(hotstring_id: int) -> bool:
    """Delete a HotString and return whether a row was removed."""

    with session_scope() as session:
        hotstring = session.get(HotString, hotstring_id)
        if hotstring is None:
            return False
        session.delete(hotstring)
        return True


__all__ = [
    "NotFoundError",
    "count_hotstrings",
    "create_hotstring",
    "delete_hotstring",
    "enabled_triggers",
    "get_by_trigger",
    "get_engine",
    "get_hotstring",
    "has_open_action",
    "init_engine",
    "is_first_run",
    "list_hotstrings",
    "migrate_triggers_to_semicolon",
    "reset_engine",
    "session_scope",
    "set_enabled",
    "suggest_hotstrings",
    "update_hotstring",
]
