"""Cron entrypoint. One process per invocation, no scheduler.

    python -m ingestion schedule    # pull today+tomorrow into `games`
    python -m ingestion poll        # spend credits, capture, store snapshots

Cron calls these. There is deliberately no long-running process to supervise,
no retry framework, and no backoff: a failure is recorded in `ingest_runs`
and the next tick tries again. That is the entire error-handling strategy,
and at a 15-minute cadence it is sufficient.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys

from database.engine import session_scope
from ingestion.odds import BudgetExhausted, poll_odds
from ingestion.schedule import upsert_schedule


async def _schedule(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        today = args.date or dt.datetime.now(dt.UTC).date()
        # Yesterday through tomorrow, not just today.
        #
        # The odds slate is not a calendar day. It carries games already
        # under way from the previous venue-local date - observed: a
        # Yankees @ White Sox event commencing 00:00:19Z, which belongs to
        # the previous day's slate - and games whose UTC date is tomorrow
        # because a late Pacific first pitch crosses midnight UTC. A
        # today-only window leaves the poller unable to resolve either, and
        # an unresolvable event fails the whole capture.
        start, end = today - dt.timedelta(days=1), today + dt.timedelta(days=1)
        written = await upsert_schedule(session, start, end)
        print(f"schedule: {written} game(s) upserted for {start}..{end}")
    return 0


async def _poll(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        try:
            written = await poll_odds(session, min_credits=args.min_credits)
        except BudgetExhausted as exc:
            # Not an error: the floor did its job. Exit clean so cron does
            # not mail about it every tick for the rest of the month.
            print(f"skipped: {exc}")
            return 0
        print(f"poll: {written} snapshot row(s) written")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ingestion")
    sub = parser.add_subparsers(dest="command", required=True)

    schedule = sub.add_parser("schedule", help="upsert today+tomorrow into games")
    schedule.add_argument(
        "--date",
        type=dt.date.fromisoformat,
        default=None,
        help="venue-local start date (YYYY-MM-DD); defaults to today UTC",
    )
    schedule.set_defaults(func=_schedule)

    poll = sub.add_parser("poll", help="capture the slate and store snapshots")
    poll.add_argument(
        "--min-credits",
        type=int,
        default=None,
        help="refuse to spend below this many remaining credits",
    )
    poll.set_defaults(func=_poll)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    return asyncio.run(args.func(args), loop_factory=loop_factory)


if __name__ == "__main__":
    raise SystemExit(main())
