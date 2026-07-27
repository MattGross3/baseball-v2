"""Point-in-time resolution of the closing line, against real Postgres.

Every test here exists because getting the query slightly wrong produces a
plausible number rather than an error - a closing line taken from the wrong
book, the wrong line, the wrong game of a doubleheader, or from after first
pitch all look completely normal in a CLV report.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text

from betting.clv import (
    compute_clv_for_bet,
    find_closing_snapshot,
    find_opposing_closing_snapshot,
)
from database.models import OddsSnapshot
from tests.conftest import FIRST_PITCH, GAME_DATE, GAME_PK, make_bet, make_snapshot

pytestmark = pytest.mark.postgres

H = dt.timedelta(hours=1)


async def closing(session, **overrides):
    kwargs = dict(
        game_pk=GAME_PK,
        market="moneyline",
        selection="away",
        book="pinnacle",
        line=None,
        before=FIRST_PITCH,
    )
    kwargs.update(overrides)
    return await find_closing_snapshot(session, **kwargs)


class TestPointInTime:
    async def test_picks_the_latest_price_before_first_pitch(self, session, run):
        # A morning price, an hour-before price, and an in-game price. The
        # last of those is a live-betting number that must never be mistaken
        # for a closing line.
        session.add_all(
            [
                make_snapshot(run, captured_at=FIRST_PITCH - 3 * H, odds=130),
                make_snapshot(run, captured_at=FIRST_PITCH - 1 * H, odds=110),
                make_snapshot(run, captured_at=FIRST_PITCH + 1 * H, odds=350),
            ]
        )
        await session.flush()

        found = await closing(session)
        assert found is not None
        assert found.odds_american == 110
        assert found.captured_at == FIRST_PITCH - 1 * H

    async def test_excludes_a_price_captured_exactly_at_first_pitch(
        self, session, run
    ):
        # The cutoff is strict. A price stamped at first pitch is already
        # contemporaneous with the game starting.
        session.add_all(
            [
                make_snapshot(run, captured_at=FIRST_PITCH - 1 * H, odds=110),
                make_snapshot(run, captured_at=FIRST_PITCH, odds=350),
            ]
        )
        await session.flush()

        found = await closing(session)
        assert found is not None
        assert found.odds_american == 110

    async def test_none_when_every_price_is_after_the_cutoff(self, session, run):
        session.add(make_snapshot(run, captured_at=FIRST_PITCH + 1 * H, odds=350))
        await session.flush()
        assert await closing(session) is None

    async def test_none_when_there_are_no_prices_at_all(self, session, run):
        assert await closing(session) is None

    async def test_ties_broken_deterministically(self, session, run):
        # Two rows at the identical instant (a re-run writing both sides in
        # one batch). The later-inserted row wins, so repeated calls cannot
        # disagree with each other.
        at = FIRST_PITCH - 1 * H
        session.add_all(
            [
                make_snapshot(run, captured_at=at, odds=110),
                make_snapshot(run, captured_at=at, odds=115),
            ]
        )
        await session.flush()

        first = await closing(session)
        second = await closing(session)
        assert first is not None and second is not None
        assert first.id == second.id
        assert first.odds_american == 115


class TestScoping:
    async def test_respects_book(self, session, run):
        # A soft book's close and a sharp book's close are different
        # numbers; resolving against the wrong one silently rewrites CLV.
        session.add_all(
            [
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 1 * H, odds=110, book="pinnacle"
                ),
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 1 * H, odds=145, book="draftkings"
                ),
            ]
        )
        await session.flush()

        assert (await closing(session, book="pinnacle")).odds_american == 110
        assert (await closing(session, book="draftkings")).odds_american == 145

    async def test_respects_line(self, session, run):
        # Over 8.5 and over 9.0 are different markets, not the same market
        # at a different price. A total that moves from 8.5 to 9.0 must not
        # have its 9.0 price treated as the close for an 8.5 bet.
        session.add_all(
            [
                make_snapshot(
                    run,
                    captured_at=FIRST_PITCH - 2 * H,
                    odds=-110,
                    market="total",
                    selection="over",
                    line=Decimal("8.5"),
                ),
                make_snapshot(
                    run,
                    captured_at=FIRST_PITCH - 1 * H,
                    odds=105,
                    market="total",
                    selection="over",
                    line=Decimal("9.0"),
                ),
            ]
        )
        await session.flush()

        at_85 = await closing(
            session, market="total", selection="over", line=Decimal("8.5")
        )
        at_90 = await closing(
            session, market="total", selection="over", line=Decimal("9.0")
        )
        assert at_85.odds_american == -110
        assert at_90.odds_american == 105

    async def test_line_scale_is_normalised(self, session, run):
        # 8.5 and 8.50 are the same line. NUMERIC(4,2) stores both as 8.50,
        # so a caller passing either must match.
        session.add(
            make_snapshot(
                run,
                captured_at=FIRST_PITCH - 1 * H,
                odds=-110,
                market="total",
                selection="over",
                line=Decimal("8.5"),
            )
        )
        await session.flush()

        for probe in (Decimal("8.5"), Decimal("8.50")):
            found = await closing(
                session, market="total", selection="over", line=probe
            )
            assert found is not None, probe

    async def test_moneyline_matches_on_null_line(self, session, run):
        # Moneyline rows carry line IS NULL. The predicate has to be
        # `IS NULL`, not `IS NOT DISTINCT FROM` - see the note in
        # find_closing_snapshot, and test_uses_the_point_in_time_index.
        session.add(make_snapshot(run, captured_at=FIRST_PITCH - 1 * H, odds=110))
        await session.flush()

        assert (await closing(session, line=None)).odds_american == 110

    async def test_respects_selection(self, session, run):
        session.add_all(
            [
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 1 * H, odds=110, selection="away"
                ),
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 1 * H, odds=-130, selection="home"
                ),
            ]
        )
        await session.flush()

        assert (await closing(session, selection="away")).odds_american == 110
        assert (await closing(session, selection="home")).odds_american == -130

    async def test_doubleheader_isolation(self, session, run):
        # THE constraint, as a test. Both games share a date and both teams;
        # only game_pk separates them. Keying on (date, home, away) would
        # collapse these two into one and mix their prices.
        game_one, game_two = GAME_PK, GAME_PK + 1
        session.add_all(
            [
                make_snapshot(
                    run,
                    captured_at=FIRST_PITCH - 1 * H,
                    odds=110,
                    game_pk=game_one,
                    game_date=GAME_DATE,
                ),
                make_snapshot(
                    run,
                    captured_at=FIRST_PITCH - 1 * H,
                    odds=-175,
                    game_pk=game_two,
                    game_date=GAME_DATE,
                ),
            ]
        )
        await session.flush()

        assert (await closing(session, game_pk=game_one)).odds_american == 110
        assert (await closing(session, game_pk=game_two)).odds_american == -175


class TestOpposingSide:
    async def test_finds_the_other_side_of_the_market(self, session, run):
        session.add_all(
            [
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 1 * H, odds=140, selection="away"
                ),
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 1 * H, odds=-160, selection="home"
                ),
            ]
        )
        await session.flush()

        away = await closing(session, selection="away")
        opposing = await find_opposing_closing_snapshot(
            session, closing=away, before=FIRST_PITCH
        )
        assert opposing is not None
        assert opposing.selection == "home"
        assert opposing.odds_american == -160

    async def test_finds_the_other_side_when_written_milliseconds_later(
        self, session, run
    ):
        # A poll writes each side as its own row and the timestamps can
        # differ slightly. Anchoring on the first side's exact instant would
        # drop whichever side happened to be written second.
        session.add_all(
            [
                make_snapshot(
                    run,
                    captured_at=FIRST_PITCH - 1 * H,
                    odds=140,
                    selection="away",
                ),
                make_snapshot(
                    run,
                    captured_at=FIRST_PITCH - 1 * H + dt.timedelta(milliseconds=120),
                    odds=-160,
                    selection="home",
                ),
            ]
        )
        await session.flush()

        away = await closing(session, selection="away")
        opposing = await find_opposing_closing_snapshot(
            session, closing=away, before=FIRST_PITCH
        )
        assert opposing is not None
        assert opposing.odds_american == -160

    async def test_none_when_only_one_side_was_captured(self, session, run):
        session.add(make_snapshot(run, captured_at=FIRST_PITCH - 1 * H, odds=140))
        await session.flush()

        away = await closing(session)
        assert await find_opposing_closing_snapshot(
            session, closing=away, before=FIRST_PITCH
        ) is None

    async def test_totals_opposite_is_under(self, session, run):
        session.add_all(
            [
                make_snapshot(
                    run,
                    captured_at=FIRST_PITCH - 1 * H,
                    odds=-110,
                    market="total",
                    selection="over",
                    line=Decimal("8.5"),
                ),
                make_snapshot(
                    run,
                    captured_at=FIRST_PITCH - 1 * H,
                    odds=-105,
                    market="total",
                    selection="under",
                    line=Decimal("8.5"),
                ),
            ]
        )
        await session.flush()

        over = await closing(
            session, market="total", selection="over", line=Decimal("8.5")
        )
        opposing = await find_opposing_closing_snapshot(
            session, closing=over, before=FIRST_PITCH
        )
        assert opposing is not None
        assert opposing.selection == "under"
        assert opposing.odds_american == -105


class TestComputeClvForBet:
    async def test_end_to_end_with_both_sides(self, session, run):
        session.add_all(
            [
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 5 * H, odds=130, selection="away"
                ),
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 1 * H, odds=140, selection="away"
                ),
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 1 * H, odds=-160, selection="home"
                ),
            ]
        )
        bet = make_bet(odds=130, selection="away")
        session.add(bet)
        await session.flush()

        result = await compute_clv_for_bet(session, bet.id)
        assert result is not None
        assert result.bet_id == bet.id
        assert result.closing_odds_american == 140
        # Same hand-computed values as tests/test_clv_math.py, now arrived at
        # through the database rather than passed in directly.
        assert result.clv_pct == pytest.approx(-4.166667, abs=1e-6)
        assert result.fair_closing_prob == pytest.approx(0.399123, abs=1e-6)
        assert result.clv_prob_points == pytest.approx(-3.566006, abs=1e-4)
        assert result.beat_close is False

    async def test_price_metric_survives_a_missing_opposing_side(self, session, run):
        session.add(
            make_snapshot(
                run, captured_at=FIRST_PITCH - 1 * H, odds=110, selection="away"
            )
        )
        bet = make_bet(odds=130, selection="away")
        session.add(bet)
        await session.flush()

        result = await compute_clv_for_bet(session, bet.id)
        assert result is not None
        assert result.clv_pct == pytest.approx(9.523810, abs=1e-6)
        assert result.clv_prob_points is None

    async def test_none_when_no_closing_price_exists(self, session, run):
        bet = make_bet(odds=130)
        session.add(bet)
        await session.flush()

        # A gap in the data, not an error - early on there will be many of
        # these, and raising would make the CLI unusable.
        assert await compute_clv_for_bet(session, bet.id) is None

    async def test_require_settled_skips_open_bets(self, session, run):
        session.add(
            make_snapshot(
                run, captured_at=FIRST_PITCH - 1 * H, odds=110, selection="away"
            )
        )
        bet = make_bet(odds=130, selection="away")
        session.add(bet)
        await session.flush()

        assert await compute_clv_for_bet(session, bet.id, require_settled=True) is None
        # CLV is knowable at first pitch, so the default does not wait.
        assert await compute_clv_for_bet(session, bet.id) is not None

    async def test_unknown_bet_raises(self, session):
        with pytest.raises(LookupError, match="no bet with id"):
            await compute_clv_for_bet(session, 999999)

    async def test_bet_resolves_against_its_own_doubleheader_game(self, session, run):
        game_one, game_two = GAME_PK, GAME_PK + 1
        session.add_all(
            [
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 1 * H, odds=110, game_pk=game_one
                ),
                make_snapshot(
                    run, captured_at=FIRST_PITCH - 1 * H, odds=-175, game_pk=game_two
                ),
            ]
        )
        bet = make_bet(odds=130, game_pk=game_two)
        session.add(bet)
        await session.flush()

        result = await compute_clv_for_bet(session, bet.id)
        assert result is not None
        assert result.closing_odds_american == -175


class TestIndexUsage:
    """The planner must reach the closing line by an ORDERED index scan.

    Asserting merely that the index is *touched* is not enough, and an
    earlier version of this test made exactly that mistake: it passed while
    the query was doing a bitmap scan over the index followed by a top-N
    sort, reading every matching row. The name of the index appears in the
    plan either way. What matters is the absence of a Sort node - that is
    what distinguishes "seek and stop at the first row" from "read them all
    and order them".
    """

    async def _load(self, session, run, *, market: str, line, count: int = 8000):
        rows = [
            {
                "ingest_run_id": run.id,
                "game_pk": GAME_PK + (i % 400),
                "game_date": GAME_DATE,
                "commence_time_utc": FIRST_PITCH,
                "book": ["pinnacle", "draftkings", "fanduel"][i % 3],
                "market": market,
                "selection": (
                    ("over" if i % 2 else "under")
                    if market == "total"
                    else ("away" if i % 2 else "home")
                ),
                "line": line,
                "odds_american": 100 + (i % 300),
                "captured_at": FIRST_PITCH - dt.timedelta(minutes=i % 600),
            }
            for i in range(count)
        ]
        await session.execute(OddsSnapshot.__table__.insert(), rows)
        await session.commit()
        await session.execute(text("ANALYZE odds_snapshots"))

    async def _plan(self, session, *, market, selection, line_sql):
        return "\n".join(
            r[0]
            for r in (
                await session.execute(
                    text(
                        f"""
                        EXPLAIN
                        SELECT odds_american, captured_at FROM odds_snapshots
                        WHERE game_pk = :pk AND market = '{market}'
                          AND selection = '{selection}' AND book = 'pinnacle'
                          AND {line_sql} AND captured_at < :before
                        ORDER BY captured_at DESC, id DESC
                        LIMIT 1
                        """
                    ),
                    {"pk": GAME_PK + 7, "before": FIRST_PITCH},
                )
            ).all()
        )

    async def test_moneyline_uses_an_ordered_index_scan(self, session, run):
        # Moneyline is the case that breaks if `line` is put into the index
        # ahead of captured_at: `line IS NULL` is a valid scan key but not
        # an equality for pathkey purposes, so the ordering guarantee on
        # captured_at is lost and Postgres sorts instead.
        await self._load(session, run, market="moneyline", line=None)
        plan = await self._plan(
            session, market="moneyline", selection="away", line_sql="line IS NULL"
        )

        assert "ix_odds_snapshots_pit" in plan, plan
        assert "Seq Scan" not in plan, plan
        assert "Index Scan Backward" in plan, plan
        # THE assertion. A Sort here means the index stopped supplying order.
        assert "Sort" not in plan, plan
        assert "Bitmap" not in plan, plan

    async def test_totals_uses_an_ordered_index_scan(self, session, run):
        await self._load(session, run, market="total", line=Decimal("8.5"))
        plan = await self._plan(
            session, market="total", selection="over", line_sql="line = 8.5"
        )

        assert "ix_odds_snapshots_pit" in plan, plan
        assert "Seq Scan" not in plan, plan
        assert "Index Scan Backward" in plan, plan
        assert "Sort" not in plan, plan
        assert "Bitmap" not in plan, plan

    async def test_line_is_a_filter_not_an_index_column(self, session, run):
        # Documents the deliberate omission, so that "tightening" the index
        # by adding `line` fails here with an explanation rather than
        # silently costing a sort on every moneyline lookup.
        definition = (
            await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'ix_odds_snapshots_pit'"
                )
            )
        ).scalar_one()
        assert "line" not in definition, definition
        assert (
            "(game_pk, market, selection, book, captured_at, id)" in definition
        ), definition
