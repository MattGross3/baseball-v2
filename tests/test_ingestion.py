"""Ingestion: schedule upsert, odds parsing, storage.

Fixtures are REAL captures - a live Odds API slate and real StatsAPI schedule
rows - not invented ones. The bar these are written to: a wrong price cannot
be written, an unknown team raises, a doubleheader resolves to two distinct
pks, and a failure lands in ingest_runs.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from decimal import Decimal

import pytest
from sqlalchemy import select

from database.enums import IngestStatus
from database.ingest_run import ingest_run
from database.models import Game, IngestRun, OddsSnapshot
from ingestion.odds import (
    MARKETS,
    UnknownGame,
    _game_dates,
    load_game_index,
    parse_capture,
    store_snapshots,
)
from ingestion.schedule import game_values

_FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SLATE = json.loads((_FIXTURES / "odds_api_h2h_sample.json").read_text(encoding="utf-8"))
ROWS = json.loads(
    (_FIXTURES / "statsapi_schedule_rows.json").read_text(encoding="utf-8")
)

UTC = dt.UTC


def _capture(events: list[dict]) -> dict:
    return {
        "captured_at": "2026-07-27T22:00:00+00:00",
        "markets": list(MARKETS),
        "regions": ["us"],
        "headers": {"x-requests-last": "2", "x-requests-remaining": "400"},
        "events": events,
    }


def _index_for(events: list[dict], start_pk: int = 800001) -> dict:
    """Build a (home, away) -> [(commence, game_pk)] index covering a slate."""
    from ingestion.reference import resolve_team_id

    index: dict = {}
    for offset, event in enumerate(events):
        commence = dt.datetime.fromisoformat(
            event["commence_time"].replace("Z", "+00:00")
        ).astimezone(UTC)
        key = (
            resolve_team_id(event["home_team"]),
            resolve_team_id(event["away_team"]),
        )
        index.setdefault(key, []).append((commence, start_pk + offset))
    return index


def _event_for(home: str, away: str, commence: str) -> dict:
    """A real fixture event re-labelled for different clubs.

    The outcome names have to move with the team names: `_classify_outcome`
    matches an outcome to home/away by comparing those strings, so renaming
    only the top-level fields yields an event with no recognisable
    selections at all.
    """
    event = json.loads(json.dumps(SLATE[0]))
    old_home, old_away = event["home_team"], event["away_team"]
    event["home_team"], event["away_team"] = home, away
    event["commence_time"] = commence
    for book in event["bookmakers"]:
        for market in book["markets"]:
            for outcome in market["outcomes"]:
                if outcome["name"] == old_home:
                    outcome["name"] = home
                elif outcome["name"] == old_away:
                    outcome["name"] = away
    return event


class TestParseCapture:
    def test_parses_a_real_slate(self):
        capture = _capture(SLATE)
        rows = parse_capture(capture, _index_for(SLATE))

        assert rows, "real slate produced no rows"
        # Every row must be storable: the schema's CHECK constraints are the
        # spec, and the parser must not produce anything they would reject.
        for row in rows:
            assert row["market"] in ("moneyline", "total")
            assert row["selection"] in ("home", "away", "over", "under")
            assert row["odds_american"] <= -100 or row["odds_american"] >= 100
            if row["market"] == "moneyline":
                assert row["line"] is None
            else:
                assert isinstance(row["line"], Decimal)

    def test_records_every_book_returned(self):
        # No bookmakers filter: whatever the API gives us is stored. Best
        # price across books is only possible if every book is kept.
        rows = parse_capture(_capture(SLATE), _index_for(SLATE))
        books = {r["book"] for r in rows}
        assert len(books) >= 5, books
        assert "draftkings" in books
        assert "fanduel" in books

    def test_home_and_away_are_not_transposed(self):
        # The failure that would invert every CLV number while looking
        # entirely normal.
        event = SLATE[0]
        rows = parse_capture(_capture([event]), _index_for([event]))
        ml = [r for r in rows if r["market"] == "moneyline"]

        raw = {
            o["name"]: o["price"]
            for b in event["bookmakers"]
            if b["key"] == ml[0]["book"]
            for m in b["markets"]
            if m["key"] == "h2h"
            for o in m["outcomes"]
        }
        for row in ml:
            if row["book"] != ml[0]["book"]:
                continue
            expected_name = (
                event["home_team"] if row["selection"] == "home" else event["away_team"]
            )
            assert row["odds_american"] == raw[expected_name]

    def test_unknown_team_raises(self):
        event = json.loads(json.dumps(SLATE[0]))
        event["home_team"] = "Portland Beavers"
        with pytest.raises(KeyError, match="no MLB team id"):
            parse_capture(_capture([event]), _index_for(SLATE))

    def test_unresolvable_game_raises_rather_than_dropping(self):
        # v1 logged this at debug and moved on, losing that game's prices
        # silently. It must be loud.
        with pytest.raises(UnknownGame, match="no game for"):
            parse_capture(_capture([SLATE[0]]), {})

    def test_a_night_game_resolves_to_the_previous_local_date(self):
        # A 7pm Pacific first pitch is 02:00 UTC tomorrow; the game belongs
        # to today's slate. The index is keyed on venue-local game_date.
        event = json.loads(json.dumps(SLATE[0]))
        event["commence_time"] = "2026-07-28T02:10:00Z"
        from ingestion.reference import resolve_team_id

        index = {
            (
                resolve_team_id(event["home_team"]),
                resolve_team_id(event["away_team"]),
            ): [(dt.datetime(2026, 7, 28, 2, 10, tzinfo=UTC), 800999)]
        }
        rows = parse_capture(_capture([event]), index)
        assert rows
        assert {r["game_pk"] for r in rows} == {800999}

    def test_ignores_markets_we_did_not_ask_for(self):
        event = json.loads(json.dumps(SLATE[0]))
        for book in event["bookmakers"]:
            book["markets"].append(
                {
                    "key": "spreads",
                    "outcomes": [
                        {"name": event["home_team"], "price": -110, "point": -1.5}
                    ],
                }
            )
        rows = parse_capture(_capture([event]), _index_for([event]))
        assert all(r["market"] != "run_line" for r in rows)


@pytest.mark.postgres
class TestScheduleUpsert:
    async def test_game_values_from_a_real_row(self, session):
        values = game_values(ROWS["ordinary"])
        assert values["game_pk"] == 776986
        assert values["game_date"] == dt.date(2025, 7, 27)
        assert values["commence_time_utc"] == dt.datetime(
            2025, 7, 27, 17, 35, tzinfo=UTC
        )
        assert values["status"] == "Final"
        assert values["rescheduled_from_date"] is None

    async def test_postponed_row_stores_the_resolved_pair(self, session):
        # game_date and commence_time_utc must describe the same event even
        # though the source contradicts itself.
        values = game_values(ROWS["postponed"])
        assert values["game_date"] == dt.date(2025, 8, 9)
        assert values["commence_time_utc"].date() == dt.date(2025, 8, 9)

    async def test_upsert_is_idempotent_and_updates_in_place(self, session):
        # A game's status and times change over its life; re-running must
        # update rather than duplicate or fail.
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        first = game_values(ROWS["postponed"])
        second = game_values(ROWS["replayed"])
        assert first["game_pk"] == second["game_pk"]

        for values in (first, second):
            stmt = pg_insert(Game).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[Game.game_pk],
                set_={k: stmt.excluded[k] for k in values if k != "game_pk"},
            )
            await session.execute(stmt)
        await session.commit()

        # Scoped to this pk: conftest seeds a block of games for the FK.
        games = (
            (await session.execute(select(Game).where(Game.game_pk == 778431)))
            .scalars()
            .all()
        )
        assert len(games) == 1, "upsert duplicated instead of updating in place"
        assert games[0].status == "Final"
        assert games[0].rescheduled_from_date == dt.date(2025, 4, 6)


@pytest.mark.postgres
class TestGameIndexAndStorage:
    async def _game(self, session, *, pk, home, away, date, commence, venue=22):
        session.add(
            Game(
                game_pk=pk,
                game_date=date,
                commence_time_utc=commence,
                home_team_id=home,
                away_team_id=away,
                venue_id=venue,
                status="Scheduled",
            )
        )
        await session.commit()

    async def test_doubleheader_yields_two_distinct_pks(self, session):
        """Two games, same clubs, same date. They must stay separate.

        This is the constraint the whole schema is keyed around, asserted at
        the ingest layer rather than only at the storage layer.
        """
        date = dt.date(2026, 7, 27)
        await self._game(
            session,
            pk=810001,
            home=147,
            away=145,
            date=date,
            commence=dt.datetime(2026, 7, 27, 17, 5, tzinfo=UTC),
        )
        await self._game(
            session,
            pk=810002,
            home=147,
            away=145,
            date=date,
            commence=dt.datetime(2026, 7, 27, 23, 5, tzinfo=UTC),
        )

        games = (
            (
                await session.execute(
                    select(Game).where(Game.game_pk.in_([810001, 810002]))
                )
            )
            .scalars()
            .all()
        )
        assert {g.game_pk for g in games} == {810001, 810002}
        # Same clubs, same date, two rows. Keyed only by game_pk, so a
        # (date, home, away) key would have collapsed them into one.
        assert len({(g.home_team_id, g.away_team_id, g.game_date) for g in games}) == 1
        assert len({g.game_pk for g in games}) == 2
        # And they are separable by start time, which is what a poller needs.
        assert len({g.commence_time_utc for g in games}) == 2

    async def test_the_poller_separates_a_doubleheader(self, session):
        """The bug the first live poll actually had.

        Odds API lists a doubleheader's games as separate events with
        separate commence_times. An index keyed on (home, away, date) - or
        on clubs alone without comparing start times - collapses them, and
        one game's prices get filed against the other's pk while the other
        gets none. On 2026-07-28 that happened for real: Guardians @ Reds,
        pk 824490 at 17:40Z and pk 824489 at 23:10Z, with the 17:41Z event
        resolving to 824489.

        Resolution is by clubs AND nearest start time.
        """
        date = dt.date(2026, 7, 28)
        early = dt.datetime(2026, 7, 28, 17, 40, tzinfo=UTC)
        late = dt.datetime(2026, 7, 28, 23, 10, tzinfo=UTC)
        await self._game(
            session, pk=824490, home=113, away=114, date=date, commence=early
        )
        await self._game(
            session, pk=824489, home=113, away=114, date=date, commence=late
        )

        index = await load_game_index(session, date)
        assert len(index[(113, 114)]) == 2

        # Game one's event, a minute off the scheduled time as observed.
        game_one = _event_for(
            "Cincinnati Reds", "Cleveland Guardians", "2026-07-28T17:41:00Z"
        )
        rows = parse_capture(_capture([game_one]), index)
        assert rows, "no rows parsed"
        assert {r["game_pk"] for r in rows} == {824490}, "game one filed wrongly"

        # Game two's event.
        game_two = _event_for(
            "Cincinnati Reds", "Cleveland Guardians", "2026-07-28T23:12:00Z"
        )
        rows = parse_capture(_capture([game_two]), index)
        assert {r["game_pk"] for r in rows} == {824489}, "game two filed wrongly"

    async def test_a_wildly_wrong_start_time_does_not_match(self, session):
        # Better to raise than to file prices against a game that merely
        # looks plausible.
        date = dt.date(2026, 7, 28)
        await self._game(
            session,
            pk=824491,
            home=113,
            away=114,
            date=date,
            commence=dt.datetime(2026, 7, 28, 17, 40, tzinfo=UTC),
        )
        index = await load_game_index(session, date)

        event = _event_for(
            "Cincinnati Reds", "Cleveland Guardians", "2026-07-29T17:40:00Z"
        )  # a day out
        with pytest.raises(UnknownGame):
            parse_capture(_capture([event]), index)

    async def test_store_snapshots_writes_and_is_idempotent(self, session):
        date = dt.date(2026, 7, 27)
        commence = dt.datetime(2026, 7, 27, 23, 5, tzinfo=UTC)
        await self._game(
            session, pk=810003, home=147, away=145, date=date, commence=commence
        )

        rows = [
            {
                "game_pk": 810003,
                "book": "draftkings",
                "market": "moneyline",
                "selection": "home",
                "line": None,
                "odds_american": -150,
            }
        ]
        captured = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)

        async with ingest_run(session, source="test", run_kind="odds_poll") as run:
            dates = await _game_dates(session, {810003})
            written = await store_snapshots(
                session, rows, run_id=run.id, captured_at=captured, game_dates=dates
            )
            run.rows_written = written
        await session.commit()
        assert written == 1

        # Same capture again: no duplicate observation.
        async with ingest_run(session, source="test", run_kind="odds_poll") as run:
            dates = await _game_dates(session, {810003})
            again = await store_snapshots(
                session, rows, run_id=run.id, captured_at=captured, game_dates=dates
            )
            run.rows_written = again
        await session.commit()
        assert again == 0

        stored = (await session.execute(select(OddsSnapshot))).scalars().all()
        assert len(stored) == 1
        # game_date came from `games`, not from the UTC timestamp.
        assert stored[0].game_date == date

    async def test_snapshot_cannot_reference_a_missing_game(self, session):
        # The FK is what makes a typo'd game_pk fail loudly instead of
        # producing a price nothing can ever join to.
        from sqlalchemy import exc as sa_exc

        # The run is opened and closed cleanly first. Provoking the failure
        # INSIDE the context manager would leave its success path flushing a
        # transaction that pytest.raises had already rolled back.
        async with ingest_run(session, source="test", run_kind="odds_poll") as run:
            run.rows_written = 0
            run_id = run.id
        await session.commit()

        session.add(
            OddsSnapshot(
                ingest_run_id=run_id,
                game_pk=999999,
                game_date=dt.date(2026, 7, 27),
                commence_time_utc=dt.datetime(2026, 7, 27, 23, 5, tzinfo=UTC),
                book="draftkings",
                market="moneyline",
                selection="home",
                line=None,
                odds_american=-150,
                captured_at=dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC),
            )
        )
        with pytest.raises(sa_exc.IntegrityError, match="fk_odds_snapshots_game"):
            await session.flush()
        await session.rollback()

    async def test_a_game_cannot_be_deleted_out_from_under_its_prices(self, session):
        """ON DELETE RESTRICT, not CASCADE.

        Nothing should ever delete a game. If something tries, it must fail
        rather than silently take the price history - which cannot be
        re-fetched at any price.
        """
        from sqlalchemy import delete
        from sqlalchemy import exc as sa_exc

        date = dt.date(2026, 7, 27)
        commence = dt.datetime(2026, 7, 27, 23, 5, tzinfo=UTC)
        await self._game(
            session, pk=810004, home=147, away=145, date=date, commence=commence
        )
        async with ingest_run(session, source="test", run_kind="odds_poll") as run:
            dates = await _game_dates(session, {810004})
            await store_snapshots(
                session,
                [
                    {
                        "game_pk": 810004,
                        "book": "fanduel",
                        "market": "moneyline",
                        "selection": "away",
                        "line": None,
                        "odds_american": 130,
                    }
                ],
                run_id=run.id,
                captured_at=dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC),
                game_dates=dates,
            )
            run.rows_written = 1
        await session.commit()

        async def delete_the_game() -> None:
            await session.execute(delete(Game).where(Game.game_pk == 810004))
            await session.flush()

        with pytest.raises(sa_exc.IntegrityError, match="fk_odds_snapshots_game"):
            await delete_the_game()
        await session.rollback()

    async def test_load_game_index_keys_on_local_date(self, session):
        date = dt.date(2026, 7, 27)
        # Clubs the conftest seed block does not use, so this sees only its
        # own game.
        await self._game(
            session,
            pk=810005,
            home=141,
            away=110,
            date=date,
            commence=dt.datetime(2026, 7, 28, 2, 10, tzinfo=UTC),  # 7:10pm PT
        )
        index = await load_game_index(session, date)
        assert index[(141, 110)] == [
            (dt.datetime(2026, 7, 28, 2, 10, tzinfo=UTC), 810005)
        ]


@pytest.mark.postgres
class TestFailuresAreVisible:
    async def test_a_parse_failure_lands_in_ingest_runs(self, session):
        """No retry framework: the failure is recorded and cron tries again.

        What matters is that it is not silent.
        """

        async def failing_poll() -> None:
            async with ingest_run(session, source="the-odds-api", run_kind="odds_poll"):
                raise UnknownGame("no game for Foo @ Bar")

        with pytest.raises(UnknownGame):
            await failing_poll()

        await session.rollback()
        run = (await session.execute(select(IngestRun))).scalars().one()
        assert run.status == IngestStatus.FAILED.value
        assert "no game for" in run.error
        assert run.finished_at is not None
