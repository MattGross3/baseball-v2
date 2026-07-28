"""The Odds API poller.

CAPTURE BEFORE PARSE. The raw response is written to disk before anything
tries to interpret it, and that ordering is the whole design, not tidiness.
A credit spent is gone; if a parse bug throws, the observation must still
exist on disk to be replayed later. The alternative - parse, then persist -
converts every parser defect into permanently lost prices.

`ingestion/replay.py` re-runs the same parse function over those captures, so
a fixed parser can recover everything already spent.

BUDGET. The Odds API charges one credit PER MARKET PER REGION, not per
request - measured, not assumed (a single-market call returns
`x-requests-last: 1`). h2h + totals on the US region is 2 credits a poll, so
500/month is ~250 polls, ~8 a day. Spreads is deliberately not requested:
it would be a 50% cost increase for a market nothing currently measures.

Invoked as a script from cron. There is no scheduler process.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from betting.odds import validate_american
from config import settings
from database.ingest_run import ingest_run
from database.models import Game, OddsSnapshot
from ingestion.reference import resolve_team_id

__all__ = [
    "MARKETS",
    "MATCH_TOLERANCE",
    "RAW_DIR",
    "REGIONS",
    "SOURCE",
    "BudgetExhausted",
    "UnknownGame",
    "capture_slate",
    "parse_capture",
    "poll_odds",
    "store_snapshots",
]

log = logging.getLogger(__name__)

SOURCE = "the-odds-api"
BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEY = "baseball_mlb"
REQUEST_TIMEOUT = 30

# One credit per market per region. Two markets, one region = 2 credits.
MARKETS = ("h2h", "totals")
REGIONS = ("us",)

RAW_DIR = pathlib.Path("raw")

# The Odds API's own market keys -> ours.
_MARKET_KEYS = {"h2h": "moneyline", "totals": "total"}


class BudgetExhausted(RuntimeError):
    """Refused to spend: too few credits left in the period."""


class UnknownGame(LookupError):
    """An event could not be resolved to a game_pk."""


def _api_key() -> str:
    key = settings.odds_api_key
    if not key:
        raise RuntimeError(
            "ODDS_API_KEY is not set. Copy .env.example to .env and add it."
        )
    return key


def capture_slate(*, raw_dir: pathlib.Path = RAW_DIR) -> tuple[pathlib.Path, dict]:
    """Fetch the slate and write the raw response to disk BEFORE parsing.

    Returns the capture path and the response headers. The credits are spent
    by the time this returns; nothing after it can un-spend them, which is
    why nothing after it is allowed to lose the payload.
    """
    params = {
        "apiKey": _api_key(),
        "regions": ",".join(REGIONS),
        "markets": ",".join(MARKETS),
        "oddsFormat": "american",
    }
    url = f"{BASE_URL}/sports/{SPORT_KEY}/odds/?{urllib.parse.urlencode(params)}"

    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
        body = response.read()
        headers = dict(response.headers)

    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = raw_dir / f"odds_{stamp}.json"
    # Envelope, not bare payload: the headers carry the credit accounting and
    # the capture time, and a bare array on disk cannot say what it cost.
    path.write_bytes(
        json.dumps(
            {
                "captured_at": dt.datetime.now(dt.UTC).isoformat(),
                "markets": list(MARKETS),
                "regions": list(REGIONS),
                "headers": {
                    k: v
                    for k, v in headers.items()
                    if k.lower().startswith("x-requests")
                },
                "events": json.loads(body),
            },
            indent=2,
        ).encode("utf-8")
    )
    log.info("captured %s", path)
    return path, headers


def parse_capture(
    capture: dict, game_pks: dict[tuple[int, int, dt.date], int]
) -> list[dict[str, Any]]:
    """Raw capture -> snapshot rows. Pure: no network, no database.

    THE SAME FUNCTION the poller and the replay tool both use. If it were
    duplicated, a parser fix would apply to live polling but not to the
    backlog of captures it was written to recover.

    `game_pks` maps (home_team_id, away_team_id, game_date) -> game_pk. The
    date is part of the key because a doubleheader is two games between the
    same clubs on the same date - which is precisely why it also carries the
    Odds API's commence_time to separate them.
    """
    rows: list[dict[str, Any]] = []
    events = capture["events"]

    for event in events:
        # Raises rather than guessing. A silent miss is a game whose prices
        # are never recorded, found weeks later as a hole in the series.
        home_id = resolve_team_id(event["home_team"])
        away_id = resolve_team_id(event["away_team"])
        commence = dt.datetime.fromisoformat(
            event["commence_time"].replace("Z", "+00:00")
        ).astimezone(dt.UTC)

        game_pk = _match_game(game_pks, home_id, away_id, commence)
        if game_pk is None:
            raise UnknownGame(
                f"no game for {event['away_team']} @ {event['home_team']} "
                f"commencing {commence.isoformat()}. Run the schedule ingest "
                "first; if it has run, the slate and the schedule disagree."
            )

        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                our_market = _MARKET_KEYS.get(market["key"])
                if our_market is None:
                    continue  # a market we did not ask for
                for outcome in market["outcomes"]:
                    selection, line = _classify_outcome(our_market, outcome, event)
                    if selection is None:
                        continue
                    rows.append(
                        {
                            "game_pk": game_pk,
                            "book": book["key"],
                            "market": our_market,
                            "selection": selection,
                            "line": line,
                            "odds_american": validate_american(int(outcome["price"])),
                        }
                    )
    return rows


# How far an Odds API commence_time may sit from a scheduled first pitch and
# still be the same game. Observed drift is a minute or two (17:41Z against a
# scheduled 17:40Z). A doubleheader's two games are hours apart, so this is
# wide enough for clock skew and far too narrow to confuse them.
MATCH_TOLERANCE = dt.timedelta(hours=3)


def _match_game(
    index: dict[tuple[int, int], list[tuple[dt.datetime, int]]],
    home_id: int,
    away_id: int,
    commence: dt.datetime,
) -> int | None:
    """Resolve an event to a game_pk by clubs AND start time.

    Start time is what separates a doubleheader. Team names alone, or teams
    plus a date, cannot: both games have the same clubs on the same date and
    differ only by first pitch.

    Picks the candidate closest in time, and only if it is within
    MATCH_TOLERANCE - a match that is hours off is a different game, and
    returning None so the caller raises is better than filing prices against
    a game that merely looks plausible.
    """
    candidates = index.get((home_id, away_id))
    if not candidates:
        return None

    scheduled, game_pk = min(candidates, key=lambda c: abs(c[0] - commence))
    if abs(scheduled - commence) > MATCH_TOLERANCE:
        return None
    return game_pk


def _classify_outcome(
    market: str, outcome: dict, event: dict
) -> tuple[str | None, Decimal | None]:
    """Odds API outcome -> (our selection, line)."""
    if market == "moneyline":
        if outcome["name"] == event["home_team"]:
            return "home", None
        if outcome["name"] == event["away_team"]:
            return "away", None
        return None, None
    if market == "total":
        name = outcome["name"].lower()
        if name in ("over", "under"):
            return name, Decimal(str(outcome["point"]))
        return None, None
    return None, None


async def load_game_index(
    session: AsyncSession, around: dt.date
) -> dict[tuple[int, int], list[tuple[dt.datetime, int]]]:
    """(home, away) -> [(commence_time_utc, game_pk), ...] near a date.

    Keyed on the CLUBS ONLY, with every candidate game and its start time,
    because the clubs plus a date do not identify a game. A doubleheader is
    two games between the same two clubs on the same date; keying on
    (home, away, date) collapses them and one game's prices get written
    against the other's pk.

    That is not hypothetical - it happened on the first live poll. The
    2026-07-28 Guardians @ Reds doubleheader (pks 824490 at 17:40Z and
    824489 at 23:10Z) resolved entirely to 824489, so game one's prices were
    filed under game two and game two had none. `_match_game` disambiguates
    on start time.

    The window spans a day either side because a night game's UTC date is
    the following venue-local day.
    """
    window = (around - dt.timedelta(days=1), around + dt.timedelta(days=1))
    result = await session.execute(
        select(
            Game.home_team_id,
            Game.away_team_id,
            Game.commence_time_utc,
            Game.game_pk,
        )
        .where(Game.game_date.between(*window))
        .order_by(Game.commence_time_utc)
    )
    index: dict[tuple[int, int], list[tuple[dt.datetime, int]]] = {}
    for home, away, commence, pk in result.all():
        index.setdefault((home, away), []).append((commence, pk))
    return index


async def store_snapshots(
    session: AsyncSession,
    rows: list[dict[str, Any]],
    *,
    run_id: int,
    captured_at: dt.datetime,
    game_dates: dict[int, tuple[dt.date, dt.datetime]],
) -> int:
    """Write snapshot rows, ON CONFLICT DO NOTHING. Returns rows inserted."""
    if not rows:
        return 0

    payload = []
    for row in rows:
        game_date, commence = game_dates[row["game_pk"]]
        payload.append(
            {
                **row,
                "ingest_run_id": run_id,
                "game_date": game_date,
                "commence_time_utc": commence,
                "captured_at": captured_at,
            }
        )

    statement = (
        pg_insert(OddsSnapshot)
        .values(payload)
        .on_conflict_do_nothing(constraint="uq_odds_snapshots_observation")
        .returning(OddsSnapshot.id)
    )
    inserted = (await session.execute(statement)).scalars().all()
    return len(inserted)


async def poll_odds(
    session: AsyncSession,
    *,
    raw_dir: pathlib.Path = RAW_DIR,
    min_credits: int | None = None,
) -> int:
    """Capture the slate, parse it, and store snapshots. Returns rows written.

    Refuses to spend when too few credits remain in the period, so a runaway
    cron cannot exhaust the month in an afternoon.
    """
    floor = settings.odds_api_safety_buffer if min_credits is None else min_credits

    async with ingest_run(
        session,
        source=SOURCE,
        run_kind="odds_poll",
        params={"markets": list(MARKETS), "regions": list(REGIONS)},
    ) as run:
        remaining = await _remaining_credits(session)
        if remaining is not None and remaining <= floor:
            raise BudgetExhausted(
                f"{remaining} credits remain, at or below the {floor} floor. "
                "Not spending. Raise ODDS_API_SAFETY_BUFFER or wait for the "
                "period to reset."
            )

        path, headers = capture_slate(raw_dir=raw_dir)
        # Charge the run with what the API says it cost, not what we assumed,
        # and record what is left so the next poll can check the floor without
        # spending a round trip to find out.
        run.api_requests = int(headers.get("x-requests-last", len(MARKETS)))
        run.params = {
            **run.params,
            "capture": path.name,
            "remaining_after": headers.get("x-requests-remaining"),
            "used_total": headers.get("x-requests-used"),
        }

        capture = json.loads(path.read_text(encoding="utf-8"))
        index = await load_game_index(session, dt.datetime.now(dt.UTC).date())
        rows = parse_capture(capture, index)

        game_dates = await _game_dates(session, {r["game_pk"] for r in rows})
        written = await store_snapshots(
            session,
            rows,
            run_id=run.id,
            captured_at=run.started_at,
            game_dates=game_dates,
        )
        run.rows_written = written
        log.info(
            "poll: %d parsed, %d written, %s credits left",
            len(rows),
            written,
            headers.get("x-requests-remaining"),
        )
    return written


async def _game_dates(
    session: AsyncSession, game_pks: set[int]
) -> dict[int, tuple[dt.date, dt.datetime]]:
    if not game_pks:
        return {}
    result = await session.execute(
        select(Game.game_pk, Game.game_date, Game.commence_time_utc).where(
            Game.game_pk.in_(game_pks)
        )
    )
    return {pk: (date, commence) for pk, date, commence in result.all()}


async def _remaining_credits(session: AsyncSession) -> int | None:
    """Credits left, from the last poll's recorded headers.

    Derived from our own ingest history rather than a free probe request:
    /v4/sports costs nothing but is still a round trip, and the last poll
    already told us. None on the first ever poll, which is allowed to
    proceed.
    """
    from database.models import IngestRun

    result = await session.execute(
        select(IngestRun.params)
        .where(IngestRun.source == SOURCE, IngestRun.run_kind == "odds_poll")
        .order_by(IngestRun.started_at.desc())
        .limit(1)
    )
    params = result.scalar_one_or_none()
    if not params:
        return None
    value = params.get("remaining_after")
    return int(value) if value is not None else None
