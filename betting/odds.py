"""Odds format conversions.

Per claude.md's conventions, odds live as American integers everywhere -
in the database, in function arguments, in the CLI. Decimal odds and implied
probabilities are *derived* representations that exist only inside a
calculation. This module is the boundary where that conversion happens, and
it is the only place in the codebase allowed to do it.

Everything here is pure: no I/O, no state, no configuration.

Sign convention for American odds:
    -150  stake 150 to win 100  (favorite)
    +130  stake 100 to win 130  (underdog)
There is no such thing as odds of 0, and no valid price falls strictly
between -100 and +100 - both sides of that gap describe the same thing,
and +100 == -100 == even money. `validate_american` rejects the gap, which
catches the common failure of a percentage or a units value reaching an
odds argument.
"""

from __future__ import annotations

import math

__all__ = [
    "american_to_decimal",
    "american_to_implied_prob",
    "decimal_to_american",
    "implied_prob_to_american",
    "profit_multiple",
    "validate_american",
]


def validate_american(odds: int) -> int:
    """Return `odds` unchanged, or raise ValueError if it is not a price.

    Rejects bools explicitly: `isinstance(True, int)` is True in Python, and
    a stray boolean would otherwise sail through as +1 and be rejected only
    by the range check, with a misleading message.
    """
    if isinstance(odds, bool) or not isinstance(odds, int):
        raise ValueError(
            f"American odds must be an int, got {type(odds).__name__}: {odds!r}"
        )
    if -100 < odds < 100:
        raise ValueError(
            f"{odds} is not a valid American price: no odds fall strictly between "
            "-100 and +100. A value in this range is usually a probability, a "
            "percentage, or a stake that reached an odds argument by mistake."
        )
    return odds


def american_to_decimal(odds: int) -> float:
    """American -> decimal (European) odds, i.e. total return per 1 staked.

    -150 -> 1.666...  (+100 -> 2.0 -> even money)
    +130 -> 2.30
    """
    validate_american(odds)
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / -odds


def decimal_to_american(decimal_odds: float) -> int:
    """Decimal -> American, rounded to the nearest integer price.

    Even money (2.0) is returned as +100 rather than -100; the two are
    identical prices and the positive form is conventional.
    """
    if not math.isfinite(decimal_odds) or decimal_odds <= 1.0:
        raise ValueError(
            f"decimal odds must be finite and greater than 1.0, got {decimal_odds!r}"
        )
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1.0) * 100.0)
    return -round(100.0 / (decimal_odds - 1.0))


def american_to_implied_prob(odds: int) -> float:
    """American -> raw implied probability, vig included.

    This is the break-even win rate for the price, NOT an estimate of the
    true probability - a set of these across a market sums to more than 1.
    Removing the difference is `betting.devig`'s job, and comparing a model
    probability against this number directly is the mistake that manufactures
    edge out of the book's margin.

    -150 -> 0.6 exactly       +130 -> 100/230
    """
    validate_american(odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def implied_prob_to_american(prob: float) -> int:
    """Probability -> the American price whose break-even rate is `prob`."""
    if not math.isfinite(prob) or not 0.0 < prob < 1.0:
        raise ValueError(f"probability must be strictly between 0 and 1, got {prob!r}")
    if prob <= 0.5:
        return round(100.0 * (1.0 - prob) / prob)
    return -round(100.0 * prob / (1.0 - prob))


def profit_multiple(odds: int) -> float:
    """Net profit per 1 unit staked - decimal odds minus the returned stake.

    -150 -> 0.666...   +130 -> 1.30
    This is `b` in the Kelly formulation, and the multiplier used to settle
    a winning bet.
    """
    return american_to_decimal(odds) - 1.0
