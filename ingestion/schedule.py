"""Daily schedule ingest: StatsAPI -> `games`.

Runs before the odds poller, always. `game_date` is StatsAPI's officialDate
and cannot be derived from anything the odds feed provides, so the poller has
nothing to resolve prices against until this has run.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.ingest_run import ingest_run
from database.models import Game
from ingestion.statsapi import (
    GameRow,
    fetch_schedule,
    resolve_commence_time,
    resolve_game_date,
    resolve_rescheduled_from,
)

__all__ = ["SOURCE", "game_values", "upsert_schedule"]

log = logging.getLogger(__name__)

SOURCE = "mlb-statsapi"


def game_values(row: GameRow) -> dict[str, object]:
    """One schedule row -> the `games` column values.

    Every date/time field goes through the resolvers rather than being read
    off the payload, because a postponed row's `gameDate` and `officialDate`
    contradict each other - see ingestion/statsapi.py.
    """
    return {
        "game_pk": row["gamePk"],
        "game_date": resolve_game_date(row),
        "commence_time_utc": resolve_commence_time(row),
        "home_team_id": row["teams"]["home"]["team"]["id"],
        "away_team_id": row["teams"]["away"]["team"]["id"],
        "venue_id": row["venue"]["id"],
        "status": row["status"]["detailedState"],
        "rescheduled_from_date": resolve_rescheduled_from(row),
    }


async def upsert_schedule(
    session: AsyncSession,
    start: dt.date,
    end: dt.date | None = None,
) -> int:
    """Pull the schedule for a date range and upsert into `games`.

    Returns the number of rows written.

    UPSERT ON game_pk, not insert. Two reasons, both observed rather than
    defensive:

    - A range query can return the SAME pk under two different dates - the
      postponed date and the replayed date - so one response can carry a pk
      twice. Later rows win, which is correct: the replayed row is the more
      current truth.
    - Re-running for a date already ingested must be a no-op, because a
      game's status, start time and date all change over its life (Scheduled
      -> Pre-Game -> In Progress -> Final, or -> Postponed and then moved
      months later). This table is a mutable projection of MLB's current
      opinion, unlike odds_snapshots which is an append-only record of
      observations.
    """
    async with ingest_run(
        session,
        source=SOURCE,
        run_kind="schedule",
        params={"start": start.isoformat(), "end": (end or start).isoformat()},
    ) as run:
        rows = fetch_schedule(start, end)
        # StatsAPI is free and unmetered, but the run still records that a
        # request went out so ingest history is complete.
        run.api_requests = 1

        written = 0
        for row in rows:
            values = game_values(row)
            statement = pg_insert(Game).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[Game.game_pk],
                set_={
                    key: statement.excluded[key] for key in values if key != "game_pk"
                },
            )
            await session.execute(statement)
            written += 1

        run.rows_written = written
        log.info("schedule %s..%s: %d game(s)", start, end or start, written)

    return written
