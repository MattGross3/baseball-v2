"""Odds conversion tests.

Expected values are exact fractions computed by hand from the definition of
American odds, not read off the implementation.
"""

from __future__ import annotations

import pytest

from betting.odds import (
    american_to_decimal,
    american_to_implied_prob,
    decimal_to_american,
    implied_prob_to_american,
    profit_multiple,
    validate_american,
)


class TestImpliedProbability:
    def test_negative_american(self):
        # -150 risks 150 to win 100 -> break-even at 150 / (150 + 100).
        assert american_to_implied_prob(-150) == pytest.approx(0.6, abs=1e-15)
        assert american_to_implied_prob(-150) == 150 / 250

    def test_positive_american(self):
        # +130 risks 100 to win 130 -> break-even at 100 / (130 + 100).
        assert american_to_implied_prob(130) == pytest.approx(100 / 230, abs=1e-15)
        assert american_to_implied_prob(130) == pytest.approx(
            0.4347826086956522, abs=1e-15
        )

    def test_even_money_both_signs_agree(self):
        # +100 and -100 are the same price written two ways.
        assert american_to_implied_prob(100) == 0.5
        assert american_to_implied_prob(-100) == 0.5

    def test_a_market_sums_to_more_than_one(self):
        # The property that makes devigging necessary at all: a real book's
        # two sides always imply more than 100%.
        total = american_to_implied_prob(-150) + american_to_implied_prob(130)
        assert total > 1.0
        assert total == pytest.approx(1.0347826086956522, abs=1e-12)


class TestDecimalConversion:
    def test_american_to_decimal(self):
        assert american_to_decimal(-150) == pytest.approx(1 + 100 / 150, abs=1e-15)
        assert american_to_decimal(130) == pytest.approx(2.30, abs=1e-15)
        assert american_to_decimal(100) == 2.0
        assert american_to_decimal(-100) == 2.0

    def test_roundtrip_is_exact_over_the_realistic_range(self):
        # Every price a book actually posts must survive a round trip
        # unchanged. -100 is excluded because it collapses onto +100 (below).
        for odds in list(range(100, 2001)) + list(range(-2000, -100)):
            assert decimal_to_american(american_to_decimal(odds)) == odds, odds

    def test_even_money_normalises_to_positive(self):
        # 2.0 decimal is a single price; the positive form is conventional,
        # so -100 deliberately does NOT round-trip.
        assert decimal_to_american(2.0) == 100
        assert decimal_to_american(american_to_decimal(-100)) == 100

    def test_rejects_decimal_at_or_below_one(self):
        # Decimal odds of 1.0 would be a bet that returns exactly the stake.
        for bad in (1.0, 0.5, 0.0, -2.0, float("inf"), float("nan")):
            with pytest.raises(ValueError, match="must be finite and greater than 1"):
                decimal_to_american(bad)


class TestProbabilityToAmerican:
    def test_roundtrip_through_probability(self):
        for odds in (-2000, -300, -150, -110, -101, 101, 110, 130, 300, 2000):
            prob = american_to_implied_prob(odds)
            assert implied_prob_to_american(prob) == odds, odds

    def test_half_is_even_money(self):
        assert implied_prob_to_american(0.5) == 100

    def test_rejects_out_of_range(self):
        for bad in (0.0, 1.0, -0.1, 1.5, float("nan")):
            with pytest.raises(ValueError, match="strictly between 0 and 1"):
                implied_prob_to_american(bad)


class TestProfitMultiple:
    def test_values(self):
        # Stake 150 to win 100 -> 100/150 profit per unit.
        assert profit_multiple(-150) == pytest.approx(2 / 3, abs=1e-15)
        assert profit_multiple(130) == pytest.approx(1.30, abs=1e-15)
        assert profit_multiple(100) == 1.0

    def test_is_decimal_minus_returned_stake(self):
        for odds in (-2000, -150, -100, 100, 130, 2000):
            assert profit_multiple(odds) == american_to_decimal(odds) - 1.0


class TestValidation:
    @pytest.mark.parametrize("bad", [0, 50, -50, 99, -99, 1, -1])
    def test_rejects_the_impossible_range(self, bad):
        # No American price falls strictly between -100 and +100. A value in
        # that gap is a probability, a percentage, or a stake that reached an
        # odds argument by mistake - the same class of bad value the DB's
        # ck_*_odds_american_valid CHECK constraint catches.
        with pytest.raises(ValueError, match="not a valid American price"):
            validate_american(bad)

    @pytest.mark.parametrize("bad", [1.5, "150", None, -150.0])
    def test_rejects_non_int(self, bad):
        with pytest.raises(ValueError, match="must be an int"):
            validate_american(bad)

    def test_rejects_bool(self):
        # isinstance(True, int) is True in Python, so a stray boolean would
        # otherwise be read as the price +1.
        with pytest.raises(ValueError, match="must be an int"):
            validate_american(True)

    def test_accepts_and_returns_valid(self):
        assert validate_american(-150) == -150
        assert validate_american(100) == 100

    def test_conversions_reject_invalid_too(self):
        # Validation is not just on the explicit entry point - every public
        # conversion refuses a bad price rather than returning a plausible
        # but meaningless number.
        for fn in (american_to_decimal, american_to_implied_prob, profit_multiple):
            with pytest.raises(ValueError, match="not a valid American price"):
                fn(50)
