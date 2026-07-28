"""Schedule-row interpretation. Pure, no network, no database.

Fixtures are REAL StatsAPI responses captured from the live endpoint
(tests/fixtures/statsapi_schedule_rows.json), not invented ones. The
postponement case in particular is a shape that would be easy to get wrong
if written from imagination - the whole point is that the source contradicts
itself and the fixture proves it.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from ingestion.reference import (
    MLB_TEAM_IDS,
    ODDS_API_TEAM_TO_MLB_ID,
    PACIFIC_VENUE_IDS,
    VENUE_TIMEZONES,
    resolve_team_id,
    venue_timezone,
)
from ingestion.statsapi import (
    resolve_commence_time,
    resolve_game_date,
    resolve_rescheduled_from,
)

_FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ROWS = json.loads(
    (_FIXTURES / "statsapi_schedule_rows.json").read_text(encoding="utf-8")
)
ODDS_SLATE = json.loads(
    (_FIXTURES / "odds_api_h2h_sample.json").read_text(encoding="utf-8")
)

UTC = dt.UTC


class TestResolveCommenceTime:
    def test_ordinary_game_uses_game_date(self):
        # The default path, pinned by a real row so it cannot drift.
        row = ROWS["ordinary"]
        assert "rescheduleDate" not in row or row.get("rescheduleDate") is None
        assert resolve_commence_time(row) == dt.datetime(
            2025, 7, 27, 17, 35, tzinfo=UTC
        )

    def test_postponed_game_uses_reschedule_date_not_game_date(self):
        """The case that would silently write a four-month-inconsistent row.

        pk 778431 as it stood on 2025-04-06:
            officialDate   = 2025-08-09        (already moved)
            gameDate       = 2025-04-06T17:35Z (has NOT moved)
            rescheduleDate = 2025-08-09T17:15Z
        """
        row = ROWS["postponed"]
        assert row["gamePk"] == 778431
        assert row["status"]["detailedState"] == "Postponed"
        # The source really is self-contradictory - this is not a strawman.
        assert row["officialDate"] == "2025-08-09"
        assert row["gameDate"] == "2025-04-06T17:35:00Z"

        commence = resolve_commence_time(row)
        assert commence == dt.datetime(2025, 8, 9, 17, 15, tzinfo=UTC)
        # NOT the stale gameDate.
        assert commence != dt.datetime(2025, 4, 6, 17, 35, tzinfo=UTC)

    def test_postponed_row_is_internally_consistent_after_resolution(self):
        # The property that matters: game_date and commence_time_utc must
        # describe the same event. Taking (officialDate, gameDate) naively
        # would put them four months apart.
        row = ROWS["postponed"]
        assert resolve_game_date(row) == resolve_commence_time(row).date()

    def test_replayed_game_uses_its_real_game_date(self):
        row = ROWS["replayed"]
        assert row["gamePk"] == 778431
        assert row["status"]["detailedState"] == "Final"
        assert resolve_commence_time(row) == dt.datetime(2025, 8, 9, 17, 15, tzinfo=UTC)

    def test_the_pk_is_the_same_across_the_postponement(self):
        # Why there is no `rescheduled_as` column: StatsAPI reuses the pk,
        # so there is no successor game to point at. A permanently-NULL
        # column would be a claim about the data that is not true.
        assert ROWS["postponed"]["gamePk"] == ROWS["replayed"]["gamePk"] == 778431

    def test_returned_times_are_utc_aware(self):
        for key in ("ordinary", "postponed", "replayed"):
            commence = resolve_commence_time(ROWS[key])
            assert commence.tzinfo is not None
            assert commence.utcoffset() == dt.timedelta(0)


class TestResolveGameDate:
    def test_is_official_date_verbatim(self):
        assert resolve_game_date(ROWS["ordinary"]) == dt.date(2025, 7, 27)
        assert resolve_game_date(ROWS["replayed"]) == dt.date(2025, 8, 9)

    def test_postponed_row_already_carries_the_new_date(self):
        # StatsAPI moves officialDate forward the moment a game is
        # postponed, which is why no special case is needed here.
        assert resolve_game_date(ROWS["postponed"]) == dt.date(2025, 8, 9)

    def test_is_never_derived_from_the_utc_timestamp(self):
        # A Pacific night game's UTC date is the following day. This asserts
        # the function reads officialDate rather than computing a date from
        # gameDate - the v1 defect.
        row = dict(ROWS["ordinary"])
        row["officialDate"] = "2025-07-27"
        row["gameDate"] = "2025-07-28T02:10:00Z"  # 7:10pm Pacific on the 27th
        assert resolve_game_date(row) == dt.date(2025, 7, 27)
        assert resolve_commence_time(row).date() == dt.date(2025, 7, 28)


class TestRescheduledFrom:
    def test_present_on_the_replayed_row(self):
        assert resolve_rescheduled_from(ROWS["replayed"]) == dt.date(2025, 4, 6)

    def test_absent_on_an_ordinary_game(self):
        assert resolve_rescheduled_from(ROWS["ordinary"]) is None

    def test_absent_on_the_postponed_row_itself(self):
        # The backward link only appears once the game has actually been
        # replayed; before that the row carries the forward rescheduleDate.
        assert resolve_rescheduled_from(ROWS["postponed"]) is None


class TestTeamCrosswalk:
    def test_every_team_on_a_live_slate_resolves(self):
        """The join v1 got wrong, asserted against a real slate.

        v1 matched games by comparing team-name strings and dropped any game
        whose names did not match character for character, with a debug log
        and no error.
        """
        names = {e["home_team"] for e in ODDS_SLATE} | {
            e["away_team"] for e in ODDS_SLATE
        }
        assert names, "fixture slate is empty"
        for name in sorted(names):
            assert resolve_team_id(name) in MLB_TEAM_IDS, name

    def test_covers_all_thirty_clubs(self):
        assert len(MLB_TEAM_IDS) == 30

    def test_unknown_name_raises_rather_than_guessing(self):
        # A silent miss is a game whose prices are never recorded, found
        # weeks later as a hole in the CLV series.
        with pytest.raises(KeyError, match="no MLB team id"):
            resolve_team_id("Oakland As")

    def test_relocated_club_maps_under_both_names(self):
        # The Athletics have no city in StatsAPI's current name. Older Odds
        # API payloads may still say "Oakland Athletics".
        assert resolve_team_id("Athletics") == 133
        assert resolve_team_id("Oakland Athletics") == 133

    def test_ids_are_distinct_apart_from_the_alias(self):
        ids = list(ODDS_API_TEAM_TO_MLB_ID.values())
        assert len(set(ids)) == 30
        assert len(ids) == 31  # the one deliberate alias


class TestVenueTimezones:
    def test_every_venue_has_one(self):
        assert len(VENUE_TIMEZONES) == 30

    def test_lookup(self):
        assert venue_timezone(19) == "America/Denver"  # Coors Field
        assert venue_timezone(15) == "America/Phoenix"  # Chase Field, no DST

    def test_unknown_venue_raises(self):
        with pytest.raises(KeyError, match="no timezone for MLB venue"):
            venue_timezone(999999)

    def test_pacific_venues_identified(self):
        # These are the venues where a night game's UTC date is reliably
        # tomorrow, which is what the migration assertion joins against.
        assert len(PACIFIC_VENUE_IDS) == 6
        assert 22 in PACIFIC_VENUE_IDS  # Dodger Stadium
        assert 680 in PACIFIC_VENUE_IDS  # T-Mobile Park
        assert 19 not in PACIFIC_VENUE_IDS  # Coors is Mountain

    def test_all_timezones_are_iana_names(self):
        # "PDT" is an abbreviation and ambiguous; "America/Los_Angeles" is a
        # zone with a DST history attached.
        for tz in VENUE_TIMEZONES.values():
            assert "/" in tz, tz
