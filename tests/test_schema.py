"""Schema constraints and time semantics, against real Postgres.

These tests are the reason the suite requires Postgres rather than SQLite.
CHECK enforcement, TIMESTAMPTZ behaviour and NULL handling in btree indexes
are all Postgres semantics; a SQLite run would report these as passing while
proving nothing about what production actually rejects.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import select, text

from database.enums import BetStatus, IngestStatus
from database.ingest_run import ingest_run
from database.models import Bet, IngestRun, OddsSnapshot
from database.utc import require_utc, utcnow
from tests.conftest import FIRST_PITCH, GAME_DATE, make_bet, make_snapshot

pytestmark = pytest.mark.postgres

H = dt.timedelta(hours=1)


async def expect_rejected(session, obj, *, exception=sa_exc.IntegrityError):
    """Add, flush, require a database-level rejection, then recover."""
    session.add(obj)
    with pytest.raises(exception):
        await session.flush()
    await session.rollback()


class TestTimeSemantics:
    async def test_rejects_naive_datetime(self, session, run):
        # Postgres would NOT reject this on its own: timestamptz silently
        # interprets a naive value in the server's timezone and stores a
        # different instant than intended. UtcDateTime closes that hole
        # before the value reaches the driver.
        naive = dt.datetime(2026, 7, 27, 22, 55)  # no tzinfo
        snapshot = make_snapshot(run, captured_at=naive, odds=130)
        await expect_rejected(session, snapshot, exception=sa_exc.StatementError)

    async def test_naive_message_names_the_alternative(self):
        with pytest.raises(ValueError, match="naive"):
            require_utc(dt.datetime(2026, 7, 27, 22, 55))

    async def test_aware_non_utc_is_converted_not_rejected(self, session, run):
        # Converting an aware value is lossless and unambiguous, so it is
        # accepted; only guessing at a naive one is forbidden.
        eastern = dt.timezone(dt.timedelta(hours=-4))
        local = dt.datetime(2026, 7, 27, 18, 55, tzinfo=eastern)
        session.add(make_snapshot(run, captured_at=local, odds=130))
        await session.commit()

        stored = (await session.execute(select(OddsSnapshot))).scalars().one()
        assert stored.captured_at == dt.datetime(
            2026, 7, 27, 22, 55, tzinfo=dt.timezone.utc
        )

    async def test_reads_back_utc_aware(self, session, run):
        session.add(make_snapshot(run, captured_at=FIRST_PITCH - H, odds=130))
        await session.commit()

        stored = (await session.execute(select(OddsSnapshot))).scalars().one()
        assert stored.captured_at.tzinfo is not None
        assert stored.captured_at.utcoffset() == dt.timedelta(0)

    async def test_game_date_is_venue_local_not_the_utc_date(self, session, run):
        # A 10:10pm PACIFIC first pitch on 27 July is 05:10 UTC on 28 July.
        # The venue-local date and the UTC date are different days, and
        # game_date must hold the former.
        #
        # This is the v1 project's live bug encoded as a test: its odds
        # ingest matched games on commence_time.date() (UTC), so late West
        # Coast games silently never matched and their prices were dropped.
        commence = dt.datetime(2026, 7, 28, 5, 10, tzinfo=dt.timezone.utc)
        venue_local_date = dt.date(2026, 7, 27)

        session.add(
            make_snapshot(
                run,
                captured_at=commence - H,
                odds=130,
                game_date=venue_local_date,
                commence=commence,
            )
        )
        await session.commit()

        stored = (await session.execute(select(OddsSnapshot))).scalars().one()
        assert stored.game_date == venue_local_date
        assert stored.commence_time_utc.date() == dt.date(2026, 7, 28)
        # The two disagree by a day, and that is correct - not a bug to
        # "fix" by deriving one from the other.
        assert stored.game_date != stored.commence_time_utc.date()

    async def test_game_date_is_a_date_not_a_timestamp(self, session, run):
        session.add(make_snapshot(run, captured_at=FIRST_PITCH - H, odds=130))
        await session.commit()

        col_type = (
            await session.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'odds_snapshots' AND column_name = 'game_date'"
                )
            )
        ).scalar_one()
        assert col_type == "date"

    async def test_timestamps_are_timestamptz(self, session):
        rows = (
            await session.execute(
                text(
                    "SELECT table_name, column_name, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND column_name IN ('captured_at','commence_time_utc',"
                    "'placed_at','settled_at','started_at','finished_at',"
                    "'created_at','updated_at')"
                )
            )
        ).all()
        assert rows
        for table, column, data_type in rows:
            assert data_type == "timestamp with time zone", (table, column, data_type)


class TestOddsConstraints:
    @pytest.mark.parametrize("bad", [0, 50, -50, 99, -99])
    async def test_rejects_impossible_american_odds(self, session, run, bad):
        # A value in (-100, +100) is a probability or a percentage that
        # reached an odds column by mistake. Left unchecked it produces a
        # perfectly plausible implied probability and a silently wrong CLV.
        await expect_rejected(
            session, make_snapshot(run, captured_at=FIRST_PITCH - H, odds=bad)
        )

    @pytest.mark.parametrize("good", [-100, 100, -150, 130, -2000, 1200])
    async def test_accepts_valid_american_odds(self, session, run, good):
        session.add(make_snapshot(run, captured_at=FIRST_PITCH - H, odds=good))
        await session.commit()

    async def test_rejects_bet_with_impossible_odds(self, session):
        await expect_rejected(session, make_bet(odds=50))


class TestMarketConstraints:
    async def test_moneyline_may_not_carry_a_line(self, session, run):
        await expect_rejected(
            session,
            make_snapshot(
                run,
                captured_at=FIRST_PITCH - H,
                odds=130,
                market="moneyline",
                line=Decimal("8.5"),
            ),
        )

    async def test_total_requires_a_line(self, session, run):
        await expect_rejected(
            session,
            make_snapshot(
                run,
                captured_at=FIRST_PITCH - H,
                odds=-110,
                market="total",
                selection="over",
                line=None,
            ),
        )

    async def test_total_may_not_have_a_home_away_selection(self, session, run):
        await expect_rejected(
            session,
            make_snapshot(
                run,
                captured_at=FIRST_PITCH - H,
                odds=-110,
                market="total",
                selection="home",
                line=Decimal("8.5"),
            ),
        )

    async def test_moneyline_may_not_have_over_under(self, session, run):
        await expect_rejected(
            session,
            make_snapshot(
                run, captured_at=FIRST_PITCH - H, odds=130, selection="over"
            ),
        )

    async def test_unknown_market_rejected(self, session, run):
        await expect_rejected(
            session,
            make_snapshot(
                run,
                captured_at=FIRST_PITCH - H,
                odds=130,
                market="player_props",
                selection="over",
                line=Decimal("1.5"),
            ),
        )

    async def test_run_line_requires_a_line(self, session, run):
        await expect_rejected(
            session,
            make_snapshot(
                run,
                captured_at=FIRST_PITCH - H,
                odds=130,
                market="run_line",
                selection="home",
                line=None,
            ),
        )

    async def test_negative_run_line_is_allowed(self, session, run):
        # NUMERIC(4,2) must accommodate the -1.5 favourite side.
        session.add(
            make_snapshot(
                run,
                captured_at=FIRST_PITCH - H,
                odds=130,
                market="run_line",
                selection="home",
                line=Decimal("-1.5"),
            )
        )
        await session.commit()

    async def test_quarter_line_survives_the_round_trip(self, session, run):
        # NUMERIC(4,2) rather than (4,1) so books quoting 7.75 do not get
        # silently rounded to 7.8.
        session.add(
            make_snapshot(
                run,
                captured_at=FIRST_PITCH - H,
                odds=-110,
                market="total",
                selection="over",
                line=Decimal("7.75"),
            )
        )
        await session.commit()

        stored = (await session.execute(select(OddsSnapshot))).scalars().one()
        assert stored.line == Decimal("7.75")


class TestBetConstraints:
    async def test_rejects_zero_stake(self, session):
        await expect_rejected(session, make_bet(odds=130, stake_cents=0))

    async def test_rejects_negative_stake(self, session):
        await expect_rejected(session, make_bet(odds=130, stake_cents=-100))

    async def test_rejects_nonpositive_game_pk(self, session):
        await expect_rejected(session, make_bet(odds=130, game_pk=0))

    async def test_settled_bet_requires_settled_at(self, session):
        bet = make_bet(odds=130)
        bet.status = BetStatus.WON.value
        bet.payout_cents = 11500
        bet.settled_at = None
        await expect_rejected(session, bet)

    async def test_settled_bet_requires_payout(self, session):
        bet = make_bet(odds=130)
        bet.status = BetStatus.WON.value
        bet.settled_at = utcnow()
        bet.payout_cents = None
        await expect_rejected(session, bet)

    async def test_open_bet_may_not_have_a_payout(self, session):
        bet = make_bet(odds=130)
        bet.payout_cents = 11500
        await expect_rejected(session, bet)

    async def test_open_bet_may_not_have_settled_at(self, session):
        bet = make_bet(odds=130)
        bet.settled_at = utcnow()
        await expect_rejected(session, bet)

    async def test_coherent_settlement_is_accepted(self, session):
        bet = make_bet(odds=130)
        bet.status = BetStatus.WON.value
        bet.settled_at = utcnow()
        bet.payout_cents = 11500
        session.add(bet)
        await session.commit()

    async def test_unknown_status_rejected(self, session):
        bet = make_bet(odds=130)
        bet.status = "cashed_out"
        bet.settled_at = utcnow()
        bet.payout_cents = 5000
        await expect_rejected(session, bet)

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("1"), Decimal("1.5")])
    async def test_model_prob_must_be_a_probability(self, session, bad):
        bet = make_bet(odds=130)
        bet.model_prob = bad
        await expect_rejected(session, bet)

    async def test_model_prob_is_nullable(self, session):
        # Nothing produces a model probability in Phase 0.
        session.add(make_bet(odds=130))
        await session.commit()
        stored = (await session.execute(select(Bet))).scalars().one()
        assert stored.model_prob is None


class TestAppendOnly:
    async def test_identical_prices_at_different_times_both_persist(
        self, session, run
    ):
        # Asserts the ABSENCE of a unique constraint on the natural key.
        # An unchanged re-poll is a real observation - "we checked at T and
        # it had not moved" - and collapsing the two would destroy it.
        for offset in (3, 2, 1):
            session.add(
                make_snapshot(run, captured_at=FIRST_PITCH - offset * H, odds=130)
            )
        await session.commit()

        stored = (await session.execute(select(OddsSnapshot))).scalars().all()
        assert len(stored) == 3
        assert {s.odds_american for s in stored} == {130}

    async def test_the_same_observation_cannot_be_stored_twice(self, session, run):
        # The same natural key at the same instant is ONE observation, not
        # two. Writers use ON CONFLICT DO NOTHING so a retry is a no-op;
        # writing it directly, as here, raises.
        at = FIRST_PITCH - H
        session.add(make_snapshot(run, captured_at=at, odds=130))
        await session.commit()

        await expect_rejected(session, make_snapshot(run, captured_at=at, odds=130))

    async def test_unique_key_treats_nulls_as_not_distinct(self, session):
        """The detail the whole constraint hinges on.

        Postgres treats NULLs as DISTINCT in a unique index by default, so a
        plain unique constraint over a nullable `line` would never fire for
        moneyline - the most common market, where line IS NULL - and the
        constraint would silently protect only totals and run lines.
        """
        definition = (
            await session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'uq_odds_snapshots_observation'"
                )
            )
        ).scalar_one()
        assert "NULLS NOT DISTINCT" in definition, definition
        assert (
            "(game_pk, market, selection, book, line, captured_at)" in definition
        ), definition

    async def test_moneyline_duplicate_is_actually_caught(self, session, run):
        # The behavioural half of the test above: moneyline rows carry
        # line IS NULL, and must still collide.
        at = FIRST_PITCH - H
        session.add(make_snapshot(run, captured_at=at, odds=130, market="moneyline"))
        await session.commit()

        await expect_rejected(
            session, make_snapshot(run, captured_at=at, odds=130, market="moneyline")
        )

    async def test_a_different_price_at_the_same_instant_still_collides(
        self, session, run
    ):
        # Two different prices stamped at the same instant for the same
        # selection is not two observations - it is one observation recorded
        # inconsistently. Rejecting it surfaces the bug rather than leaving
        # an ambiguous pair for the tie-break to guess between.
        at = FIRST_PITCH - H
        session.add(make_snapshot(run, captured_at=at, odds=130))
        await session.commit()

        await expect_rejected(session, make_snapshot(run, captured_at=at, odds=115))


class TestProvenance:
    async def test_snapshot_requires_an_existing_ingest_run(self, session, run):
        orphan = make_snapshot(run, captured_at=FIRST_PITCH - H, odds=130)
        orphan.ingest_run_id = 999999
        await expect_rejected(session, orphan)

    async def test_ingest_run_id_is_not_nullable(self, session, run):
        orphan = make_snapshot(run, captured_at=FIRST_PITCH - H, odds=130)
        orphan.ingest_run_id = None
        await expect_rejected(session, orphan)


class TestIngestRunConstraints:
    async def test_running_may_not_have_finished_at(self, session):
        await expect_rejected(
            session,
            IngestRun(
                source="test",
                run_kind="manual",
                status=IngestStatus.RUNNING.value,
                finished_at=utcnow(),
            ),
        )

    async def test_finished_must_have_finished_at(self, session):
        await expect_rejected(
            session,
            IngestRun(
                source="test",
                run_kind="manual",
                status=IngestStatus.SUCCESS.value,
                finished_at=None,
            ),
        )

    async def test_failed_requires_an_error(self, session):
        await expect_rejected(
            session,
            IngestRun(
                source="test",
                run_kind="manual",
                status=IngestStatus.FAILED.value,
                finished_at=utcnow(),
                error=None,
            ),
        )

    async def test_negative_counts_rejected(self, session):
        await expect_rejected(
            session,
            IngestRun(
                source="test",
                run_kind="manual",
                status=IngestStatus.RUNNING.value,
                rows_written=-1,
            ),
        )


class TestIngestRunContext:
    async def test_marks_success_and_records_rows(self, session):
        async with ingest_run(session, source="manual-cli", run_kind="manual") as r:
            session.add(
                make_snapshot(r, captured_at=FIRST_PITCH - H, odds=130)
            )
            r.rows_written = 1
        await session.commit()

        stored = (await session.execute(select(IngestRun))).scalars().one()
        assert stored.status == IngestStatus.SUCCESS.value
        assert stored.finished_at is not None
        assert stored.rows_written == 1

    async def test_records_the_failure_instead_of_swallowing_it(self, session):
        # The crash that most needs recording is the one that would
        # otherwise leave no trace: the transaction is already poisoned, so
        # the failure row has to be written on a fresh one.
        class Boom(RuntimeError):
            pass

        with pytest.raises(Boom):
            async with ingest_run(session, source="manual-cli", run_kind="manual"):
                raise Boom("upstream returned 503")

        await session.rollback()
        stored = (await session.execute(select(IngestRun))).scalars().all()
        assert len(stored) == 1
        assert stored[0].status == IngestStatus.FAILED.value
        assert stored[0].finished_at is not None
        assert "upstream returned 503" in stored[0].error

    async def test_failed_run_writes_no_snapshots(self, session):
        class Boom(RuntimeError):
            pass

        with pytest.raises(Boom):
            async with ingest_run(session, source="manual-cli", run_kind="manual") as r:
                session.add(make_snapshot(r, captured_at=FIRST_PITCH - H, odds=130))
                raise Boom("failed halfway")

        await session.rollback()
        # The rows written before the failure rolled back with it - a
        # partial poll does not leave half a slate behind.
        assert (await session.execute(select(OddsSnapshot))).scalars().all() == []


class TestIndexesExist:
    async def test_expected_indexes_are_present(self, session):
        names = set(
            (
                await session.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = 'public'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {
            "ix_odds_snapshots_pit_no_line",
            "ix_odds_snapshots_pit_lined",
            "ix_odds_snapshots_game_captured",
            "ix_odds_snapshots_ingest_run_id",
            "ix_bets_game_pk",
            "ix_bets_game_date",
            "ix_bets_open",
            "ix_ingest_runs_source_started",
            "ix_ingest_runs_running",
        } <= names

    async def test_point_in_time_index_column_order(self, session):
        # The column ORDER is what makes the ordered seek possible: equality
        # predicates first, captured_at then id last. The split into two
        # partial indexes on line-presence is deliberate - see
        # tests/test_clv_queries.py::TestIndexUsage. A well-meaning
        # consolidation keeps every functional test passing and quietly costs
        # a sort on every unlined lookup.
        rows = dict(
            (
                await session.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes WHERE "
                        "indexname IN ('ix_odds_snapshots_pit_no_line',"
                        "'ix_odds_snapshots_pit_lined')"
                    )
                )
            ).all()
        )
        assert (
            "(game_pk, market, selection, book, captured_at, id)"
            in rows["ix_odds_snapshots_pit_no_line"]
        )
        assert (
            "(game_pk, market, selection, book, line, captured_at, id)"
            in rows["ix_odds_snapshots_pit_lined"]
        )

    async def test_partial_indexes_are_actually_partial(self, session):
        rows = dict(
            (
                await session.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE indexname IN ('ix_bets_open','ix_ingest_runs_running')"
                    )
                )
            ).all()
        )
        assert "WHERE (status = 'open'" in rows["ix_bets_open"]
        assert "WHERE (status = 'running'" in rows["ix_ingest_runs_running"]


class TestNoGamesTable:
    async def test_game_pk_carries_no_foreign_key(self, session):
        # Deliberate for Phase 0: there is no games table to point at yet.
        # This test documents the absence so that adding one later is a
        # conscious change rather than a surprise.
        rows = (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE c.contype = 'f' AND t.relname IN ('bets','odds_snapshots')"
                )
            )
        ).scalars().all()
        assert rows == ["fk_odds_snapshots_ingest_run"]

    async def test_only_three_tables_exist(self, session):
        names = set(
            (
                await session.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert names == {"bets", "odds_snapshots", "ingest_runs", "alembic_version"}
