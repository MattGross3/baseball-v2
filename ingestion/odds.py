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

import contextlib
import datetime as dt
import json
import logging
import os
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
    "AmbiguousGame",
    "BudgetExhausted",
    "UnknownGame",
    "budget_gate",
    "capture_slate",
    "parse_capture",
    "ping_healthcheck",
    "poll_odds",
    "remaining_start_clusters",
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


class AmbiguousGame(LookupError):
    """More than one game matched. Deliberately not resolved by guessing."""


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
    capture: dict, game_pks: dict[tuple[int, int], list[tuple[dt.datetime, int]]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Raw capture -> (snapshot rows, unresolved event descriptions).

    Pure: no network, no database.

    Three failure modes, deliberately treated differently:

    - **Unknown team name: RAISES.** The crosswalk is hand-maintained and a
      miss means a club was renamed - it affects every slate until fixed,
      so it must stop the run.
    - **Ambiguous game match: RAISES.** Two candidates means a wrong guess
      is possible, and a price filed against the wrong game of a
      doubleheader is undetectable downstream.
    - **No candidate within tolerance: SKIPPED and reported.** Usually a
      delayed game. Writes nothing wrong, and does not cost the rest of the
      slate.

    THE SAME FUNCTION the poller and the replay tool both use. If it were
    duplicated, a parser fix would apply to live polling but not to the
    backlog of captures it was written to recover.

    `game_pks` maps (home_team_id, away_team_id, game_date) -> game_pk. The
    date is part of the key because a doubleheader is two games between the
    same clubs on the same date - which is precisely why it also carries the
    Odds API's commence_time to separate them.
    """
    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    events = capture["events"]

    for event in events:
        # Raises rather than guessing. A silent miss is a game whose prices
        # are never recorded, found weeks later as a hole in the series.
        home_id = resolve_team_id(event["home_team"])
        away_id = resolve_team_id(event["away_team"])
        commence = dt.datetime.fromisoformat(
            event["commence_time"].replace("Z", "+00:00")
        ).astimezone(dt.UTC)

        # Ambiguity raises from inside _match_game - two candidates means a
        # wrong guess, and a wrong guess is unrecoverable.
        game_pk = _match_game(game_pks, home_id, away_id, commence)
        if game_pk is None:
            # NOT a raise, and this is the one deliberate softening.
            #
            # No candidate within tolerance usually means a delayed game:
            # the Odds API moves commence_time to the revised start while
            # the schedule still holds the scheduled one. Observed - Cubs @
            # Cardinals reported 01:33Z against a scheduled 23:45Z, 1h48m
            # out.
            #
            # Raising here would abort the whole capture, so one delayed
            # game would cost the closing lines of every other game on the
            # slate, and would do so identically on every retry. Skipping
            # writes no wrong price; the event is counted and named in the
            # run's params so it is visible rather than silent.
            unresolved.append(
                f"{event['away_team']} @ {event['home_team']} @{commence.isoformat()}"
            )
            continue

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
    return rows, unresolved


def _classify_outcome(
    market: str, outcome: dict, event: dict
) -> tuple[str | None, Decimal | None]:
    """Odds API outcome -> (our selection, line).

    Moneyline outcomes are named after the teams, so home/away is decided by
    string comparison against the event's own team names - not by position
    in the list, which is not guaranteed and would transpose every price
    while looking completely normal.
    """
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


# How far an Odds API commence_time may sit from a scheduled first pitch and
# still be the same game.
#
# Observed drift is a minute or two (17:41Z against a scheduled 17:40Z), so
# this is generous for clock skew. It is deliberately far NARROWER than the
# gap between a doubleheader's games: a traditional doubleheader's nightcap
# starts roughly three and a half hours after the opener, a split
# doubleheader six or more. A wider window would put both games inside it
# and make every doubleheader ambiguous.
MATCH_TOLERANCE = dt.timedelta(minutes=90)


def _match_game(
    index: dict[tuple[int, int], list[tuple[dt.datetime, int]]],
    home_id: int,
    away_id: int,
    commence: dt.datetime,
) -> int | None:
    """Resolve an event to a game_pk by clubs AND start time.

    Returns None when nothing is close enough. RAISES `AmbiguousGame` when
    more than one game is - it does NOT pick the nearest.

    Not picking is the whole point. Both games of a doubleheader have the
    same clubs on the same date and differ only by first pitch, so a
    resolver that takes the closest candidate will confidently file one
    game's prices against the other whenever the times are close. That
    already happened here once: an earlier version keyed on
    (home, away, date), collapsed the 2026-07-28 Guardians @ Reds
    doubleheader, and wrote game one's prices under game two's pk. Nothing
    downstream could have detected it - the rows looked entirely normal.

    A raise costs one slate's prices for that game and is visible in
    ingest_runs. A wrong guess costs the integrity of every CLV number
    computed from it, silently.
    """
    candidates = index.get((home_id, away_id))
    if not candidates:
        return None

    within = [
        (scheduled, pk)
        for scheduled, pk in candidates
        if abs(scheduled - commence) <= MATCH_TOLERANCE
    ]
    if not within:
        return None
    if len(within) > 1:
        raise AmbiguousGame(
            f"{len(within)} games match {home_id} vs {away_id} within "
            f"{MATCH_TOLERANCE} of {commence.isoformat()}: "
            + ", ".join(f"pk {pk} at {t.isoformat()}" for t, pk in sorted(within))
            + ". Refusing to guess - picking the nearest is how one game's "
            "prices get filed against another."
        )
    return within[0][1]


# Two first pitches closer together than this are one cluster, not two: an
# MLB slate bunches into a handful of start times (roughly 13:05, 19:05,
# 20:10, 22:10 ET) and one poll captures every game in flight at that moment.
CLUSTER_GAP = dt.timedelta(minutes=45)


async def remaining_start_clusters(
    session: AsyncSession, *, now: dt.datetime, horizon: dt.timedelta
) -> int:
    """How many distinct first-pitch clusters are still ahead.

    One poll captures the whole slate, so what the budget has to cover is
    the number of times worth polling, not the number of games.
    """
    result = await session.execute(
        select(Game.commence_time_utc)
        .where(
            Game.commence_time_utc > now,
            Game.commence_time_utc <= now + horizon,
        )
        .order_by(Game.commence_time_utc)
    )
    clusters = 0
    previous: dt.datetime | None = None
    for (commence,) in result.all():
        if previous is None or commence - previous > CLUSTER_GAP:
            clusters += 1
        previous = commence
    return clusters


async def budget_gate(
    session: AsyncSession,
    *,
    remaining: int | None,
    now: dt.datetime,
    horizon: dt.timedelta = dt.timedelta(hours=24),
    floor: int | None = None,
) -> tuple[bool, str]:
    """Whether this poll may spend. Returns (allowed, reason).

    Lives here rather than in cron so the strategy can change without
    touching a crontab, and so it is testable.

    The floor is not a fixed number: it is what the REST OF THE DAY still
    needs. A closing line is the only price this project actually measures,
    so credits must be reserved for the first pitches still ahead rather
    than spent on whichever tick happens to run first. A flat floor cannot
    express that - it would let a quiet morning drain what the evening
    slate needs.
    """
    if remaining is None:
        return True, "no prior poll recorded; spending"

    clusters = await remaining_start_clusters(session, now=now, horizon=horizon)
    cost = len(MARKETS)
    reserved = floor if floor is not None else clusters * cost

    if remaining - cost < reserved:
        return False, (
            f"{remaining} credits left; this poll costs {cost} and "
            f"{clusters} first-pitch cluster(s) in the next "
            f"{horizon} still need {reserved}. Holding back."
        )
    return True, (
        f"{remaining} credits left; {clusters} cluster(s) ahead reserve "
        f"{reserved}, spending {cost}"
    )


def ping_healthcheck(env_var: str = "HEALTHCHECK_URL") -> None:
    """Best-effort liveness ping. Never fails the run.

    A monitoring call that can break the thing it monitors is worse than no
    monitoring - the poll has already spent its credits and written its
    rows by the time this runs, and an unreachable healthcheck host must
    not turn a successful capture into a failed one.
    """
    url = os.environ.get(env_var, "").strip()
    if not url:
        return
    with contextlib.suppress(Exception), urllib.request.urlopen(url, timeout=5):
        pass


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
    floor = min_credits  # None means 'derive it from the remaining slate'

    async with ingest_run(
        session,
        source=SOURCE,
        run_kind="odds_poll",
        params={"markets": list(MARKETS), "regions": list(REGIONS)},
    ) as run:
        remaining = await _remaining_credits(session, exclude_run_id=run.id)
        allowed, reason = await budget_gate(
            session,
            remaining=remaining,
            now=dt.datetime.now(dt.UTC),
            floor=floor,
        )
        if not allowed:
            raise BudgetExhausted(reason)
        log.info("budget: %s", reason)

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
        # captured_at comes from the CAPTURE ENVELOPE, not run.started_at.
        #
        # Both are "one instant per poll", which is the property the unique
        # key needs. But replay reads the envelope, so if the live path used
        # run.started_at instead the two would differ by the seconds between
        # opening the run and the response arriving - and replaying a file
        # already ingested would write a whole second copy rather than
        # colliding. Observed: 820 rows re-inserted on a no-op replay.
        #
        # The envelope's timestamp is also the more accurate one: it is when
        # the prices were actually observed, not when the bookkeeping began.
        captured_at = dt.datetime.fromisoformat(capture["captured_at"]).astimezone(
            dt.UTC
        )
        index = await load_game_index(session, captured_at.date())
        rows, unresolved = parse_capture(capture, index)

        game_dates = await _game_dates(session, {r["game_pk"] for r in rows})
        written = await store_snapshots(
            session,
            rows,
            run_id=run.id,
            captured_at=captured_at,
            game_dates=game_dates,
        )
        run.rows_written = written
        if unresolved:
            run.params = {**run.params, "unresolved": unresolved}
            log.warning(
                "%d event(s) did not resolve to a game: %s",
                len(unresolved),
                "; ".join(unresolved),
            )
        log.info(
            "poll: %d parsed, %d written, %s credits left",
            len(rows),
            written,
            headers.get("x-requests-remaining"),
        )

    # After the run is closed out, so a ping cannot affect what was recorded.
    ping_healthcheck()
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


async def _remaining_credits(
    session: AsyncSession, *, exclude_run_id: int | None = None
) -> int | None:
    """Credits left, from the last poll's recorded headers.

    Derived from our own ingest history rather than a probe request: the
    last poll already told us, and /v4/sports is free but still a round
    trip. None on the first ever poll, which is allowed to proceed.

    `exclude_run_id` IS NOT OPTIONAL IN PRACTICE. `ingest_run` commits the
    run row before the body executes, so by the time this runs the CURRENT
    poll is already the most recent odds_poll row - and it has no
    `remaining_after` yet, because that is written after the request
    returns. Without the exclusion this always found itself, always
    returned None, and the budget gate always allowed the spend while
    logging "no prior poll recorded" as though that were a finding. A
    safety mechanism that is inert but looks like it is working is worse
    than not having one.
    """
    from database.models import IngestRun

    query = (
        select(IngestRun.params)
        .where(
            IngestRun.source == SOURCE,
            IngestRun.run_kind == "odds_poll",
            IngestRun.params.has_key("remaining_after"),
        )
        .order_by(IngestRun.started_at.desc())
        .limit(1)
    )
    if exclude_run_id is not None:
        query = query.where(IngestRun.id != exclude_run_id)

    params = (await session.execute(query)).scalar_one_or_none()
    if not params:
        return None
    value = params.get("remaining_after")
    return int(value) if value is not None else None
