"""Manual bet logging and CLV inspection.

Phase 0 has no API and no UI. This is the only way in, and it exists so the
measurement loop can be exercised end to end before any ingest worker is
written: `snapshot add` stands in for the odds poller, so CLV is testable
against real data today.

    python -m betting.cli bet log --game-pk 776543 --game-date 2026-07-27 \
        --commence-time 2026-07-27T23:05:00Z --book pinnacle \
        --market moneyline --selection away --odds=+130 --stake 50.00

    python -m betting.cli bet settle --id 1 --status won
    python -m betting.cli bet clv --id 1
    python -m betting.cli bet list --status open
    python -m betting.cli devig --odds=-150 --odds=+130

Every mutating command opens its own `ingest_runs` row with
source='manual-cli', so a hand-entered price carries exactly the same
provenance as a scraped one and nothing in the database is of unknown
origin.

The command handlers are thin wrappers over plain async functions
(`log_bet`, `settle_bet`, `add_snapshot`, ...) so tests call those directly
rather than shelling out to a subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import inspect
import sys
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from betting.clv import compute_clv_for_bet
from betting.devig import devig_american
from betting.odds import validate_american
from betting.settle import payout_cents as compute_payout
from betting.settle import profit_cents
from database.engine import session_scope
from database.enums import SELECTIONS_FOR_MARKET, BetStatus, Market, Selection
from database.ingest_run import ingest_run
from database.models import Bet, OddsSnapshot
from database.utc import require_utc, utcnow

__all__ = [
    "add_snapshot",
    "build_parser",
    "list_bets",
    "log_bet",
    "main",
    "parse_american",
    "parse_stake_cents",
    "parse_utc",
    "settle_bet",
]

SOURCE = "manual-cli"


# --- argument parsing -----------------------------------------------------


def parse_american(value: str) -> int:
    """Parse an American price. Accepts '+130', '130', '-150'.

    Note for callers on the command line: prefer `--odds=-150` over
    `--odds -150`. Both work today - argparse treats a negative number as a
    value when no option string looks like one - but that behaviour depends
    on the parser having no numeric-looking flags, which is a property that
    could change as the CLI grows. The `=` form never depends on it.
    """
    text = value.strip()
    try:
        odds = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an integer American price (e.g. -150, +130)"
        ) from None
    try:
        return validate_american(odds)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def parse_stake_cents(value: str) -> int:
    """Parse a money amount into integer cents.

    Via Decimal, never float: int(float("50.10") * 100) is 5009.
    More than two decimal places is rejected rather than rounded, because
    at this scale it is a typo, not a real sub-cent stake.
    """
    try:
        amount = Decimal(value.strip().lstrip("$"))
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a valid amount (e.g. 50, 50.00, 12.34)"
        ) from None
    # NaN and Infinity are valid Decimals. NaN fails every comparison, so it
    # slips past `amount <= 0`, and its exponent is the string 'n' rather than
    # an int - negating which raises TypeError instead of a usable message.
    if not amount.is_finite():
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a finite amount (e.g. 50, 50.00, 12.34)"
        )
    if amount <= 0:
        raise argparse.ArgumentTypeError(f"stake must be positive, got {value!r}")
    exponent = amount.as_tuple().exponent
    assert isinstance(exponent, int)  # guaranteed by is_finite() above
    if -exponent > 2:
        raise argparse.ArgumentTypeError(
            f"{value!r} has sub-cent precision; use at most two decimal places"
        )
    return int(amount * 100)


def parse_utc(value: str) -> dt.datetime:
    """Parse an ISO-8601 timestamp, requiring an explicit offset.

    A bare '2026-07-27T23:05:00' is rejected: the whole point of the UTC
    convention is that no code ever guesses which zone a timestamp meant.
    """
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO-8601 timestamp "
            "(e.g. 2026-07-27T23:05:00Z or 2026-07-27T19:05:00-04:00)"
        ) from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} has no timezone offset. Timestamps must be explicit - "
            "append 'Z' for UTC, or an offset like '-04:00'."
        )
    return parsed.astimezone(dt.UTC)


def parse_date(value: str) -> dt.date:
    """Parse a venue-local calendar date (YYYY-MM-DD)."""
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date in YYYY-MM-DD form"
        ) from None


def parse_line(value: str) -> Decimal:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a valid line (e.g. 8.5, -1.5)"
        ) from None


def validate_market_selection(
    market: str, selection: str, line: Decimal | None
) -> tuple[str, str, Decimal | None]:
    """Reject incoherent market/selection/line combinations at the boundary.

    The database enforces all of this too, but a CHECK violation surfaces as
    an IntegrityError naming a constraint, which is a poor thing to show
    someone typing a bet in by hand.
    """
    try:
        market_enum = Market(market)
        selection_enum = Selection(selection)
    except ValueError as exc:
        raise ValueError(str(exc)) from None

    allowed = SELECTIONS_FOR_MARKET[market_enum]
    if selection_enum not in allowed:
        raise ValueError(
            f"selection '{selection}' is not valid for a {market} market; "
            f"expected one of {', '.join(s.value for s in allowed)}"
        )
    if market_enum.needs_line and line is None:
        raise ValueError(f"a {market} bet needs --line (e.g. --line 8.5)")
    if not market_enum.needs_line and line is not None:
        raise ValueError(f"a {market} bet has no line; drop --line")
    return market_enum.value, selection_enum.value, line


# --- operations -----------------------------------------------------------


async def add_snapshot(
    session: AsyncSession,
    *,
    game_pk: int,
    game_date: dt.date,
    commence_time_utc: dt.datetime,
    book: str,
    market: str,
    selection: str,
    odds_american: int,
    captured_at: dt.datetime | None = None,
    line: Decimal | None = None,
) -> OddsSnapshot | None:
    """Record one observed price.

    Append-only: this never updates a row. Re-recording the same observation
    is a no-op rather than a duplicate or an error, so a retried ingest run
    is idempotent. Returns None when the row already existed.

    `captured_at` defaults to the ingest run's start time, NOT to now().
    Every price from one poll then shares a timestamp, which is what makes
    the unique constraint able to recognise a retry. Pass it explicitly only
    when back-dating a hand-entered historical price.
    """
    market, selection, line = validate_market_selection(market, selection, line)
    validate_american(odds_american)

    async with ingest_run(
        session,
        source=SOURCE,
        run_kind="snapshot_add",
        params={"game_pk": game_pk, "book": book, "market": market},
    ) as run:
        observed_at = require_utc(captured_at) if captured_at else run.started_at

        # ON CONFLICT DO NOTHING inserts or it does not; it never rewrites an
        # existing row, so this is not an upsert and append-only still holds.
        # RETURNING yields no row when the observation was already recorded.
        stmt = (
            pg_insert(OddsSnapshot)
            .values(
                ingest_run_id=run.id,
                game_pk=game_pk,
                game_date=game_date,
                commence_time_utc=require_utc(commence_time_utc),
                book=book,
                market=market,
                selection=selection,
                line=line,
                odds_american=odds_american,
                captured_at=observed_at,
            )
            .on_conflict_do_nothing(constraint="uq_odds_snapshots_observation")
            .returning(OddsSnapshot)
        )
        snapshot = (await session.execute(stmt)).scalars().first()
        run.rows_written = 1 if snapshot is not None else 0
    return snapshot


async def log_bet(
    session: AsyncSession,
    *,
    game_pk: int,
    game_date: dt.date,
    commence_time_utc: dt.datetime,
    book: str,
    market: str,
    selection: str,
    odds_american: int,
    stake_cents: int,
    line: Decimal | None = None,
    placed_at: dt.datetime | None = None,
    model_prob: Decimal | None = None,
    notes: str | None = None,
) -> Bet:
    market, selection, line = validate_market_selection(market, selection, line)
    validate_american(odds_american)
    if stake_cents <= 0:
        raise ValueError(f"stake must be positive, got {stake_cents} cents")

    async with ingest_run(
        session,
        source=SOURCE,
        run_kind="bet_log",
        params={"game_pk": game_pk, "market": market, "selection": selection},
    ) as run:
        bet = Bet(
            game_pk=game_pk,
            game_date=game_date,
            commence_time_utc=require_utc(commence_time_utc),
            book=book,
            market=market,
            selection=selection,
            line=line,
            odds_american=odds_american,
            stake_cents=stake_cents,
            placed_at=require_utc(placed_at or utcnow()),
            status=BetStatus.OPEN.value,
            model_prob=model_prob,
            notes=notes,
        )
        session.add(bet)
        run.rows_written = 1
    return bet


async def settle_bet(
    session: AsyncSession,
    bet_id: int,
    status: BetStatus | str,
    *,
    payout_cents: int | None = None,
    settled_at: dt.datetime | None = None,
) -> Bet:
    """Settle a bet, computing the payout unless one is supplied.

    `payout_cents` overrides the computed figure, for when the book rounded
    differently or applied a promotion - what the book actually paid is the
    fact worth storing.
    """
    status = BetStatus(status)
    if status is BetStatus.OPEN:
        raise ValueError("cannot settle a bet to 'open'")

    bet = await session.get(Bet, bet_id)
    if bet is None:
        raise LookupError(f"no bet with id {bet_id}")
    if BetStatus(bet.status).is_settled:
        raise ValueError(
            f"bet {bet_id} is already settled as '{bet.status}'; "
            "settlement is not re-run in place"
        )

    async with ingest_run(
        session, source=SOURCE, run_kind="bet_settle", params={"bet_id": bet_id}
    ) as run:
        bet.status = status.value
        bet.settled_at = require_utc(settled_at or utcnow())
        bet.payout_cents = (
            payout_cents
            if payout_cents is not None
            else compute_payout(bet.stake_cents, bet.odds_american, status)
        )
        run.rows_written = 1
    return bet


async def list_bets(
    session: AsyncSession,
    *,
    status: str | None = None,
    game_date: dt.date | None = None,
) -> list[Bet]:
    stmt = select(Bet).order_by(Bet.placed_at.desc(), Bet.id.desc())
    if status is not None:
        stmt = stmt.where(Bet.status == BetStatus(status).value)
    if game_date is not None:
        stmt = stmt.where(Bet.game_date == game_date)
    return list((await session.execute(stmt)).scalars().all())


# --- formatting -----------------------------------------------------------


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100}.{abs(cents) % 100:02d}"


def _odds(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def _describe(bet: Bet) -> str:
    line = f" {bet.line}" if bet.line is not None else ""
    return (
        f"#{bet.id} game {bet.game_pk} {bet.game_date} {bet.book} "
        f"{bet.market} {bet.selection}{line} @ {_odds(bet.odds_american)} "
        f"for {_money(bet.stake_cents)} [{bet.status}]"
    )


# --- command handlers -----------------------------------------------------


async def _cmd_bet_log(args) -> int:
    async with session_scope() as session:
        bet = await log_bet(
            session,
            game_pk=args.game_pk,
            game_date=args.game_date,
            commence_time_utc=args.commence_time,
            book=args.book,
            market=args.market,
            selection=args.selection,
            line=args.line,
            odds_american=args.odds,
            stake_cents=args.stake,
            placed_at=args.placed_at,
            model_prob=args.model_prob,
            notes=args.notes,
        )
        await session.flush()
        print(f"logged {_describe(bet)}")
    return 0


async def _cmd_bet_settle(args) -> int:
    async with session_scope() as session:
        bet = await settle_bet(session, args.id, args.status, payout_cents=args.payout)
        await session.flush()
        # settle_bet always assigns a payout; ck_bets_settlement_coherent
        # would reject the row otherwise.
        assert bet.payout_cents is not None
        profit = profit_cents(bet.stake_cents, bet.payout_cents)
        print(
            f"settled {_describe(bet)}\n"
            f"  returned {_money(bet.payout_cents)}  "
            f"profit {_money(profit)}"
        )
    return 0


async def _cmd_bet_clv(args) -> int:
    async with session_scope() as session:
        result = await compute_clv_for_bet(session, args.id)
        if result is None:
            print(
                f"bet #{args.id}: no closing price captured before first pitch - "
                "CLV unavailable.\n"
                "  Add the closing snapshot with `snapshot add`, or wait for an "
                "odds poll to record one."
            )
            return 0

        print(f"bet #{args.id}")
        print(
            f"  bet at      {_odds(result.bet_odds_american)} "
            f"(break-even {result.bet_breakeven_prob:.4f})"
        )
        print(
            f"  closed at   {_odds(result.closing_odds_american)} "
            f"({result.closing_captured_at:%Y-%m-%d %H:%M:%S}Z)"
        )
        print(f"  CLV (price) {result.clv_pct:+.3f}%")
        if result.clv_prob_points is None:
            print(
                "  CLV (no-vig) unavailable - the opposing side's closing price "
                "was never captured, and one price cannot be devigged."
            )
        else:
            print(
                f"  CLV (no-vig) {result.clv_prob_points:+.3f} pts   "
                f"fair closing prob {result.fair_closing_prob:.4f} "
                "(power method)"
            )
        print(f"  beat close  {'yes' if result.beat_close else 'no'}")
    return 0


async def _cmd_bet_list(args) -> int:
    async with session_scope() as session:
        bets = await list_bets(session, status=args.status, game_date=args.game_date)
        if not bets:
            print("no bets match")
            return 0
        for bet in bets:
            suffix = ""
            if bet.payout_cents is not None:
                suffix = (
                    f"  returned {_money(bet.payout_cents)}  "
                    f"profit {_money(profit_cents(bet.stake_cents, bet.payout_cents))}"
                )
            print(_describe(bet) + suffix)
        print(f"\n{len(bets)} bet(s)")
    return 0


async def _cmd_snapshot_add(args) -> int:
    async with session_scope() as session:
        snapshot = await add_snapshot(
            session,
            game_pk=args.game_pk,
            game_date=args.game_date,
            commence_time_utc=args.commence_time,
            book=args.book,
            market=args.market,
            selection=args.selection,
            line=args.line,
            odds_american=args.odds,
            captured_at=args.captured_at,
        )
        await session.flush()
        if snapshot is None:
            # Already recorded. ON CONFLICT DO NOTHING makes a retry a no-op
            # rather than an error, so say so plainly instead of implying a
            # write happened.
            when = (
                f"{args.captured_at:%Y-%m-%d %H:%M:%S}Z"
                if args.captured_at
                else "this run's start time"
            )
            print(
                f"already recorded: game {args.game_pk} {args.book} "
                f"{args.market} {args.selection} at {when} - no new row written"
            )
            return 0
        line = f" {snapshot.line}" if snapshot.line is not None else ""
        print(
            f"recorded game {snapshot.game_pk} {snapshot.book} {snapshot.market} "
            f"{snapshot.selection}{line} @ {_odds(snapshot.odds_american)} "
            f"at {snapshot.captured_at:%Y-%m-%d %H:%M:%S}Z"
        )
    return 0


def _cmd_devig(args) -> int:
    result = devig_american(args.odds)
    print(f"overround {result.overround * 100:+.3f}%   k = {result.k:.9f}")
    triples = zip(args.odds, result.raw_probs, result.fair_probs, strict=True)
    for odds, raw, fair in triples:
        print(f"  {_odds(odds):>7}  raw {raw:.6f}  ->  fair {fair:.6f}")
    return 0


# --- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="betting.cli",
        description="Log bets and measure closing line value.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_market_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--game-pk", type=int, required=True, help="MLB gamePk")
        p.add_argument(
            "--game-date",
            type=parse_date,
            required=True,
            help="venue-LOCAL date (YYYY-MM-DD), from StatsAPI officialDate - "
            "not the UTC date of first pitch",
        )
        p.add_argument(
            "--commence-time",
            type=parse_utc,
            required=True,
            help="first pitch, ISO-8601 with offset (e.g. 2026-07-27T23:05:00Z)",
        )
        p.add_argument("--book", required=True)
        p.add_argument("--market", required=True, choices=[m.value for m in Market])
        p.add_argument(
            "--selection", required=True, choices=[s.value for s in Selection]
        )
        p.add_argument("--line", type=parse_line, default=None)
        p.add_argument(
            "--odds",
            type=parse_american,
            required=True,
            help="American price; use --odds=-150 for negative values",
        )

    bet = sub.add_parser("bet", help="log, settle, list and price bets")
    bet_sub = bet.add_subparsers(dest="subcommand", required=True)

    log = bet_sub.add_parser("log", help="record a bet you placed")
    add_market_args(log)
    log.add_argument("--stake", type=parse_stake_cents, required=True)
    log.add_argument("--placed-at", type=parse_utc, default=None)
    log.add_argument("--model-prob", type=Decimal, default=None)
    log.add_argument("--notes", default=None)
    log.set_defaults(func=_cmd_bet_log)

    settle = bet_sub.add_parser("settle", help="settle a bet and record the payout")
    settle.add_argument("--id", type=int, required=True)
    settle.add_argument(
        "--status",
        required=True,
        choices=[s.value for s in BetStatus if s is not BetStatus.OPEN],
    )
    settle.add_argument(
        "--payout",
        type=parse_stake_cents,
        default=None,
        help="override the computed payout (total returned, incl. stake)",
    )
    settle.set_defaults(func=_cmd_bet_settle)

    clv = bet_sub.add_parser("clv", help="closing line value for a bet")
    clv.add_argument("--id", type=int, required=True)
    clv.set_defaults(func=_cmd_bet_clv)

    listing = bet_sub.add_parser("list", help="list logged bets")
    listing.add_argument("--status", choices=[s.value for s in BetStatus], default=None)
    listing.add_argument("--game-date", type=parse_date, default=None)
    listing.set_defaults(func=_cmd_bet_list)

    snapshot = sub.add_parser("snapshot", help="record observed prices")
    snapshot_sub = snapshot.add_subparsers(dest="subcommand", required=True)
    add = snapshot_sub.add_parser("add", help="record one observed price")
    add_market_args(add)
    add.add_argument(
        "--captured-at",
        type=parse_utc,
        default=None,
        help="when the price was observed; defaults to now",
    )
    add.set_defaults(func=_cmd_snapshot_add)

    devig = sub.add_parser("devig", help="devig a market (power method)")
    devig.add_argument(
        "--odds",
        type=parse_american,
        action="append",
        required=True,
        help="repeat once per selection, e.g. --odds=-150 --odds=+130",
    )
    devig.set_defaults(func=_cmd_devig)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        # inspect.iscoroutinefunction, not asyncio's: the asyncio alias is
        # deprecated in 3.14 and removed in 3.16.
        if inspect.iscoroutinefunction(args.func):
            # Windows defaults to ProactorEventLoop, which psycopg's async
            # mode cannot use. Selecting the loop explicitly is required at
            # runtime, not just under test.
            loop_factory = (
                asyncio.SelectorEventLoop if sys.platform == "win32" else None
            )
            return asyncio.run(args.func(args), loop_factory=loop_factory)
        return args.func(args)
    except (ValueError, LookupError) as exc:
        # Expected, user-facing failures: a bad market/selection combination
        # or an unknown id. A traceback here would bury the message.
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
