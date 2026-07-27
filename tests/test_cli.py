"""CLI parsing and operations.

Parsing tests are pure. Operation tests use the plain async functions rather
than shelling out, so a failure points at the logic instead of at a
subprocess's exit code.
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from betting.cli import (
    add_snapshot,
    build_parser,
    list_bets,
    log_bet,
    parse_american,
    parse_date,
    parse_stake_cents,
    parse_utc,
    settle_bet,
    validate_market_selection,
)
from betting.clv import compute_clv_for_bet
from database.enums import BetStatus, IngestStatus
from database.ingest_run import ingest_run
from database.models import Bet, IngestRun, OddsSnapshot
from tests.conftest import FIRST_PITCH, GAME_DATE, GAME_PK

H = dt.timedelta(hours=1)


class TestParseAmerican:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("+130", 130), ("130", 130), ("-150", -150), (" -150 ", -150), ("+100", 100)],
    )
    def test_accepts_signed_and_unsigned(self, text, expected):
        assert parse_american(text) == expected

    @pytest.mark.parametrize("text", ["50", "-99", "0", "abc", "1.5", ""])
    def test_rejects_invalid(self, text):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_american(text)


class TestNegativeOddsOnTheCommandLine:
    def test_equals_form(self):
        # The form the help text recommends: unambiguous regardless of what
        # other flags the parser grows later.
        args = build_parser().parse_args(
            [
                "devig",
                "--odds=-150",
                "--odds=+130",
            ]
        )
        assert args.odds == [-150, 130]

    def test_space_separated_form_also_works(self):
        # argparse treats a negative number as a value when no option string
        # looks like one. True today; asserted so that adding a numeric-
        # looking flag later fails here rather than in someone's terminal.
        args = build_parser().parse_args(["devig", "--odds", "-150", "--odds", "+130"])
        assert args.odds == [-150, 130]

    def test_rejected_price_reports_a_useful_message(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["devig", "--odds=50"])
        assert "not a valid American price" in capsys.readouterr().err


class TestParseStake:
    @pytest.mark.parametrize(
        ("text", "cents"),
        [
            ("50", 5000),
            ("50.00", 5000),
            # int(float("50.10") * 100) is 5009 - the reason this goes
            # through Decimal.
            ("50.10", 5010),
            ("0.01", 1),
            ("$25.50", 2550),
            ("1234.56", 123456),
        ],
    )
    def test_exact_cents(self, text, cents):
        assert parse_stake_cents(text) == cents

    @pytest.mark.parametrize("text", ["0", "-5", "abc", ""])
    def test_rejects_invalid(self, text):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_stake_cents(text)

    def test_rejects_sub_cent_precision(self):
        # At this scale a third decimal place is a typo, not a real stake -
        # silently rounding it would hide the mistake.
        with pytest.raises(argparse.ArgumentTypeError, match="sub-cent"):
            parse_stake_cents("50.005")


class TestParseTime:
    def test_z_suffix(self):
        assert parse_utc("2026-07-27T23:05:00Z") == FIRST_PITCH

    def test_explicit_offset_is_converted_to_utc(self):
        assert parse_utc("2026-07-27T19:05:00-04:00") == FIRST_PITCH

    def test_rejects_naive_timestamp(self):
        # The convention only holds if nothing ever guesses a zone.
        with pytest.raises(argparse.ArgumentTypeError, match="no timezone offset"):
            parse_utc("2026-07-27T23:05:00")

    def test_rejects_garbage(self):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_utc("yesterday")

    def test_date_parsing(self):
        assert parse_date("2026-07-27") == GAME_DATE
        with pytest.raises(argparse.ArgumentTypeError):
            parse_date("07/27/2026")


class TestMarketValidation:
    def test_moneyline_rejects_a_line(self):
        with pytest.raises(ValueError, match="has no line"):
            validate_market_selection("moneyline", "home", Decimal("8.5"))

    def test_total_requires_a_line(self):
        with pytest.raises(ValueError, match="needs --line"):
            validate_market_selection("total", "over", None)

    def test_total_rejects_home_away(self):
        with pytest.raises(ValueError, match="not valid for a total market"):
            validate_market_selection("total", "home", Decimal("8.5"))

    def test_moneyline_rejects_over_under(self):
        with pytest.raises(ValueError, match="not valid for a moneyline market"):
            validate_market_selection("moneyline", "over", None)

    def test_accepts_valid_combinations(self):
        assert validate_market_selection("moneyline", "home", None) == (
            "moneyline",
            "home",
            None,
        )
        assert validate_market_selection("total", "over", Decimal("8.5")) == (
            "total",
            "over",
            Decimal("8.5"),
        )


# --- database-backed -------------------------------------------------------

pytestmark_db = pytest.mark.postgres


@pytest.mark.postgres
class TestLogBet:
    async def test_persists_every_field(self, session):
        bet = await log_bet(
            session,
            game_pk=GAME_PK,
            game_date=GAME_DATE,
            commence_time_utc=FIRST_PITCH,
            book="pinnacle",
            market="moneyline",
            selection="away",
            odds_american=130,
            stake_cents=5000,
            notes="test entry",
        )
        await session.commit()

        stored = (await session.execute(select(Bet))).scalars().one()
        assert stored.id == bet.id
        assert stored.game_pk == GAME_PK
        assert stored.game_date == GAME_DATE
        assert stored.commence_time_utc == FIRST_PITCH
        assert stored.book == "pinnacle"
        assert stored.market == "moneyline"
        assert stored.selection == "away"
        assert stored.line is None
        assert stored.odds_american == 130
        assert stored.stake_cents == 5000
        assert stored.status == BetStatus.OPEN.value
        assert stored.settled_at is None
        assert stored.payout_cents is None
        assert stored.notes == "test entry"

    async def test_opens_an_ingest_run(self, session):
        # Hand-entered data carries the same provenance as scraped data;
        # nothing in the database is of unknown origin.
        await log_bet(
            session,
            game_pk=GAME_PK,
            game_date=GAME_DATE,
            commence_time_utc=FIRST_PITCH,
            book="pinnacle",
            market="moneyline",
            selection="away",
            odds_american=130,
            stake_cents=5000,
        )
        await session.commit()

        run = (await session.execute(select(IngestRun))).scalars().one()
        assert run.source == "manual-cli"
        assert run.run_kind == "bet_log"
        assert run.status == IngestStatus.SUCCESS.value
        assert run.rows_written == 1
        assert run.finished_at is not None

    async def test_rejects_bad_market_before_touching_the_database(self, session):
        with pytest.raises(ValueError, match="has no line"):
            await log_bet(
                session,
                game_pk=GAME_PK,
                game_date=GAME_DATE,
                commence_time_utc=FIRST_PITCH,
                book="pinnacle",
                market="moneyline",
                selection="home",
                line=Decimal("8.5"),
                odds_american=130,
                stake_cents=5000,
            )
        await session.rollback()
        assert (await session.execute(select(IngestRun))).scalars().all() == []

    async def test_totals_bet_keeps_its_line(self, session):
        await log_bet(
            session,
            game_pk=GAME_PK,
            game_date=GAME_DATE,
            commence_time_utc=FIRST_PITCH,
            book="pinnacle",
            market="total",
            selection="over",
            line=Decimal("8.5"),
            odds_american=-110,
            stake_cents=11000,
        )
        await session.commit()

        stored = (await session.execute(select(Bet))).scalars().one()
        assert stored.line == Decimal("8.50")


@pytest.mark.postgres
class TestSettleBet:
    async def _open_bet(self, session, odds=-150, stake=5000):
        bet = await log_bet(
            session,
            game_pk=GAME_PK,
            game_date=GAME_DATE,
            commence_time_utc=FIRST_PITCH,
            book="pinnacle",
            market="moneyline",
            selection="away",
            odds_american=odds,
            stake_cents=stake,
        )
        await session.commit()
        return bet

    async def test_computes_the_payout(self, session):
        bet = await self._open_bet(session)
        settled = await settle_bet(session, bet.id, BetStatus.WON)
        await session.commit()

        assert settled.status == "won"
        assert settled.payout_cents == 8333  # 5000 * (1 + 100/150), HALF_UP
        assert settled.settled_at is not None

    async def test_loss_returns_nothing(self, session):
        bet = await self._open_bet(session)
        settled = await settle_bet(session, bet.id, BetStatus.LOST)
        await session.commit()
        assert settled.payout_cents == 0

    async def test_push_returns_the_stake(self, session):
        bet = await self._open_bet(session)
        settled = await settle_bet(session, bet.id, BetStatus.PUSH)
        await session.commit()
        assert settled.payout_cents == 5000

    async def test_explicit_payout_overrides(self, session):
        # What the book actually paid is the fact worth storing.
        bet = await self._open_bet(session)
        settled = await settle_bet(session, bet.id, BetStatus.WON, payout_cents=8334)
        await session.commit()
        assert settled.payout_cents == 8334

    async def test_cannot_settle_twice(self, session):
        bet = await self._open_bet(session)
        await settle_bet(session, bet.id, BetStatus.WON)
        await session.commit()

        with pytest.raises(ValueError, match="already settled"):
            await settle_bet(session, bet.id, BetStatus.LOST)

    async def test_cannot_settle_to_open(self, session):
        bet = await self._open_bet(session)
        with pytest.raises(ValueError, match="cannot settle a bet to 'open'"):
            await settle_bet(session, bet.id, BetStatus.OPEN)

    async def test_unknown_bet(self, session):
        with pytest.raises(LookupError, match="no bet with id"):
            await settle_bet(session, 999999, BetStatus.WON)


@pytest.mark.postgres
class TestSnapshotAdd:
    async def test_persists_and_is_append_only(self, session):
        for offset, odds in ((3, 130), (1, 110)):
            await add_snapshot(
                session,
                game_pk=GAME_PK,
                game_date=GAME_DATE,
                commence_time_utc=FIRST_PITCH,
                book="pinnacle",
                market="moneyline",
                selection="away",
                odds_american=odds,
                captured_at=FIRST_PITCH - offset * H,
            )
        await session.commit()

        stored = (
            (await session.execute(select(OddsSnapshot).order_by(OddsSnapshot.id)))
            .scalars()
            .all()
        )
        assert [s.odds_american for s in stored] == [130, 110]
        # Two runs, one per command, each accounting for its own row.
        runs = (await session.execute(select(IngestRun))).scalars().all()
        assert len(runs) == 2
        assert all(r.run_kind == "snapshot_add" for r in runs)

    async def test_defaults_captured_at_to_the_ingest_run_start(self, session):
        # NOT now(). Every price from one poll shares the run's timestamp,
        # which is what lets the unique constraint recognise a retry - a
        # fresh now() on the retry would collide with nothing.
        snapshot = await add_snapshot(
            session,
            game_pk=GAME_PK,
            game_date=GAME_DATE,
            commence_time_utc=FIRST_PITCH,
            book="pinnacle",
            market="moneyline",
            selection="away",
            odds_american=130,
        )
        await session.commit()

        run = (await session.execute(select(IngestRun))).scalars().one()
        assert snapshot.captured_at == run.started_at

    async def test_all_prices_from_one_run_share_a_timestamp(self, session):
        # The property the opposing-side lookup depends on.
        common = {
            "game_pk": GAME_PK,
            "game_date": GAME_DATE,
            "commence_time_utc": FIRST_PITCH,
            "book": "pinnacle",
            "market": "moneyline",
        }
        async with ingest_run(session, source="test", run_kind="odds_poll") as poll:
            for selection, odds in (("home", -130), ("away", 110)):
                session.add(
                    OddsSnapshot(
                        ingest_run_id=poll.id,
                        selection=selection,
                        odds_american=odds,
                        captured_at=poll.started_at,
                        **common,
                    )
                )
            poll.rows_written = 2
        await session.commit()

        stored = (await session.execute(select(OddsSnapshot))).scalars().all()
        assert len({s.captured_at for s in stored}) == 1

    async def test_re_recording_the_same_observation_is_a_no_op(self, session):
        """Retried ingest runs must be idempotent.

        ON CONFLICT DO NOTHING inserts or it does not - it never rewrites an
        existing row, so append-only holds. The second call returns None
        rather than raising or duplicating.
        """
        args = {
            "game_pk": GAME_PK,
            "game_date": GAME_DATE,
            "commence_time_utc": FIRST_PITCH,
            "book": "pinnacle",
            "market": "moneyline",
            "selection": "away",
            "odds_american": 130,
            "captured_at": FIRST_PITCH - H,
        }
        first = await add_snapshot(session, **args)
        await session.commit()
        second = await add_snapshot(session, **args)
        await session.commit()

        assert first is not None
        assert second is None
        stored = (await session.execute(select(OddsSnapshot))).scalars().all()
        assert len(stored) == 1

    async def test_a_no_op_write_is_recorded_as_zero_rows(self, session):
        # The ingest run still happened; it just wrote nothing. Reporting 1
        # would make a retry storm look like real data collection.
        args = {
            "game_pk": GAME_PK,
            "game_date": GAME_DATE,
            "commence_time_utc": FIRST_PITCH,
            "book": "pinnacle",
            "market": "moneyline",
            "selection": "away",
            "odds_american": 130,
            "captured_at": FIRST_PITCH - H,
        }
        await add_snapshot(session, **args)
        await session.commit()
        await add_snapshot(session, **args)
        await session.commit()

        runs = (
            (await session.execute(select(IngestRun).order_by(IngestRun.id)))
            .scalars()
            .all()
        )
        assert [r.rows_written for r in runs] == [1, 0]

    async def test_a_price_change_at_a_new_instant_is_a_new_observation(self, session):
        args = {
            "game_pk": GAME_PK,
            "game_date": GAME_DATE,
            "commence_time_utc": FIRST_PITCH,
            "book": "pinnacle",
            "market": "moneyline",
            "selection": "away",
        }
        await add_snapshot(
            session, **args, odds_american=130, captured_at=FIRST_PITCH - 2 * H
        )
        await add_snapshot(
            session, **args, odds_american=110, captured_at=FIRST_PITCH - H
        )
        await session.commit()

        stored = (await session.execute(select(OddsSnapshot))).scalars().all()
        assert len(stored) == 2

    async def test_an_unchanged_price_at_a_new_instant_is_still_recorded(self, session):
        # "We checked at T and it had not moved" is information, and the
        # unique key must not swallow it.
        args = {
            "game_pk": GAME_PK,
            "game_date": GAME_DATE,
            "commence_time_utc": FIRST_PITCH,
            "book": "pinnacle",
            "market": "moneyline",
            "selection": "away",
            "odds_american": 130,
        }
        await add_snapshot(session, **args, captured_at=FIRST_PITCH - 2 * H)
        await add_snapshot(session, **args, captured_at=FIRST_PITCH - H)
        await session.commit()

        stored = (await session.execute(select(OddsSnapshot))).scalars().all()
        assert len(stored) == 2

    async def test_rejects_naive_captured_at(self, session):
        with pytest.raises(ValueError, match="naive"):
            await add_snapshot(
                session,
                game_pk=GAME_PK,
                game_date=GAME_DATE,
                commence_time_utc=FIRST_PITCH,
                book="pinnacle",
                market="moneyline",
                selection="away",
                odds_american=130,
                captured_at=dt.datetime(2026, 7, 27, 22, 0),
            )


@pytest.mark.postgres
class TestListBets:
    async def _bet(self, session, *, odds, game_date=GAME_DATE):
        return await log_bet(
            session,
            game_pk=GAME_PK,
            game_date=game_date,
            commence_time_utc=FIRST_PITCH,
            book="pinnacle",
            market="moneyline",
            selection="away",
            odds_american=odds,
            stake_cents=5000,
        )

    async def test_filters_by_status(self, session):
        first = await self._bet(session, odds=130)
        await self._bet(session, odds=140)
        await session.commit()
        await settle_bet(session, first.id, BetStatus.WON)
        await session.commit()

        assert len(await list_bets(session)) == 2
        assert len(await list_bets(session, status="open")) == 1
        assert len(await list_bets(session, status="won")) == 1

    async def test_filters_by_game_date(self, session):
        await self._bet(session, odds=130)
        await self._bet(session, odds=140, game_date=dt.date(2026, 7, 28))
        await session.commit()

        assert len(await list_bets(session, game_date=GAME_DATE)) == 1
        assert len(await list_bets(session, game_date=dt.date(2026, 7, 28))) == 1


@pytest.mark.postgres
class TestEndToEnd:
    async def test_full_loop(self, session):
        """snapshot add x3 -> bet log -> settle -> clv, the whole phase.

        Numbers match tests/test_clv_math.py, arrived at here through the
        database and the CLI layer rather than passed straight in.
        """
        common = {
            "game_pk": GAME_PK,
            "game_date": GAME_DATE,
            "commence_time_utc": FIRST_PITCH,
            "book": "pinnacle",
            "market": "moneyline",
        }
        # An opening price, a closing price, and the opposing closing price
        # so the no-vig metric is computable.
        await add_snapshot(
            session,
            **common,
            selection="away",
            odds_american=130,
            captured_at=FIRST_PITCH - 3 * H,
        )
        await add_snapshot(
            session,
            **common,
            selection="away",
            odds_american=110,
            captured_at=FIRST_PITCH - 10 * dt.timedelta(minutes=1),
        )
        await add_snapshot(
            session,
            **common,
            selection="home",
            odds_american=-130,
            captured_at=FIRST_PITCH - 10 * dt.timedelta(minutes=1),
        )
        await session.commit()

        bet = await log_bet(
            session,
            **common,
            selection="away",
            odds_american=130,
            stake_cents=5000,
        )
        await session.commit()

        settled = await settle_bet(session, bet.id, BetStatus.WON)
        await session.commit()
        assert settled.payout_cents == 11500  # $50 at +130

        result = await compute_clv_for_bet(session, bet.id)
        assert result is not None
        assert result.closing_odds_american == 110
        # 2.30 / 2.10 - 1
        assert result.clv_pct == pytest.approx(9.523810, abs=1e-6)
        assert result.beat_close is True
        # Closing market +110 / -130 devigged by the power method.
        assert result.clv_prob_points is not None
        assert result.clv_prob_points > 0

    async def test_clv_is_none_before_any_snapshot_exists(self, session):
        bet = await log_bet(
            session,
            game_pk=GAME_PK,
            game_date=GAME_DATE,
            commence_time_utc=FIRST_PITCH,
            book="pinnacle",
            market="moneyline",
            selection="away",
            odds_american=130,
            stake_cents=5000,
        )
        await session.commit()
        assert await compute_clv_for_bet(session, bet.id) is None
