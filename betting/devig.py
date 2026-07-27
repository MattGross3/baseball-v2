"""Devigging by the power method.

A book's posted prices always imply probabilities summing to more than 1.
The excess is the book's margin, not uncertainty about the game. Recovering
the book's actual opinion means finding the fair probabilities that the
posted ones were derived from.

The power method assumes the book took fair probabilities `p_i` and posted
`q_i = p_i ** (1/k)` for some single exponent `k`. Inverting that means
solving for the `k` where

    sum(q_i ** k) == 1

and reporting `q_i ** k` as the fair probabilities.

WHY NOT MULTIPLICATIVE NORMALIZATION (q_i / sum(q))
---------------------------------------------------
Proportional normalization removes the same *fraction* of probability from
every selection. Real books do not price that way: they load more margin
onto longshots than onto favorites. The power method reproduces that, taking
proportionally more from the longshot than the favorite.

Concretely, for a -150 / +130 moneyline (raw 0.600000 / 0.434783):

    power method    0.583983 / 0.416017
    multiplicative  0.579832 / 0.420168

Multiplicative understates the favorite's fair probability by ~0.42 points.
A model backing that favorite would compare its own number against a market
probability that is too low, and see edge that is not there. Repeated across
a season of favorites, this manufactures a positive backtest out of nothing
but the devig choice. That is why claude.md forbids it and why this module
never normalizes - not even as a final tidy-up after solving (see the note
in `devig_power`).

Everything here is pure: no I/O, no state, no configuration.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from betting.odds import (
    american_to_implied_prob,
    implied_prob_to_american,
    validate_american,
)

__all__ = [
    "DevigResult",
    "devig_american",
    "devig_power",
    "fair_odds_american",
    "fair_probability",
    "overround",
]

# Bounds on the exponent search. These are far outside anything a real market
# produces (a -150/+130 book solves near k=1.05); they exist so a nonsensical
# input fails loudly instead of spinning.
_K_MIN = 1e-6
_K_MAX = 1e6


@dataclass(frozen=True, slots=True)
class DevigResult:
    """Outcome of a devig. `fair_probs` is positionally aligned with the input."""

    fair_probs: tuple[float, ...]
    raw_probs: tuple[float, ...]
    k: float
    overround: float
    iterations: int

    def __post_init__(self) -> None:
        if len(self.fair_probs) != len(self.raw_probs):
            raise ValueError("fair_probs and raw_probs must be the same length")


def _validate_probs(raw_probs: Sequence[float]) -> tuple[float, ...]:
    probs = tuple(float(p) for p in raw_probs)
    if len(probs) < 2:
        raise ValueError(
            f"devigging needs at least 2 selections, got {len(probs)}. A single "
            "price carries no information about the book's margin."
        )
    for i, p in enumerate(probs):
        if not math.isfinite(p):
            raise ValueError(f"probability at index {i} is not finite: {p!r}")
        if p <= 0.0 or p >= 1.0:
            # p == 1.0 is not merely out of range, it breaks the solver:
            # 1**k == 1 for every k, so f(k) has no root to find.
            raise ValueError(
                f"probability at index {i} must be strictly between 0 and 1, got {p!r}"
            )
    return probs


def overround(raw_probs: Sequence[float]) -> float:
    """How much more than 1 the raw probabilities sum to. 0.035 == 3.5% vig."""
    return math.fsum(_validate_probs(raw_probs)) - 1.0


def _solve_k(probs: tuple[float, ...], tol: float, max_iter: int) -> tuple[float, int]:
    """Bisect for the k where sum(p**k) == 1.

    f(k) = sum(p_i**k) - 1 is strictly decreasing in k, because every p_i is
    in (0, 1) and d/dk p**k = p**k * ln(p) < 0. A strictly monotone function
    has exactly one root and can be bracketed without a derivative, which is
    why this is bisection rather than Newton: no starting-guess sensitivity,
    no divergence, no failure mode to handle.
    """

    def f(k: float) -> float:
        return math.fsum(p**k for p in probs) - 1.0

    f_at_one = f(1.0)
    if abs(f_at_one) <= tol:
        # Already a fair book (sum == 1). k is exactly 1 and the probabilities
        # pass through untouched.
        return 1.0, 0

    iterations = 0
    if f_at_one > 0.0:
        # Overround: sum > 1, so the root is above 1. Grow the upper bound
        # until f goes negative.
        lo, hi = 1.0, 2.0
        while f(hi) > 0.0:
            lo = hi
            hi *= 2.0
            iterations += 1
            if hi > _K_MAX:
                raise ValueError(
                    f"no solution for k below {_K_MAX:g}; raw probabilities sum to "
                    f"{math.fsum(probs):.6f}, which is not a plausible market"
                )
    else:
        # Underround: sum < 1 (an arbitrage, or a book quoted with negative
        # margin). The root is below 1. Shrink the lower bound until f goes
        # positive. Handling this direction is what keeps arbs from silently
        # returning k=1 and un-devigged probabilities.
        lo, hi = 0.5, 1.0
        while f(lo) < 0.0:
            hi = lo
            lo /= 2.0
            iterations += 1
            if lo < _K_MIN:
                raise ValueError(
                    f"no solution for k above {_K_MIN:g}; raw probabilities sum to "
                    f"{math.fsum(probs):.6f}, which is not a plausible market"
                )

    mid = 0.5 * (lo + hi)
    while iterations < max_iter:
        mid = 0.5 * (lo + hi)
        residual = f(mid)
        iterations += 1
        if abs(residual) <= tol or (hi - lo) <= 1e-15 * max(1.0, mid):
            return mid, iterations
        # f decreasing: positive residual means we are still below the root.
        if residual > 0.0:
            lo = mid
        else:
            hi = mid

    raise ValueError(
        f"k did not converge to {tol:g} within {max_iter} iterations "
        f"(last k={mid!r}, residual={f(mid)!r})"
    )


def devig_power(
    raw_probs: Sequence[float],
    *,
    tol: float = 1e-12,
    max_iter: int = 200,
) -> DevigResult:
    """Strip vig from raw implied probabilities using the power method.

    Works for any number of selections - two-way moneylines, three-way
    markets and N-runner fields all take the same path, with no special case
    for N == 2.
    """
    probs = _validate_probs(raw_probs)
    k, iterations = _solve_k(probs, tol, max_iter)
    fair = tuple(p**k for p in probs)

    # Deliberately NOT renormalising here. `fair` sums to 1 within `tol`
    # because that is precisely what the solver converged on; dividing through
    # by the residual sum to force an exact 1.0 would be a multiplicative
    # normalisation applied on top of the power method, reintroducing in
    # miniature the bias this whole module exists to avoid. If the residual
    # ever matters, tighten `tol`.
    return DevigResult(
        fair_probs=fair,
        raw_probs=probs,
        k=k,
        overround=math.fsum(probs) - 1.0,
        iterations=iterations,
    )


def devig_american(
    odds: Sequence[int],
    *,
    tol: float = 1e-12,
    max_iter: int = 200,
) -> DevigResult:
    """Devig a market quoted as American integers - the usual entry point."""
    raw = [american_to_implied_prob(validate_american(o)) for o in odds]
    return devig_power(raw, tol=tol, max_iter=max_iter)


def fair_probability(odds: Sequence[int], index: int) -> float:
    """Fair probability of one selection, given every price in its market.

    `index` selects which one. The full market is required because a single
    price cannot be devigged - the margin is only visible across all sides.
    """
    result = devig_american(odds)
    return result.fair_probs[index]


def fair_odds_american(odds: Sequence[int]) -> tuple[int, ...]:
    """The same market with the margin removed, re-quoted as American ints.

    Rounding to integer prices means these will not devig back to exactly the
    same probabilities - a fair price is rarely expressible as a whole
    American number. Use `devig_american(...).fair_probs` for arithmetic, and
    this only for display.
    """
    return tuple(implied_prob_to_american(p) for p in devig_american(odds).fair_probs)
