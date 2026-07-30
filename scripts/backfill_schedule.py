"""One-off: backfill `games` from 2024-03-01 to today.

Chunked by month. A multi-month range is not merely slow - the droplet
cannot issue one at all, because MLB returns 406 for startDate/endDate from
a datacenter IP, so fetch_schedule already loops day by day internally.
Chunking here keeps each ingest_run to a bounded unit of work so a failure
costs one month rather than the whole season.

Free: StatsAPI is unmetered. No Odds API credits are involved.
"""

import asyncio
import datetime as dt
import sys

from sqlalchemy import func, select

from database.engine import session_scope
from database.models import Game
from ingestion.schedule import upsert_schedule

START = dt.date(2024, 3, 1)


def months(start: dt.date, end: dt.date):
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        yield cur, min(nxt - dt.timedelta(days=1), end)
        cur = nxt


async def main() -> int:
    today = dt.datetime.now(dt.UTC).date()
    total = 0
    for lo, hi in months(START, today):
        async with session_scope() as session:
            n = await upsert_schedule(session, lo, hi)
        total += n
        print(f"  {lo:%Y-%m}  {n:>4} games", flush=True)
    async with session_scope() as session:
        per_season = (
            await session.execute(
                select(
                    func.extract("year", Game.game_date).label("season"),
                    func.count().label("games"),
                )
                .group_by("season")
                .order_by("season")
            )
        ).all()
    print("\n=== games per season ===", flush=True)
    for season, games in per_season:
        print(f"  {int(season)}  {games}", flush=True)
    print(f"  TOTAL upserted this run: {total}", flush=True)
    return 0


loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
raise SystemExit(asyncio.run(main(), loop_factory=loop_factory))
