"""A TIMESTAMPTZ column type that refuses naive datetimes.

claude.md: "All timestamps UTC in DB. `game_date` is the LOCAL date at the
venue." Those are two different things and conflating them causes
point-in-time leakage - a game starting 10:10pm Pacific is 05:10 UTC the
*next day*, so its UTC date and its venue-local date disagree. Deriving one
from the other is the bug in the v1 project's odds ingest, where late West
Coast games silently never matched and their prices were dropped.

Postgres `timestamptz` does not store a zone: it stores an instant, and
converts on the way in using whatever the session's TimeZone happens to be.
That means a NAIVE datetime is not rejected by the database - it is silently
interpreted in the server's timezone and written as a different instant than
intended. The bug surfaces months later as prices that appear to have been
observed hours away from when they were.

`UtcDateTime` closes that hole at the only place it can be closed
completely: the type itself. A per-column validator would work equally well
until someone adds a column and forgets one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

__all__ = ["UtcDateTime", "require_utc", "utcnow"]


def utcnow() -> dt.datetime:
    """Timezone-aware current time. Never `datetime.utcnow()`, which returns
    a naive value that looks correct and compares wrongly."""
    return dt.datetime.now(dt.UTC)


def require_utc(value: dt.datetime, *, field: str = "datetime") -> dt.datetime:
    """Return `value` normalised to UTC, or raise if it is naive.

    An aware value in another zone is converted rather than rejected - that
    is a lossless, unambiguous operation. A naive value is rejected because
    guessing its zone is exactly the mistake this module exists to prevent.
    """
    if not isinstance(value, dt.datetime):
        raise TypeError(f"{field} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{field} is naive ({value!r}). All timestamps must be timezone-aware "
            "UTC - a naive value would be silently reinterpreted in the database "
            "server's timezone. Use database.utc.utcnow(), or attach a tzinfo. "
            "If you meant the venue-local calendar date, that belongs in the "
            "separate `game_date` DATE column, not here."
        )
    return value.astimezone(dt.UTC)


class UtcDateTime(TypeDecorator[dt.datetime]):
    """`TIMESTAMPTZ` that enforces aware-UTC in both directions."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: dt.datetime | None, dialect: Dialect
    ) -> dt.datetime | None:
        if value is None:
            return None
        return require_utc(value)

    def process_result_value(
        self, value: dt.datetime | None, dialect: Dialect
    ) -> dt.datetime | None:
        if value is None:
            return None
        # psycopg3 returns aware datetimes for timestamptz, but in the
        # connection's timezone rather than necessarily UTC. Normalising here
        # means calling code can compare and format without re-checking.
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UtcDateTime()"

    # SQLAlchemy compares literal values during autogenerate; without this the
    # type is treated as a plain DateTime and `alembic check` reports spurious
    # diffs on every timestamp column.
    def copy(self, **kw: Any) -> UtcDateTime:
        return UtcDateTime()
