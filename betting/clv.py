"""Closing Line Value.

CLV compares the price you got to the price the market settled on at first
pitch. It is the standard proxy for "does this actually have edge",
independent of whether any individual bet won, because results converge
slowly and CLV converges fast: a hundred bets tell you almost nothing about
win rate but a great deal about whether you are consistently beating the
close.

TWO METRICS, DELIBERATELY
-------------------------
`clv_pct` - price-based: (decimal_bet / decimal_close - 1) * 100. Needs only
your own side's closing price. This is the number most people mean by CLV.
It conflates genuine line movement with changes in the book's margin, but it
is always computable.

`clv_prob_points` - the no-vig comparison: the devigged closing probability
of your selection, minus the break-even probability of the price you took,
in probability points. This is the theoretically cleaner measure - it asks
"at the price I took, was my bet +EV against the market's final honest
estimate?" - but it requires the OPPOSING closing price too, because a
single price cannot be devigged. It is None when that is missing rather
than being guessed at.

HOW THE TWO RELATE
------------------
They are not interchangeable and they do NOT always agree in sign. For a
normal (overround) closing market the implication runs one way only:

    clv_prob_points > 0  =>  clv_pct > 0        (when overround > 0)
    clv_pct > 0          =>  clv_prob_points > 0 (never guaranteed)

Proof of the first: devigging an overround book lowers every probability, so
the fair closing probability sits below the raw one. If your break-even is
below the fair probability it is necessarily below the raw one too, which is
exactly the condition for a positive price-based CLV.

The gap between them is the book's margin on your side. So a bet can beat
the closing price and still be -EV against closing fair value - taking +200
on a market that closes +180/-220 is +7.14% price CLV but -0.22 probability
points, because a 20-cent improvement does not cover a 4.5% overround. That
is a real distinction, not a data error: the first metric says you got a
better number than the market's final posted price, the second says the
market still thinks the bet is bad.

THE OVERROUND CONDITION IS NOT DECORATION. The proof depends on k > 1.
An UNDERROUND closing market - the two sides summing to less than 1, i.e. a
genuine arbitrage at the close - solves to k < 1, and then devigging RAISES
every probability rather than lowering it. In that regime fair sits above
raw and the implication reverses. It is rare, and it usually means the two
closing prices came from different books or different instants, but it is
representable and `devig_power` handles it deliberately (see its underround
bracketing). Any code or test asserting the implication above must guard on
`overround > 0` first.

`clv_prob_points` is the stricter test, and the one to trust when they
disagree.

WHAT "CLOSING" MEANS HERE
-------------------------
The last snapshot captured strictly before `commence_time_utc`, for the same
game_pk, market, selection, book and line as the bet. Every part of that
matters:

- strictly before first pitch, so a live in-game price can never be mistaken
  for a closing line;
- same book, because a soft book's close and a sharp book's close are
  different numbers (see the module note below);
- same line, because "over 8.5" and "over 9.0" are different markets, not
  the same market at a different price;
- same game_pk, because doubleheaders share a date and both teams.

Phase 0 resolves the close at the SAME BOOK you bet. A sharp book's closing
line (Pinnacle, or a consensus) is the better long-run standard, since a
soft book's close is not an efficient price - but that needs multi-book
ingest, which does not exist yet. The schema supports the switch without a
migration.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from betting.devig import devig_american
from betting.odds import american_to_decimal, american_to_implied_prob, validate_american
from database.enums import OPPOSITE_SELECTION, BetStatus, Market, Selection
from database.models import Bet, OddsSnapshot

__all__ = [
    "ClvResult",
    "compute_clv",
    "opposite_selection",
    "find_closing_snapshot",
    "find_opposing_closing_snapshot",
    "compute_clv_for_bet",
]


@dataclass(frozen=True, slots=True)
class ClvResult:
    bet_id: int | None
    bet_odds_american: int
    closing_odds_american: int
    closing_captured_at: dt.datetime
    clv_pct: float
    clv_prob_points: float | None
    fair_closing_prob: float | None
    bet_breakeven_prob: float
    beat_close: bool
    # Overround of the closing market, or None when only one side was
    # captured. Exposed because the relationship between the two CLV metrics
    # only holds when this is positive - see the module docstring. A negative
    # value means the close was an arbitrage, which in practice usually means
    # the two prices came from different books or different instants.
    closing_overround: float | None
    # Which book's close this was measured against. Normally the book the bet
    # was placed at; see `reference_book` on compute_clv_for_bet.
    closing_book: str | None = None


def opposite_selection(market: str, selection: str) -> str:
    """The other side of a two-way market."""
    market = Market(market)
    selection = Selection(selection)
    opposite = OPPOSITE_SELECTION[selection]
    # Guard against home/away being asked for on a totals market, which would
    # silently look up a selection that market never quotes.
    if market is Market.TOTAL and selection not in (Selection.OVER, Selection.UNDER):
        raise ValueError(f"{selection} is not a selection in a {market} market")
    if market is not Market.TOTAL and selection not in (Selection.HOME, Selection.AWAY):
        raise ValueError(f"{selection} is not a selection in a {market} market")
    return opposite.value


def compute_clv(
    *,
    bet_odds_american: int,
    closing_odds_american: int,
    closing_captured_at: dt.datetime,
    opposing_closing_odds_american: int | None = None,
    bet_id: int | None = None,
    closing_book: str | None = None,
) -> ClvResult:
    """CLV arithmetic. PURE - no session, no I/O, no configuration.

    Keeping this separate from the queries means every property of the
    calculation is testable without a database, and that the same function
    serves a backtest and a live request identically.
    """
    validate_american(bet_odds_american)
    validate_american(closing_odds_american)

    decimal_bet = american_to_decimal(bet_odds_american)
    decimal_close = american_to_decimal(closing_odds_american)

    # Positive when the price you took returns more than the closing price
    # would have - i.e. the market moved toward your side after you bet.
    clv_pct = (decimal_bet / decimal_close - 1.0) * 100.0

    bet_breakeven_prob = american_to_implied_prob(bet_odds_american)

    fair_closing_prob: float | None = None
    clv_prob_points: float | None = None
    closing_overround: float | None = None
    if opposing_closing_odds_american is not None:
        validate_american(opposing_closing_odds_american)
        # Power method, never multiplicative - see betting/devig.py. Using
        # p/sum(p) here would systematically overstate CLV on favorites.
        devigged = devig_american(
            [closing_odds_american, opposing_closing_odds_american]
        )
        fair_closing_prob = devigged.fair_probs[0]
        closing_overround = devigged.overround
        clv_prob_points = (fair_closing_prob - bet_breakeven_prob) * 100.0

    return ClvResult(
        bet_id=bet_id,
        bet_odds_american=bet_odds_american,
        closing_odds_american=closing_odds_american,
        closing_captured_at=closing_captured_at,
        clv_pct=clv_pct,
        clv_prob_points=clv_prob_points,
        fair_closing_prob=fair_closing_prob,
        bet_breakeven_prob=bet_breakeven_prob,
        # Strictly greater: getting exactly the closing price is not beating
        # it.
        beat_close=clv_pct > 0.0,
        closing_overround=closing_overround,
        closing_book=closing_book,
    )


async def find_closing_snapshot(
    session: AsyncSession,
    *,
    game_pk: int,
    market: str,
    selection: str,
    book: str,
    line: Decimal | None,
    before: dt.datetime,
) -> OddsSnapshot | None:
    """The latest price observed strictly before `before`, or None.

    This is the query `ix_odds_snapshots_pit` exists for.

    NOTE ON `line`: the predicate is `IS NULL` or `= value`, chosen by the
    caller's market, NOT `IS NOT DISTINCT FROM`. Postgres cannot use a btree
    index for IS NOT DISTINCT FROM, so the null-safe-equality form - which
    looks like the obvious way to write this - silently degrades the seek
    into a full scan of a table that only grows. Since the market always
    determines whether a line exists, splitting the predicate costs nothing.
    """
    stmt = (
        select(OddsSnapshot)
        .where(
            OddsSnapshot.game_pk == game_pk,
            OddsSnapshot.market == str(market),
            OddsSnapshot.selection == str(selection),
            OddsSnapshot.book == book,
            OddsSnapshot.line.is_(None) if line is None else OddsSnapshot.line == line,
            OddsSnapshot.captured_at < before,
        )
        .order_by(OddsSnapshot.captured_at.desc(), OddsSnapshot.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def find_opposing_closing_snapshot(
    session: AsyncSession,
    *,
    closing: OddsSnapshot,
    before: dt.datetime,
) -> OddsSnapshot | None:
    """The closing price on the other side of the same market.

    Uses the same cutoff as the original closing lookup rather than the
    closing row's own timestamp: a poll writes each side as its own row, and
    those rows can carry timestamps a few milliseconds apart. Anchoring on
    the first side's exact instant would drop the other side whenever it
    happened to be written second.
    """
    return await find_closing_snapshot(
        session,
        game_pk=closing.game_pk,
        market=closing.market,
        selection=opposite_selection(closing.market, closing.selection),
        book=closing.book,
        line=closing.line,
        before=before,
    )


async def compute_clv_for_bet(
    session: AsyncSession,
    bet_id: int,
    *,
    reference_book: str | None = None,
) -> ClvResult | None:
    """CLV for a logged bet, or None if no closing price was ever captured.

    Returns None rather than raising for a missing close: early on there
    will be plenty of bets with no snapshot behind them, and that is a gap
    in the data, not an error.

    CLV is computed as soon as a closing price exists. It is *reported* per
    settled bet, but it is *knowable* the moment the game starts, and
    waiting for settlement would delay the only signal that converges fast.

    `reference_book` chooses whose close to measure against, defaulting to
    the book the bet was placed at. That default is right for now but is not
    the long-run standard: a soft book's close is not an efficient price, so
    a sharp reference (Pinnacle, or a consensus) is the better yardstick
    once multi-book ingest exists. Keeping it a parameter means that switch
    is a changed default, not a refactor.
    """
    bet = await session.get(Bet, bet_id)
    if bet is None:
        raise LookupError(f"no bet with id {bet_id}")

    book = reference_book or bet.book
    closing = await find_closing_snapshot(
        session,
        game_pk=bet.game_pk,
        market=bet.market,
        selection=bet.selection,
        book=book,
        line=bet.line,
        before=bet.commence_time_utc,
    )
    if closing is None:
        return None

    opposing = await find_opposing_closing_snapshot(
        session, closing=closing, before=bet.commence_time_utc
    )

    return compute_clv(
        bet_odds_american=bet.odds_american,
        closing_odds_american=closing.odds_american,
        closing_captured_at=closing.captured_at,
        opposing_closing_odds_american=(
            opposing.odds_american if opposing is not None else None
        ),
        bet_id=bet.id,
        closing_book=closing.book,
    )
