"""The three Phase 0 tables: ingest_runs, odds_snapshots, bets.

Conventions enforced here, all from claude.md:

- Games are keyed by `game_pk` (int) alone. Never (date, home, away):
  doubleheaders share a date and both teams, so that key collapses two
  distinct games into one.
- Odds are American integers at rest. Decimal odds and implied
  probabilities are derived inside `betting/`, never stored.
- Every timestamp is timezone-aware UTC, via `UtcDateTime`. `game_date` is a
  separate DATE column holding the venue-LOCAL date. These are not
  interchangeable - see database/utc.py.
- `odds_snapshots` is append-only.

There is deliberately no `games` table yet, so `game_pk` carries no foreign
key. It is a bare indexed integer until a schedule ingest exists to own it.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base
from database.enums import BetStatus, IngestStatus
from database.utc import UtcDateTime, utcnow

__all__ = ["IngestRun", "OddsSnapshot", "Bet"]

# Reused across both tables that store a price. American odds are never 0 and
# never fall strictly between -100 and +100; a value in that gap is a
# probability, a percentage, or a stake that reached an odds column by
# mistake. Catching it here turns a silently plausible implied probability
# into a loud IntegrityError.
_VALID_AMERICAN_ODDS = "odds_american <= -100 OR odds_american >= 100"

# A moneyline price has no line; a total or run line is meaningless without
# one. Keeping these consistent is what lets CLV resolve "over 8.5" against
# 8.5 snapshots only, rather than mixing in a market that moved to 9.0.
_LINE_PRESENCE = (
    "(market = 'moneyline' AND line IS NULL) "
    "OR (market <> 'moneyline' AND line IS NOT NULL)"
)

_SELECTION_MATCHES_MARKET = (
    "(market = 'total' AND selection IN ('over','under')) "
    "OR (market <> 'total' AND selection IN ('home','away'))"
)

_MARKET_VALUES = "market IN ('moneyline','total','run_line')"
_SELECTION_VALUES = "selection IN ('home','away','over','under')"


class IngestRun(Base):
    """One execution of something that writes data - an odds poll, a schedule
    sync, or a manual CLI command.

    Unlike `odds_snapshots`, this table IS mutable: a run is created in
    `running` state and updated to `success` or `failed` when it finishes.
    That is the point - a row stuck in `running` is how a crashed worker
    announces itself instead of vanishing silently.
    """

    __tablename__ = "ingest_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','success','partial','failed')", name="status"
        ),
        # A run is finished exactly when it has a finish time. Without this,
        # "is it done?" has two possible answers that can disagree.
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="finished_iff_done"
        ),
        CheckConstraint(
            "rows_written >= 0 AND api_requests >= 0", name="counts_nonneg"
        ),
        CheckConstraint(
            "status <> 'failed' OR error IS NOT NULL", name="failed_has_error"
        ),
        # "When did odds last poll successfully?" - the health question a
        # scheduler asks constantly - in one index seek.
        Index("ix_ingest_runs_source_started", "source", "started_at"),
        # Tiny (normally 0-2 rows) and finds stuck runs. Without it, detecting
        # a crashed worker means scanning all history.
        Index(
            "ix_ingest_runs_running",
            "started_at",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(Text)  # 'the-odds-api' | 'manual-cli' | ...
    run_kind: Mapped[str] = mapped_column(Text)  # 'odds_poll' | 'schedule' | 'manual'
    status: Mapped[str] = mapped_column(
        Text, default=IngestStatus.RUNNING.value, server_default=text("'running'")
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utcnow, server_default=func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    rows_written: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    # Counts calls against a metered external API, so a monthly budget can be
    # reconstructed from history rather than tracked in a separate counter.
    api_requests: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    params: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    snapshots: Mapped[list["OddsSnapshot"]] = relationship(back_populates="ingest_run")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"IngestRun(id={self.id!r}, source={self.source!r}, "
            f"run_kind={self.run_kind!r}, status={self.status!r})"
        )


class OddsSnapshot(Base):
    """One observed price, for one selection, at one book, at one instant.

    APPEND-ONLY. No UPDATE, no UPSERT, ever. Two identical prices captured at
    different times are two legitimate observations, not a conflict - "we
    checked at T and it had not moved" is real information, and there is
    deliberately no unique constraint on the natural key that would collapse
    them.

    Long/narrow rather than one wide row per game. A wide row cannot express
    per-book prices, cannot represent a market with more than two sides, and
    cannot answer "the latest price for THIS selection before T" - which is
    the query the whole CLV loop is built on.
    """

    __tablename__ = "odds_snapshots"
    __table_args__ = (
        CheckConstraint(_VALID_AMERICAN_ODDS, name="odds_american_valid"),
        CheckConstraint(_MARKET_VALUES, name="market"),
        CheckConstraint(_SELECTION_VALUES, name="selection"),
        CheckConstraint(_LINE_PRESENCE, name="line_presence"),
        CheckConstraint(_SELECTION_MATCHES_MARKET, name="selection_matches_market"),
        CheckConstraint("game_pk > 0", name="game_pk_positive"),
        # THE point-in-time lookup, split into two PARTIAL indexes on whether
        # the market carries a line. Both answer "captured_at < T ORDER BY
        # captured_at DESC, id DESC LIMIT 1" with an ordered backward walk
        # that stops at the first row.
        #
        # Why two, rather than one index containing `line`:
        #
        # A btree yields ordered output on a trailing column only when every
        # preceding column is bound by EQUALITY. `line IS NULL` is a NullTest,
        # not an equality operator, so it never forms an equivalence class
        # with a constant and the planner cannot drop the `line` pathkey. The
        # rows ARE physically ordered by captured_at within the NULL-line
        # range; the planner just cannot prove it, so it sorts. Measured on
        # 400k rows: a single index with `line` in it turned a 0.04ms ordered
        # scan into a bitmap scan plus top-N sort at ~13x the estimated cost
        # (289.99 vs 8.47).
        #
        # Simply dropping `line` fixes the ordering but makes a lined lookup
        # scan every row for that game/market/selection/book across ALL lines
        # and filter. Four totals lines is nothing; a strikeout prop with 15
        # alternate lines polled every 15 minutes for six hours is 360 rows
        # scanned to return one.
        #
        # Partial indexes get both properties. In the unlined index the
        # predicate absorbs `line IS NULL`, so `line` is not a column at all
        # and every leading column is an equality. In the lined index
        # `line = $5` is a genuine equality, so ordering holds AND the scan
        # narrows to the one line instead of filtering siblings.
        #
        # This only works if the query emits `line IS NULL` or `line = $5`
        # to match a predicate - see betting/clv.py::find_closing_snapshot.
        # `IS NOT DISTINCT FROM` matches neither and is not indexable at all.
        #
        # `id` trails captured_at because the query breaks ties on it, so two
        # prices stamped at the identical instant resolve the same way on
        # every call. Without it the ORDER BY is only partly satisfied and
        # Postgres bolts on an Incremental Sort.
        #
        # Declared ASC deliberately: Postgres scans a btree backward at the
        # same cost as forward, so a DESC declaration would buy nothing while
        # making the index one Alembic compares poorly.
        Index(
            "ix_odds_snapshots_pit_no_line",
            "game_pk",
            "market",
            "selection",
            "book",
            "captured_at",
            "id",
            postgresql_where=text("line IS NULL"),
        ),
        Index(
            "ix_odds_snapshots_pit_lined",
            "game_pk",
            "market",
            "selection",
            "book",
            "line",
            "captured_at",
            "id",
            postgresql_where=text("line IS NOT NULL"),
        ),
        # Whole-game time-ordered scans (line movement across every market).
        # The index above only orders by time WITHIN a fixed
        # market/selection/book, so it cannot serve this.
        Index("ix_odds_snapshots_game_captured", "game_pk", "captured_at"),
        # Postgres does not auto-index foreign key columns, and "what did run
        # 47 write?" is the first question asked when an ingest looks wrong.
        Index("ix_odds_snapshots_ingest_run_id", "ingest_run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ingest_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingest_runs.id", name="fk_odds_snapshots_ingest_run")
    )

    game_pk: Mapped[int] = mapped_column(Integer)
    # The venue-LOCAL calendar date, from MLB StatsAPI's `officialDate`.
    # NEVER derive this from commence_time_utc.date(): a 10:10pm Pacific game
    # is 05:10 UTC the next day, so the UTC date is a different day. NOT NULL
    # forces the future odds worker to resolve the schedule before writing
    # prices, which is the correct dependency order anyway.
    game_date: Mapped[dt.date] = mapped_column(Date)
    # First pitch as reported by the source AT CAPTURE TIME. Denormalised
    # because there is no games table yet. Append-only makes that honest: a
    # postponement shows up as a new value on later rows, which is accurate
    # history rather than a lost update.
    commence_time_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)

    book: Mapped[str] = mapped_column(Text)
    market: Mapped[str] = mapped_column(Text)
    selection: Mapped[str] = mapped_column(Text)
    line: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    odds_american: Mapped[int] = mapped_column(Integer)

    captured_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow)

    ingest_run: Mapped[IngestRun] = relationship(back_populates="snapshots")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"OddsSnapshot(game_pk={self.game_pk!r}, book={self.book!r}, "
            f"market={self.market!r}, selection={self.selection!r}, "
            f"line={self.line!r}, odds={self.odds_american!r}, "
            f"captured_at={self.captured_at!r})"
        )


class Bet(Base):
    """A single-leg wager that was actually placed.

    Money is integer cents throughout. Floats do not represent 0.10 exactly
    and a bankroll accumulated from them drifts; `Decimal` avoids that but
    invites accidental float contamination at every boundary. Integer cents
    cannot be wrong.
    """

    __tablename__ = "bets"
    __table_args__ = (
        CheckConstraint(_VALID_AMERICAN_ODDS, name="odds_american_valid"),
        CheckConstraint(_MARKET_VALUES, name="market"),
        CheckConstraint(_SELECTION_VALUES, name="selection"),
        CheckConstraint(_LINE_PRESENCE, name="line_presence"),
        CheckConstraint(_SELECTION_MATCHES_MARKET, name="selection_matches_market"),
        CheckConstraint("game_pk > 0", name="game_pk_positive"),
        CheckConstraint("stake_cents > 0", name="stake_positive"),
        CheckConstraint(
            "payout_cents IS NULL OR payout_cents >= 0", name="payout_nonneg"
        ),
        CheckConstraint(
            "status IN ('open','won','lost','push','void')", name="status"
        ),
        # Makes "settled" mean one thing instead of three. Without it a row
        # can claim status='won' while carrying no settlement time and no
        # payout, and every consumer has to decide which field to trust.
        CheckConstraint(
            "(status = 'open' AND settled_at IS NULL AND payout_cents IS NULL) "
            "OR (status <> 'open' AND settled_at IS NOT NULL "
            "AND payout_cents IS NOT NULL)",
            name="settlement_coherent",
        ),
        CheckConstraint(
            "model_prob IS NULL OR (model_prob > 0 AND model_prob < 1)",
            name="model_prob_range",
        ),
        # Join key to odds_snapshots for CLV.
        Index("ix_bets_game_pk", "game_pk"),
        # Day-level P&L rollups.
        Index("ix_bets_game_date", "game_date"),
        # The settle workflow's query ("what is outstanding?"). Partial, so it
        # stays small forever: open bets are a bounded working set while
        # settled bets grow without bound.
        Index(
            "ix_bets_open",
            "placed_at",
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    game_pk: Mapped[int] = mapped_column(Integer)
    game_date: Mapped[dt.date] = mapped_column(Date)  # venue-local
    # The CLV cutoff: the closing line is the last price observed strictly
    # before this instant.
    commence_time_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)

    book: Mapped[str] = mapped_column(Text)
    market: Mapped[str] = mapped_column(Text)
    selection: Mapped[str] = mapped_column(Text)
    line: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    odds_american: Mapped[int] = mapped_column(Integer)
    stake_cents: Mapped[int] = mapped_column(BigInteger)

    placed_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(
        Text, default=BetStatus.OPEN.value, server_default=text("'open'")
    )
    settled_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Total returned including the stake, so a won bet at -150 on $50 is
    # 8333, not 3333. A push returns the stake; a loss returns 0.
    payout_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Nothing populates this in Phase 0 - there is no model yet. The column
    # exists so that when one arrives, the bets already logged are not
    # retroactively unanalysable.
    model_prob: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Bet(id={self.id!r}, game_pk={self.game_pk!r}, book={self.book!r}, "
            f"market={self.market!r}, selection={self.selection!r}, "
            f"odds={self.odds_american!r}, stake_cents={self.stake_cents!r}, "
            f"status={self.status!r})"
        )
