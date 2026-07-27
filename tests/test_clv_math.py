"""Pure CLV arithmetic. No database.

Expected values are derived independently (50-digit Decimal bisection run
outside this project) and quoted below, not read off the implementation.
"""
from __future__ import annotations

import datetime as dt

import pytest

from betting.clv import ClvResult, compute_clv, opposite_selection
from betting.odds import american_to_implied_prob

AT = dt.datetime(2026, 7, 27, 22, 55, tzinfo=dt.timezone.utc)

# REF: closing market -160 (home) / +140 (away).
#   raw       0.615384615 / 0.416666667   sum 1.032051282
#   k         1.049137134
#   power     0.600877448 / 0.399122552
#   multipl.  0.596273292 / 0.403726708   <- the forbidden method
REF_FAIR_AWAY_POWER = 0.399122552
REF_FAIR_AWAY_MULTIPLICATIVE = 0.403726708
REF_CLV_POINTS_POWER = -3.566006
REF_CLV_POINTS_MULTIPLICATIVE = -3.105590


def clv(bet: int, close: int, opposing: int | None = None) -> ClvResult:
    return compute_clv(
        bet_odds_american=bet,
        closing_odds_american=close,
        closing_captured_at=AT,
        opposing_closing_odds_american=opposing,
    )


class TestPriceBasedClv:
    def test_positive_when_the_line_shortens(self):
        # Bet +130 (decimal 2.30), closed +110 (decimal 2.10). You hold a
        # price that pays more than the close - the market came to you.
        result = clv(130, 110)
        assert result.clv_pct == pytest.approx(9.523810, abs=1e-6)
        assert result.beat_close is True

    def test_negative_when_the_line_lengthens(self):
        # The mirror image: 2.10 held against a 2.30 close.
        result = clv(110, 130)
        assert result.clv_pct == pytest.approx(-8.695652, abs=1e-6)
        assert result.beat_close is False

    def test_zero_when_unchanged(self):
        result = clv(130, 130)
        assert result.clv_pct == 0.0
        # Strictly greater: matching the close is not beating it.
        assert result.beat_close is False

    def test_favorite_side_sign_convention(self):
        # -150 -> -140 is the line shortening against you (you took the
        # worse number), so CLV is negative even though -140 > -150.
        assert clv(-150, -140).clv_pct < 0
        assert clv(-140, -150).clv_pct > 0

    def test_is_the_ratio_of_decimal_odds(self):
        # 2.30 / 2.10 - 1
        assert clv(130, 110).clv_pct == pytest.approx((2.30 / 2.10 - 1) * 100, abs=1e-12)


