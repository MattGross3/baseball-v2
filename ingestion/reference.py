"""Static reference data: team names and venue timezones.

Two hand-written tables, deliberately dumb. Both are 30 rows that change
about once a decade, and both exist to make a join explicit rather than
inferred.

WHY THE TEAM CROSSWALK IS HAND-WRITTEN AND NEVER FUZZY-MATCHED
--------------------------------------------------------------
The Odds API identifies teams by display name; MLB StatsAPI identifies them
by integer id. Something has to bridge that, and the tempting shortcut -
match on name, or on a normalised substring - is the exact join the v1
project got wrong. Its odds ingest matched games by comparing team-name
strings, which failed silently whenever a name did not match character for
character, dropping that game's prices with a debug log and no error.

Name drift is not hypothetical here. The Athletics have moved twice recently
and are currently "Athletics" with no city at all, playing in Sacramento. A
fuzzy matcher would have quietly mapped "Athletics" to nothing, or worse, to
the wrong club. An explicit table cannot do that: an unknown name raises.

As of 2026-07-27 all 30 Odds API names happen to equal their StatsAPI
`name`. That is a coincidence of the moment, not a rule, and writing the
mapping out costs nothing while making a rename a loud test failure instead
of a slate that silently loses a game.

WHY VENUE TIMEZONES ARE A FILE AND NOT AN API CALL
--------------------------------------------------
StatsAPI does expose them, but not on the schedule endpoint's venue hydrate
- it needs a second request to /api/v1/venues?hydrate=timezone. For 30 rows
that change when a team relocates, a hydration path is machinery for its own
sake. This exists mainly so the "a Pacific venue's game_date is not its UTC
date" assertion has something to join against.
"""

from __future__ import annotations

__all__ = [
    "MLB_TEAM_IDS",
    "ODDS_API_TEAM_TO_MLB_ID",
    "PACIFIC_VENUE_IDS",
    "VENUE_TIMEZONES",
    "resolve_team_id",
    "venue_timezone",
]


# Odds API display name -> MLB StatsAPI team id.
# Verified against /v4/sports/baseball_mlb/odds and
# /api/v1/teams?sportId=1&season=2026 on 2026-07-27.
ODDS_API_TEAM_TO_MLB_ID: dict[str, int] = {
    "Arizona Diamondbacks": 109,
    # No city: the club relocated and StatsAPI carries locationName
    # 'Sacramento' while the name is bare "Athletics". Historical Odds API
    # payloads may still say "Oakland Athletics"; both are mapped.
    "Athletics": 133,
    "Oakland Athletics": 133,
    "Atlanta Braves": 144,
    "Baltimore Orioles": 110,
    "Boston Red Sox": 111,
    "Chicago Cubs": 112,
    "Chicago White Sox": 145,
    "Cincinnati Reds": 113,
    "Cleveland Guardians": 114,
    "Colorado Rockies": 115,
    "Detroit Tigers": 116,
    "Houston Astros": 117,
    "Kansas City Royals": 118,
    "Los Angeles Angels": 108,
    "Los Angeles Dodgers": 119,
    "Miami Marlins": 146,
    "Milwaukee Brewers": 158,
    "Minnesota Twins": 142,
    "New York Mets": 121,
    "New York Yankees": 147,
    "Philadelphia Phillies": 143,
    "Pittsburgh Pirates": 134,
    "San Diego Padres": 135,
    "San Francisco Giants": 137,
    "Seattle Mariners": 136,
    "St. Louis Cardinals": 138,
    "Tampa Bay Rays": 139,
    "Texas Rangers": 140,
    "Toronto Blue Jays": 141,
    "Washington Nationals": 120,
}

# The 30 current MLB team ids, derived from the crosswalk. Used by the test
# that asserts every team on a live slate resolves.
MLB_TEAM_IDS: frozenset[int] = frozenset(ODDS_API_TEAM_TO_MLB_ID.values())


# MLB venue id -> IANA timezone.
# From /api/v1/venues?hydrate=timezone on 2026-07-27.
VENUE_TIMEZONES: dict[int, str] = {
    15: "America/Phoenix",  # Chase Field (AZ) - no DST
    2529: "America/Los_Angeles",  # Sutter Health Park (ATH, Sacramento)
    4705: "America/New_York",  # Truist Park (ATL)
    2: "America/New_York",  # Oriole Park at Camden Yards (BAL)
    3: "America/New_York",  # Fenway Park (BOS)
    17: "America/Chicago",  # Wrigley Field (CHC)
    4: "America/Chicago",  # Rate Field (CWS)
    2602: "America/New_York",  # Great American Ball Park (CIN)
    5: "America/New_York",  # Progressive Field (CLE)
    19: "America/Denver",  # Coors Field (COL)
    2394: "America/Detroit",  # Comerica Park (DET)
    2392: "America/Chicago",  # Daikin Park (HOU)
    7: "America/Chicago",  # Kauffman Stadium (KC)
    1: "America/Los_Angeles",  # Angel Stadium (LAA)
    22: "America/Los_Angeles",  # Dodger Stadium (LAD)
    4169: "America/New_York",  # loanDepot park (MIA)
    32: "America/Chicago",  # American Family Field (MIL)
    3312: "America/Chicago",  # Target Field (MIN)
    3289: "America/New_York",  # Citi Field (NYM)
    3313: "America/New_York",  # Yankee Stadium (NYY)
    2681: "America/New_York",  # Citizens Bank Park (PHI)
    31: "America/New_York",  # PNC Park (PIT)
    2680: "America/Los_Angeles",  # Petco Park (SD)
    2395: "America/Los_Angeles",  # Oracle Park (SF)
    680: "America/Los_Angeles",  # T-Mobile Park (SEA)
    2889: "America/Chicago",  # Busch Stadium (STL)
    12: "America/New_York",  # Tropicana Field (TB)
    5325: "America/Chicago",  # Globe Life Field (TEX)
    14: "America/Toronto",  # Rogers Centre (TOR)
    3309: "America/New_York",  # Nationals Park (WSH)
}

# The venues where a night game's UTC date is reliably the following day.
# This is what the migration's game_date assertion joins against - a Pacific
# 7pm first pitch is 02:00 UTC tomorrow, so a row whose game_date equals its
# commence_time_utc::date at one of these venues was derived the wrong way.
PACIFIC_VENUE_IDS: frozenset[int] = frozenset(
    venue_id for venue_id, tz in VENUE_TIMEZONES.items() if tz == "America/Los_Angeles"
)


def resolve_team_id(odds_api_name: str) -> int:
    """Odds API team name -> MLB team id. Raises on anything unrecognised.

    Raising is the point. A silent miss here is a game whose prices are
    never recorded, discovered weeks later as a CLV series with holes in it.
    """
    try:
        return ODDS_API_TEAM_TO_MLB_ID[odds_api_name]
    except KeyError:
        raise KeyError(
            f"no MLB team id for Odds API name {odds_api_name!r}. This is a "
            "hand-maintained crosswalk in ingestion/reference.py - add the "
            "name rather than fuzzy-matching it, and check whether a club "
            "has been renamed or relocated."
        ) from None


def venue_timezone(venue_id: int) -> str:
    """MLB venue id -> IANA timezone. Raises on an unknown venue."""
    try:
        return VENUE_TIMEZONES[venue_id]
    except KeyError:
        raise KeyError(
            f"no timezone for MLB venue id {venue_id}. Add it to "
            "VENUE_TIMEZONES in ingestion/reference.py; the value is at "
            "/api/v1/venues?venueIds=<id>&hydrate=timezone."
        ) from None
