"""Power-method devigging tests.

PROVENANCE OF THE EXPECTED VALUES
---------------------------------
No test here asserts that the implementation equals itself. Every constant
comes from one of three independent sources:

  1. A closed form solved by hand (the symmetric market, where the exponent
     reduces to a ratio of logarithms).
  2. Inverse construction - pick fair probabilities and an exponent, build
     the vigged book from them, and require the devigger to recover what was
     put in.
  3. A 50-digit `decimal` bisection run outside this project, which imports
     none of this code. Those values are marked REF below and are quoted to
     9 decimal places; the assertions use 1e-6 tolerance.

Tolerance discipline: closed-form and inverse-construction cases assert to
1e-10 or tighter. REF cases assert to 1e-6.
"""
from __future__ import annotations

import math

import pytest

from betting.devig import (
    DevigResult,
    devig_american,
    devig_power,
    fair_odds_american,
    fair_probability,
    overround,
)
from betting.odds import american_to_implied_prob

# REF: 50-digit Decimal bisection of sum(p**k) == 1 for the -150/+130 book.
REF_TWO_WAY_K = 1.052969949
REF_TWO_WAY_FAIR = (0.583982635, 0.416017365)
# What multiplicative normalisation (the forbidden method) would return.
REF_TWO_WAY_MULTIPLICATIVE = (0.579831933, 0.420168067)

# REF: the same solve for a -2000/+1200 book.
REF_EXTREME_K = 1.137904956
REF_EXTREME_FAIR = (0.945994457, 0.054005543)

# REF: underround (arbitrage) book, +105/+105.
REF_ARB_K = 0.965601499


def multiplicative_devig(probs):
    """The method claude.md forbids. Present ONLY so tests can assert that
    the implementation does not match it."""
    total = math.fsum(probs)
    return tuple(p / total for p in probs)


class TestClosedForm:
    def test_symmetric_market_is_exactly_half(self):
        # -110 both sides: raw = 110/210 = 11/21 each.
        # Symmetry forces the answer to (0.5, 0.5), so the exponent has a
        # closed form: (11/21)**k = 1/2  =>  k = ln(0.5) / ln(11/21).
        expected_k = math.log(0.5) / math.log(11 / 21)
        assert expected_k == pytest.approx(1.0719425631310815, abs=1e-15)

        result = devig_american([-110, -110])
        assert result.fair_probs[0] == pytest.approx(0.5, abs=1e-12)
        assert result.fair_probs[1] == pytest.approx(0.5, abs=1e-12)
        assert result.k == pytest.approx(expected_k, abs=1e-9)

    def test_symmetric_market_is_where_both_methods_agree(self):
        # On a symmetric book the forbidden method gives the same answer.
        # This is the ONLY shape where that is true, which is exactly why a
        # -110/-110 test cannot detect a multiplicative implementation - and
        # why TestNotMultiplicative below uses an asymmetric book.
        raw = [american_to_implied_prob(-110)] * 2
        assert devig_power(raw).fair_probs == pytest.approx(
            multiplicative_devig(raw), abs=1e-12
        )

    def test_already_fair_book_is_identity(self):
        result = devig_power([0.5, 0.5])
        assert result.k == 1.0
        assert result.iterations == 0
        assert result.fair_probs == (0.5, 0.5)
        assert result.overround == 0.0


class TestInverseConstruction:
    """The strongest tests available: build the book from a known answer."""

    def test_recovers_planted_probabilities_three_way(self):
        fair = (0.5, 0.3, 0.2)
        k = 1.05
        # A book that priced these fair probabilities with exponent k would
        # post q_i = p_i ** (1/k). Devigging must invert that exactly.
        vigged = [p ** (1 / k) for p in fair]
        assert math.fsum(vigged) > 1.0  # it really is an overround book

        result = devig_power(vigged, tol=1e-15)
        assert result.k == pytest.approx(k, abs=1e-9)
        for got, want in zip(result.fair_probs, fair):
            assert got == pytest.approx(want, abs=1e-10)

    @pytest.mark.parametrize("k", [1.01, 1.05, 1.2, 1.5])
    @pytest.mark.parametrize(
        "fair",
        [
            (0.5, 0.5),
            (0.75, 0.25),
            (0.5, 0.3, 0.2),
            (0.4, 0.3, 0.2, 0.1),
            (0.9, 0.05, 0.03, 0.015, 0.005),
        ],
    )
    def test_recovers_planted_probabilities_across_shapes(self, fair, k):
        vigged = [p ** (1 / k) for p in fair]
        result = devig_power(vigged, tol=1e-15)
        assert result.k == pytest.approx(k, abs=1e-8)
        for got, want in zip(result.fair_probs, fair):
            assert got == pytest.approx(want, abs=1e-9)