class TestDevigggedClv:
    def test_prob_points_hand_computed(self):
        # Bet away +130: break-even 100/230 = 0.434783.
        # Close -160/+140 devigs (power) to a fair away probability of
        # 0.399123 - below the break-even, so the bet was -EV against the
        # market's closing opinion.
        result = clv(130, 140, opposing=-160)

        assert result.bet_breakeven_prob == pytest.approx(100 / 230, abs=1e-12)
        assert result.fair_closing_prob == pytest.approx(REF_FAIR_AWAY_POWER, abs=1e-6)
        assert result.clv_prob_points == pytest.approx(REF_CLV_POINTS_POWER, abs=1e-4)

    def test_both_metrics_agree_on_this_market(self):
        # Computed by completely different routes - one a ratio of decimal
        # prices, one a devigged probability - so agreement here is evidence,
        # not a tautology.
        result = clv(130, 140, opposing=-160)
        assert result.clv_pct == pytest.approx(-4.166667, abs=1e-6)
        assert result.clv_prob_points is not None
        assert (result.clv_pct < 0) == (result.clv_prob_points < 0)

    @pytest.mark.parametrize(
        "bet,close,opposing",
        [
            (130, 110, -130),
            (130, 140, -160),
            (-150, -140, 120),
            (-150, -170, 145),
            (200, 180, -220),
            (-110, -105, -105),
            (300, 250, -300),
        ],
    )
    def test_devigged_positive_implies_price_positive(self, bet, close, opposing):
        # The real invariant, and it runs one way only. For an OVERROUND
        # book, devigging lowers every probability, so the fair closing
        # probability sits below the raw one; a break-even beneath the fair
        # number is necessarily beneath the raw number too. The converse
        # fails whenever your improvement over the close is smaller than the
        # book's margin - see test_price_clv_can_be_positive_while_devigged_
        # is_negative.
        #
        # The overround guard is load-bearing, not defensive noise: an
        # underround close solves to k < 1, devigging RAISES every
        # probability, and the implication reverses. See
        # test_invariant_reverses_for_an_underround_close.
        result = clv(bet, close, opposing=opposing)
        assert result.clv_prob_points is not None
        assert result.closing_overround is not None
        if result.closing_overround > 0 and result.clv_prob_points > 0:
            assert result.clv_pct > 0

    def test_invariant_reverses_for_an_underround_close(self):
        """An arbitrage at the close inverts the relationship.

        +105/+105 sums to 0.9756, so k < 1 and devigging raises both sides
        instead of lowering them. The fair closing probability then sits
        ABOVE the raw one, and `clv_prob_points > 0` no longer implies
        `clv_pct > 0`. Rare, and usually a sign the two prices came from
        different books or instants - but representable, so the invariant
        has to be stated conditionally rather than absolutely.
        """
        # Bet at -110 (break-even 0.5238) into a close of +105/+105.
        result = clv(-110, 105, opposing=105)

        assert result.closing_overround is not None
        assert result.closing_overround < 0  # underround: an arb
        assert result.fair_closing_prob == pytest.approx(0.5, abs=1e-9)
        # Fair (0.5) is ABOVE raw (0.4878) - the reverse of the normal case.
        assert result.fair_closing_prob > american_to_implied_prob(105)

        # Price CLV is negative: 1.91 held against a 2.05 close.
        assert result.clv_pct < 0
        # Devigged is also negative here, but only because the break-even is
        # high; the point is that the proof of the implication does not hold
        # in this regime, so nothing may rely on it.
        assert result.clv_prob_points == pytest.approx(
            (0.5 - american_to_implied_prob(-110)) * 100, abs=1e-6
        )

    def test_overround_is_reported(self):
        result = clv(130, 140, opposing=-160)
        assert result.closing_overround == pytest.approx(0.032051282, abs=1e-9)

    def test_overround_is_none_without_the_opposing_price(self):
        assert clv(130, 140).closing_overround is None

    def test_price_clv_can_be_positive_while_devigged_is_negative(self):
        # +200 taken on a market closing +180/-220. You beat the posted
        # close by 20 cents, but the book's ~4.5% overround is wider than
        # that, so against closing FAIR value the bet is still slightly bad.
        # The two metrics answer different questions and this is the case
        # that separates them - the stricter one is the one to trust.
        result = clv(200, 180, opposing=-220)

        assert result.clv_pct == pytest.approx(7.142857, abs=1e-6)
        assert result.beat_close is True
        assert result.bet_breakeven_prob == pytest.approx(1 / 3, abs=1e-12)
        assert result.fair_closing_prob == pytest.approx(0.331149, abs=1e-6)
        assert result.clv_prob_points == pytest.approx(-0.218450, abs=1e-5)

    def test_fair_closing_prob_is_below_raw_closing_prob(self):
        # The mechanism behind the one-way implication, asserted directly.
        # All three closes here are overround; see
        # test_invariant_reverses_for_an_underround_close for the other
        # regime, where this relation flips.
        for close, opposing in [(140, -160), (180, -220), (-105, -105)]:
            result = clv(130, close, opposing=opposing)
            assert result.fair_closing_prob is not None
            assert result.fair_closing_prob < american_to_implied_prob(close)

    def test_uses_power_method_not_multiplicative(self):
        # Regression test for the constraint. Multiplicative devigging of the
        # same closing market would give a fair away probability of 0.403727
        # and thus -3.1056 points - a CLV reading 0.46 points kinder than
        # the truth, every time, in the direction that flatters the bettor.
        result = clv(130, 140, opposing=-160)

        assert result.fair_closing_prob == pytest.approx(REF_FAIR_AWAY_POWER, abs=1e-6)
        assert result.fair_closing_prob != pytest.approx(
            REF_FAIR_AWAY_MULTIPLICATIVE, abs=1e-4
        )
        assert result.clv_prob_points == pytest.approx(REF_CLV_POINTS_POWER, abs=1e-4)
        assert result.clv_prob_points != pytest.approx(
            REF_CLV_POINTS_MULTIPLICATIVE, abs=1e-3
        )

    def test_none_without_the_opposing_price(self):
        # A single price cannot be devigged - the margin is only visible
        # across the whole market. Returning a number here would mean
        # inventing one.
        result = clv(130, 140)
        assert result.clv_prob_points is None
        assert result.fair_closing_prob is None
        # The price-based metric is unaffected and still reported.
        assert result.clv_pct == pytest.approx(-4.166667, abs=1e-6)

    def test_breakeven_is_the_raw_price_not_devigged(self):
        # Your break-even is a property of the price you actually took, vig
        # included - you have to win that often to profit. Devigging it
        # would compare a fair number against a fair number and describe a
        # bet nobody made.
        result = clv(-150, -150, opposing=130)
        assert result.bet_breakeven_prob == pytest.approx(0.6, abs=1e-12)


class TestValidation:
    @pytest.mark.parametrize("bad", [0, 50, -99])
    def test_rejects_invalid_bet_odds(self, bad):
        with pytest.raises(ValueError):
            clv(bad, 130)

    @pytest.mark.parametrize("bad", [0, 50, -99])
    def test_rejects_invalid_closing_odds(self, bad):
        with pytest.raises(ValueError):
            clv(130, bad)

    def test_rejects_invalid_opposing_odds(self):
        with pytest.raises(ValueError):
            clv(130, 140, opposing=50)


class TestPurity:
    def test_takes_no_session(self):
        import inspect

        params = inspect.signature(compute_clv).parameters
        assert "session" not in params
        assert "db" not in params

    def test_is_deterministic(self):
        first = clv(130, 140, opposing=-160)
        second = clv(130, 140, opposing=-160)
        assert first == second

    def test_result_is_immutable(self):
        with pytest.raises(AttributeError):
            clv(130, 140).clv_pct = 0.0  # type: ignore[misc]


class TestOppositeSelection:
    @pytest.mark.parametrize(
        "market,selection,expected",
        [
            ("moneyline", "home", "away"),
            ("moneyline", "away", "home"),
            ("run_line", "home", "away"),
            ("total", "over", "under"),
            ("total", "under", "over"),
        ],
    )
    def test_pairs(self, market, selection, expected):
        assert opposite_selection(market, selection) == expected

    @pytest.mark.parametrize(
        "market,selection",
        [("total", "home"), ("moneyline", "over"), ("run_line", "under")],
    )
    def test_rejects_selection_not_in_market(self, market, selection):
        # Guards against a totals bet resolving its "opposite" against a
        # home/away price that market never quoted.
        with pytest.raises(ValueError, match="not a selection in"):
            opposite_selection(market, selection)
