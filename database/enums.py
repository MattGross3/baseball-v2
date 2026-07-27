"""Vocabulary for markets, selections, and statuses.

Stored as TEXT with CHECK constraints rather than native Postgres ENUM
types. These lists will grow - player props, first-five-innings, alternate
lines - and `ALTER TYPE ... ADD VALUE` is awkward to express in Alembic and
effectively irreversible on downgrade. Dropping and recreating a CHECK is a
two-line migration that rolls back cleanly.

`StrEnum` members compare equal to their string values, so these can be
passed straight into queries and compared against values read back from the
database without conversion.
"""
from __future__ import annotations

from enum import StrEnum

__all__ = [
    "Market",
    "Selection",
    "BetStatus",
    "IngestStatus",
    "SELECTIONS_FOR_MARKET",
    "OPPOSITE_SELECTION",
]


class Market(StrEnum):
    MONEYLINE = "moneyline"
    TOTAL = "total"
    RUN_LINE = "run_line"

    @property
    def needs_line(self) -> bool:
        """Whether a price in this market is meaningless without its line.

        A total of 8.5 and a total of 9.0 are different markets, not the same
        market at a different price - which is why CLV resolves against the
        line you actually bet.
        """
        return self is not Market.MONEYLINE


class Selection(StrEnum):
    HOME = "home"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"


class BetStatus(StrEnum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    PUSH = "push"
    VOID = "void"

    @property
    def is_settled(self) -> bool:
        return self is not BetStatus.OPEN


class IngestStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


SELECTIONS_FOR_MARKET: dict[Market, tuple[Selection, ...]] = {
    Market.MONEYLINE: (Selection.HOME, Selection.AWAY),
    Market.RUN_LINE: (Selection.HOME, Selection.AWAY),
    Market.TOTAL: (Selection.OVER, Selection.UNDER),
}

OPPOSITE_SELECTION: dict[Selection, Selection] = {
    Selection.HOME: Selection.AWAY,
    Selection.AWAY: Selection.HOME,
    Selection.OVER: Selection.UNDER,
    Selection.UNDER: Selection.OVER,
}
