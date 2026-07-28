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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base
from database.enums import BetStatus, IngestStatus
from database.utc import UtcDateTime, utcnow

__all__ = ["Bet", "Game", "IngestRun", "OddsSnapshot"]

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
        # The mirror of the above, and it is not symmetry for its own sake.
        # A long run can be reaped by another process while it is still
        # working; when it then completes, it writes status='success' over
        # the reaped row. Without this, the error text from the reap survives
        # on a successful run and there is nothing to catch it - the run
        # reads as healthy while carrying a message saying it died.
        CheckConstraint(
            "status <> 'success' OR error IS NULL", name="success_has_no_error"
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

    # The Postgres backend pid that opened this run, from pg_backend_pid().
    # Lets the reaper ask whether the process is actually gone instead of
    # inferring it from a wall-clock timeout - see database/ingest_run.py.
    # Nullable because a writer that does not hold a connection for the
    # run's duration cannot supply a meaningful one, and because rows
    # predating this column have none.
    backend_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)

    snapshots: Mapped[list[OddsSnapshot]] = relationship(back_populates="ingest_run")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"IngestRun(id={self.id!r}, source={self.source!r}, "
            f"run_kind={self.run_kind!r}, status={self.status!r})"
        )


class Game(Base):
    """One MLB game, keyed by StatsAPI's gamePk.

    `game_pk` is the natural key and the primary key - no surrogate. It is
    assigned by MLB, stable, and the thing every other table already refers
    to.

    THE PK IS STABLE ACROSS A POSTPONEMENT. StatsAPI does not mint a new
    gamePk when a game is moved: pk 778431 was postponed on 2025-04-06 and
    played on 2025-08-09 under the same pk, with game_date and
    commence_time_utc updated in place. That is why there is no
    `rescheduled_as` column - there is no successor row to point at - and
    why bets and snapshots keep pointing at the right game for free.

    `rescheduled_from_date` records where it moved FROM, which is the part
    that would otherwise be lost. A twice-postponed game keeps only the most
    recent origin; see docs/known-gaps.md.
    """

    __tablename__ = "games"
    __table_args__ = (
        CheckConstraint("game_pk > 0", name="game_pk_positive"),
        # Slate queries: "what is on tonight".
        Index("ix_games_game_date", "game_date"),
        Index("ix_games_commence_time_utc", "commence_time_utc"),
    )

    # No autoincrement: this value comes from MLB, it is not ours to invent.
    game_pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    # Venue-LOCAL date, from StatsAPI officialDate. NEVER derived from
    # commence_time_utc - see database/utc.py and ingestion/statsapi.py.
    game_date: Mapped[dt.date] = mapped_column(Date)
    commence_time_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)

    home_team_id: Mapped[int] = mapped_column(Integer)
    away_team_id: Mapped[int] = mapped_column(Integer)
    # Resolves to an IANA timezone via ingestion.reference.VENUE_TIMEZONES.
    venue_id: Mapped[int] = mapped_column(Integer)

    # StatsAPI detailedState verbatim: Scheduled, Pre-Game, In Progress,
    # Final, Postponed, Completed Early, Suspended, ... Deliberately
    # unconstrained - enumerating a vocabulary owned by someone else, from an
    # incomplete sample, buys a CHECK that rejects real data.
    status: Mapped[str] = mapped_column(Text)

    rescheduled_from_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    updated_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Game(game_pk={self.game_pk!r}, game_date={self.game_date!r}, "
            f"status={self.status!r})"
        )


class OddsSnapshot(Base):
    """One observed price, for one selection, at one book, at one instant.

    APPEND-ONLY. No UPDATE, ever. Two identical prices captured at DIFFERENT
    times are two legitimate observations, not a conflict - "we checked at T
    and it had not moved" is real information.

    But the same observation written twice is not two observations. The
    natural key is unique, and writers use ON CONFLICT DO NOTHING, which
    inserts or does not: it never rewrites an existing row, so append-only
    holds. This makes a retried ingest run idempotent instead of pushing the
    cost of de-duplication onto every read forever.

    That only works because `captured_at` is stamped from the ingest run's
    start time rather than from now() - see the column comment below.

    Long/narrow rather than one wide row per game. A wide row cannot express
    per-book prices, cannot represent a market with more than two sides, and
    cannot answer "the latest price for THIS selection before T" - which is
    the query the whole CLV loop is built on.
    """

    __tablename__ = "odds_snapshots"
    __table_args__ = (
        # One row per distinct observation. Retried ingest runs collide here
        # and are dropped by ON CONFLICT DO NOTHING rather than duplicated.
        #
        # NULLS NOT DISTINCT is essential, not decoration. Postgres treats
        # NULLs as distinct in a unique index by default, so without it every
        # moneyline row - where `line IS NULL`, the most common market -
        # would collide with nothing and duplicate freely, leaving the
        # constraint working only for totals and run lines. Requires
        # Postgres 15+; we target 16.
        UniqueConstraint(
            "game_pk",
            "market",
            "selection",
            "book",
            "line",
            "captured_at",
            name="uq_odds_snapshots_observation",
            postgresql_nulls_not_distinct=True,
        ),
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
        # `id` IS NOT DEAD WEIGHT - do not remove it from either index.
        #
        # The query is ORDER BY captured_at DESC, id DESC. Drop `id` and
        # Postgres adds an Incremental Sort: it will NOT infer from the
        # observation-uniqueness constraint that captured_at is already
        # unique within a key, so it cannot prove the second sort key is a
        # no-op. That breaks the no-Sort assertion in TestIndexUsage.
        #
        # Dropping it from the index therefore means dropping it from the
        # ORDER BY too - at which point determinism depends on that
        # constraint continuing to hold, which is a worse trade. 16 bytes a
        # row across two indexes is ~160MB at 10M rows.
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

    game_pk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("games.game_pk", ondelete="RESTRICT", name="fk_odds_snapshots_game"),
    )
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

    # When the price was observed. Stamped from the owning ingest run's
    # started_at, NOT from now() at insert time - see betting/cli.py.
    #
    # This is what makes the unique constraint above mean anything. If a
    # retried poll wrote a fresh now(), the retry would land microseconds
    # from the original, collide with nothing, and be stored as a second
    # "observation" of a single real observation. Sourcing it from the run
    # makes retries genuinely idempotent, and makes "every price from one
    # poll shares a timestamp" true - which is also what lets the opposing
    # side of a market be found at the same instant.
    #
    # Deliberately no Python-side default: a writer that has not decided
    # what instant it is recording should fail, not silently record the
    # instant the INSERT happened to run.
    captured_at: Mapped[dt.datetime] = mapped_column(UtcDateTime)

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
            "status IN ('open','won','lost','push','void','postponed')",
            name="status",
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

    game_pk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("games.game_pk", ondelete="RESTRICT", name="fk_bets_game"),
    )
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
