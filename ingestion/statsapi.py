"""MLB StatsAPI client and schedule-row interpretation.

Free, no key, no documented rate limit. Everything here was written against
real responses (see tests/fixtures/statsapi_schedule_rows.json), per
claude.md - the shapes below are observed, not remembered.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

__all__ = [
    "STATSAPI_BASE",
    "GameRow",
    "fetch_schedule",
    "resolve_commence_time",
    "resolve_game_date",
    "resolve_rescheduled_from",
]

STATSAPI_BASE = "https://statsapi.mlb.com/api/v1"
REQUEST_TIMEOUT = 30
USER_AGENT = "baseball-v2/0.1 (+https://github.com/MattGross3/baseball-v2)"

GameRow = dict[str, Any]


def _parse_utc(value: str) -> dt.datetime:
    """StatsAPI emits '2025-08-09T17:15:00Z'; fromisoformat wants an offset."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)


def resolve_commence_time(row: GameRow) -> dt.datetime:
    """First pitch, in UTC, for a schedule row.

    NOT simply `gameDate`. A postponed row is internally inconsistent in a
    way that will silently write a nonsense record if taken at face value.
    Observed, pk 778431, as it stood on 2025-04-06:

        officialDate   = 2025-08-09          <- ALREADY moved forward
        gameDate       = 2025-04-06T17:35Z   <- has NOT moved
        rescheduleDate = 2025-08-09T17:15Z

    Pairing officialDate with gameDate gives a row whose venue-local date
    and UTC start disagree by four months - in a schema whose entire point
    is that those are two different facts and both must be right. The
    consistent pairing is officialDate with rescheduleDate.

    So: when `rescheduleDate` is present, it IS the commence time. StatsAPI
    only sets it while a game is awaiting replay; once played, the row
    carries the real gameDate and `rescheduledFrom` instead.
    """
    reschedule_date = row.get("rescheduleDate")
    if reschedule_date:
        return _parse_utc(reschedule_date)
    return _parse_utc(row["gameDate"])


def resolve_game_date(row: GameRow) -> dt.date:
    """The venue-LOCAL calendar date.

    Always `officialDate`, never derived from a UTC timestamp - deriving it
    is the defect that loses every late Pacific game. StatsAPI already moves
    officialDate forward when a game is rescheduled, so it stays consistent
    with `resolve_commence_time` above without any special case.
    """
    return dt.date.fromisoformat(row["officialDate"])


def resolve_rescheduled_from(row: GameRow) -> dt.date | None:
    """The date this game was originally scheduled for, if it was moved.

    Present only on the replayed row. There is deliberately no companion
    "rescheduled_as" pointing at a successor game: StatsAPI REUSES the
    gamePk across a postponement - pk 778431 was postponed 2025-04-06 and
    played 2025-08-09 under the same pk - so no successor exists to point
    at.
    """
    value = row.get("rescheduledFromDate")
    return dt.date.fromisoformat(value) if value else None


def fetch_schedule(
    start: dt.date,
    end: dt.date | None = None,
    *,
    game_type: str = "R",
    pause: float = 0.15,
) -> list[GameRow]:
    """Schedule rows for a date or inclusive date range.

    `game_type='R'` is regular season only; spring training and exhibitions
    share the endpoint and would otherwise land in `games` as though they
    were real.

    ONE REQUEST PER DAY, AND NO `hydrate`. Both are forced, not preference.
    MLB throttles the expensive query forms by source address: from a
    datacenter IP, `startDate`/`endDate` ranges and any `hydrate` parameter
    return HTTP 406 Not Acceptable, while a bare single-date request returns
    200. The identical range request succeeds from a residential connection,
    so this is not an API change and cannot be found by testing locally -
    the droplet hit it on its first real run.

    Nothing is lost by dropping `hydrate`: the base payload already carries
    every field `game_values()` reads - gamePk, officialDate, gameDate, both
    team ids, venue id, status, and the reschedule fields. The hydrate only
    added detail we never used.

    `pause` is politeness, not rate-limit avoidance: the season backfill is
    ~850 sequential requests against a free, unmetered API.

    A pk can legitimately appear under two dates - the postponed date and
    the replayed date - so callers must upsert on game_pk rather than
    assuming one row per pk per response.
    """
    rows: list[GameRow] = []
    day, last = start, end or start

    while day <= last:
        params = {"sportId": "1", "date": day.isoformat(), "gameType": game_type}
        url = f"{STATSAPI_BASE}/schedule?{urllib.parse.urlencode(params)}"
        # Identify ourselves rather than sending Python-urllib/x.y.
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.load(response)

        rows.extend(
            game for date in payload.get("dates", []) for game in date.get("games", [])
        )
        day += dt.timedelta(days=1)
        if day <= last and pause:
            time.sleep(pause)

    return rows