class TestReferenceValues:
    def test_two_way_asymmetric(self):
        result = devig_american([-150, 130])
        assert result.raw_probs[0] == pytest.approx(0.6, abs=1e-15)
        assert result.raw_probs[1] == pytest.approx(100 / 230, abs=1e-15)
        assert result.k == pytest.approx(REF_TWO_WAY_K, abs=1e-6)
        assert result.fair_probs == pytest.approx(REF_TWO_WAY_FAIR, abs=1e-6)

    def test_overround_value(self):
        # 0.6 + 100/230 - 1
        assert overround([0.6, 100 / 230]) == pytest.approx(0.034782609, abs=1e-9)
        assert devig_american([-150, 130]).overround == pytest.approx(
            0.034782609, abs=1e-9
        )

    def test_extreme_favorite_is_numerically_stable(self):
        # p -> 1 is where a naive solver loses precision or overflows.
        result = devig_american([-2000, 1200])
        assert result.raw_probs[0] == pytest.approx(2000 / 2100, abs=1e-15)
        assert result.k == pytest.approx(REF_EXTREME_K, abs=1e-6)
        assert result.fair_probs == pytest.approx(REF_EXTREME_FAIR, abs=1e-6)
        assert math.fsum(result.fair_probs) == pytest.approx(1.0, abs=1e-12)


class TestNotMultiplicative:
    """Regression tests for the constraint. If someone replaces the solver
    with `p / sum(p)`, these fail."""

    def test_result_differs_from_multiplicative(self):
        result = devig_american([-150, 130])
        forbidden = multiplicative_devig(result.raw_probs)

        assert forbidden == pytest.approx(REF_TWO_WAY_MULTIPLICATIVE, abs=1e-6)
        # ~0.42 probability points apart - small in absolute terms, but a
        # systematic bias applied to every favorite for a whole season.
        assert abs(result.fair_probs[0] - forbidden[0]) > 1e-3

    def test_multiplicative_understates_the_favorite(self):
        # THE directional assertion. Multiplicative assigns the favorite a
        # LOWER fair probability than the power method does. A model backing
        # favorites would therefore compare against a market number that is
        # too low and see edge that is not there - manufactured, and in the
        # same direction, on every favorite it ever bets.
        result = devig_american([-150, 130])
        forbidden = multiplicative_devig(result.raw_probs)
        favorite = 0

        assert result.fair_probs[favorite] > forbidden[favorite]
        assert result.fair_probs[favorite] - forbidden[favorite] == pytest.approx(
            0.004150702, abs=1e-6
        )

    def test_bias_grows_with_asymmetry(self):
        # The two methods agree on a symmetric book and diverge as the book
        # gets lopsided - so a test suite that only ever checks -110/-110
        # would not notice the difference at all.
        gaps = []
        for odds in ([-110, -110], [-150, 130], [-400, 320], [-2000, 1200]):
            result = devig_american(odds)
            forbidden = multiplicative_devig(result.raw_probs)
            gaps.append(abs(result.fair_probs[0] - forbidden[0]))

        assert gaps == sorted(gaps)
        assert gaps[0] == pytest.approx(0.0, abs=1e-12)
        assert gaps[-1] > 0.02

    def test_shrinks_longshots_proportionally_more(self):
        # The mechanism behind the bias, stated as a property: the ratio
        # fair/raw increases with raw. The favorite keeps more of its raw
        # probability than the longshot does. Multiplicative would give every
        # selection the identical ratio.
        result = devig_american([-400, 250, 900])
        ratios = [f / r for f, r in zip(result.fair_probs, result.raw_probs)]
        by_raw = sorted(zip(result.raw_probs, ratios))
        ordered_ratios = [r for _, r in by_raw]

        assert ordered_ratios == sorted(ordered_ratios)
        assert ordered_ratios[0] < ordered_ratios[-1]


