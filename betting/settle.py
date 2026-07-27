"""Bet settlement: what a finished bet returns.

Money is integer cents throughout, and the arithmetic runs in `Decimal` with
explicit ROUND_HALF_UP. Doing it in float would make a $50 bet at -150
return 8333.333333333332 cents, and a bankroll accumulated from values like
that drifts away from what the book actually paid.

`payout` here means TOTAL RETURNED, stake included - the number the book
credits back. A won bet returns stake plus profit; a push or void returns
the stake; a loss returns nothing. Storing total-returned rather than
profit means P&L is `payout - stake` and never needs to know the status.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from betting.odds import validate_american
from database.enums import BetStatus

__all__ = ["payout_cents", "profit_cents"]


def payout_cents(stake_cents: int, odds_american: int, status: BetStatus | str) -> int:
    """Total returned, in cents, for a settled bet.

    Raises if `status` is `open` - an unsettled bet has no payout, and
    returning 0 would be indistinguishable from a loss.
    """
    status = BetStatus(status)
    validate_american(odds_american)
    if stake_cents <= 0:
        raise ValueError(f"stake must be positive, got {stake_cents}")
    if status is BetStatus.OPEN:
        raise ValueError(
            "an open bet has no payout - settle it as won/lost/push/void first"
        )

    if status is BetStatus.LOST:
        return 0
    if status.returns_stake_only:
        # Push, void, postponed: stake back, no profit either way.
        return stake_cents

    # Won: stake + profit. Computed as stake * decimal_odds in one Decimal
    # expression so there is a single rounding step, not one on the profit
    # and another on the total.
    stake = Decimal(stake_cents)
    if odds_american > 0:
        decimal_odds = Decimal(1) + Decimal(odds_american) / Decimal(100)
    else:
        decimal_odds = Decimal(1) + Decimal(100) / Decimal(-odds_american)

    return int((stake * decimal_odds).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def profit_cents(stake_cents: int, payout_cents_: int) -> int:
    """Net result. Negative for a loss, zero for a push."""
    return payout_cents_ - stake_cents