class TestProperties:
    @pytest.mark.parametrize("n", [2, 3, 4, 5, 6])
    @pytest.mark.parametrize("vig", [0.005, 0.02, 0.05, 0.15])
    def test_fair_probabilities_sum_to_one(self, n, vig):
        # Build an uneven book of n selections with the requested overround.
        base = [1.0 / (i + 2) for i in range(n)]
        scale = (1.0 + vig) / math.fsum(base)
        raw = [p * scale for p in base]

        result = devig_power(raw)
        assert math.fsum(result.fair_probs) == pytest.approx(1.0, abs=1e-12)

    def test_never_reorders_selections(self):
        raw = [0.52, 0.31, 0.14, 0.09]
        result = devig_power(raw)
        order_in = sorted(range(len(raw)), key=lambda i: raw[i])
        order_out = sorted(range(len(raw)), key=lambda i: result.fair_probs[i])
        assert order_in == order_out

    def test_positional_alignment_is_preserved(self):
        # fair_probs[i] must describe the same selection as the input at i.
        result = devig_american([-150, 130])
        assert result.fair_probs[0] > result.fair_probs[1]
        reversed_result = devig_american([130, -150])
        assert reversed_result.fair_probs[1] > reversed_result.fair_probs[0]
        assert result.fair_probs[0] == pytest.approx(
            reversed_result.fair_probs[1], abs=1e-12
        )

    def test_overround_implies_k_above_one(self):
        assert devig_american([-150, 130]).k > 1.0

    def test_every_probability_falls_when_margin_is_removed(self):
        # The sign-independent form of "every price gets longer". Because
        # k > 1 and every p is in (0, 1), p**k < p for all of them - no
        # selection ever gains probability from devigging an overround book.
        result = devig_american([-400, 250, 900])
        for fair, raw in zip(result.fair_probs, result.raw_probs):
            assert fair < raw

    def test_every_probability_rises_for_an_underround_book(self):
        # The mirror image: an arb book has k < 1, so p**k > p throughout.
        result = devig_american([105, 105])
        for fair, raw in zip(result.fair_probs, result.raw_probs):
            assert fair > raw

    def test_underround_implies_k_below_one(self):
        # An arbitrage book sums to less than 1. The solver has to bracket
        # DOWNWARD here; a solver that only ever searches k > 1 would return
        # k=1 and hand back un-devigged probabilities.
        result = devig_american([105, 105])
        assert math.fsum(result.raw_probs) < 1.0
        assert result.overround < 0
        assert result.k < 1.0
        assert result.k == pytest.approx(REF_ARB_K, abs=1e-6)
        assert math.fsum(result.fair_probs) == pytest.approx(1.0, abs=1e-12)

    def test_converges_quickly(self):
        # Bisection on a monotone function: a few dozen steps, not hundreds.
        assert devig_american([-150, 130]).iterations < 80


class TestValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            [0.5],  # one selection carries no margin information
            [],
            [0.6, 0.0],  # p <= 0
            [0.6, -0.1],
            [0.6, 1.0],  # p == 1 makes f(k) constant - no root exists
            [0.6, 1.5],
            [0.6, float("nan")],
            [0.6, float("inf")],
        ],
    )
    def test_rejects_bad_probabilities(self, bad):
        with pytest.raises(ValueError):
            devig_power(bad)

    def test_single_selection_message_is_specific(self):
        with pytest.raises(ValueError, match="at least 2 selections"):
            devig_power([0.5])

    def test_probability_of_one_is_rejected_not_hung(self):
        # 1**k == 1 for every k, so the solver would never bracket a root.
        with pytest.raises(ValueError, match="strictly between 0 and 1"):
            devig_power([1.0, 0.5])

    def test_rejects_invalid_american_odds(self):
        with pytest.raises(ValueError, match="not a valid American price"):
            devig_american([-150, 50])

    def test_mismatched_result_lengths_rejected(self):
        with pytest.raises(ValueError, match="same length"):
            DevigResult(
                fair_probs=(0.5, 0.5),
                raw_probs=(0.6,),
                k=1.0,
                overround=0.0,
                iterations=0,
            )


class TestHelpers:
    def test_fair_probability_selects_by_index(self):
        assert fair_probability([-150, 130], 0) == pytest.approx(
            REF_TWO_WAY_FAIR[0], abs=1e-6
        )
        assert fair_probability([-150, 130], 1) == pytest.approx(
            REF_TWO_WAY_FAIR[1], abs=1e-6
        )

    def test_fair_odds_american_removes_the_margin(self):
        fair = fair_odds_american([-150, 130])
        # Every price gets LONGER once the book's margin comes out, but
        # "longer" moves in opposite numeric directions either side of even
        # money: a favorite drifts toward zero, a dog away from it.
        assert fair == (-140, 140)
        assert fair[0] > -150  # favorite: -150 -> -140
        assert fair[1] > 130  # dog:      +130 -> +140
        # Re-imply them and the market is fair to within integer rounding.
        total = sum(american_to_implied_prob(o) for o in fair)
        assert total == pytest.approx(1.0, abs=0.005)

    def test_fair_odds_american_preserves_ordering(self):
        fair = fair_odds_american([-150, 130])
        assert american_to_implied_prob(fair[0]) > american_to_implied_prob(fair[1])

    def test_result_is_immutable(self):
        result = devig_american([-150, 130])
        with pytest.raises(AttributeError):
            result.k = 2.0  # type: ignore[misc]
