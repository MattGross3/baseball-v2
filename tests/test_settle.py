"""Settlement arithmetic. Pure, no database.

`payout` means TOTAL RETURNED including the stake - the number the book
credits back - so P&L is always `payout - stake` regardless of status.
"""
from __future__ import annotations

import pytest

from betting.settle import payout_cents, profit_cents
from database.enums import BetStatus


class TestWinningPayouts:
    def test_favorite(self):
        # $50 at -150 risks 50 to win 33.33 -> 83.33 back.
        # 5000 * (1 + 100/150) = 8333.33 -> 8333 at HALF_UP.
        assert payout_cents(5000, -150, BetStatus.WON) == 8333

    def test_underdog(self):
        # $50 at +130 -> 50 + 65 = 115.00
        assert payout_cents(5000, 130, BetStatus.WON) == 11500

    def test_even_money(self):
        assert payout_cents(5000, 100, BetStatus.WON) == 10000
        assert payout_cents(5000, -100, BetStatus.WON) == 10000

    def test_standard_juice(self):
        # $110 at -110 -> 110 + 100 = 210.00
        assert payout_cents(11000, -110, BetStatus.WON) == 21000

    def test_rounding_is_half_up_not_bankers(self):
        # 1 cent at +150 -> 1 * 2.5 = 2.5 -> 3, not 2.
        # Python's round() would give 2 here (banker's rounding).
        assert payout_cents(1, 150, BetStatus.WON) == 3

    def test_no_float_drift_on_repeated_settlement(self):
        # 0.10 is not representable in binary floating point, so a float
        # implementation accumulates error across many bets.
        total = sum(payout_cents(10, 130, BetStatus.WON) for _ in range(1000))
        assert total == 23000


class TestNonWinningPayouts:
    def test_loss_returns_nothing(self):
        assert payout_cents(5000, -150, BetStatus.LOST) == 0

    def test_push_returns_the_stake(self):
        assert payout_cents(5000, -150, BetStatus.PUSH) == 5000

    def test_void_returns_the_stake(self):
        assert payout_cents(5000, 130, BetStatus.VOID) == 5000

    def test_push_is_odds_independent(self):
        for odds in (-2000, -150, 100, 1200):
            assert payout_cents(5000, odds, BetStatus.PUSH) == 5000


class TestProfit:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (BetStatus.WON, 3333),
            (BetStatus.LOST, -5000),
            (BetStatus.PUSH, 0),
            (BetStatus.VOID, 0),
        ],
    )
    def test_profit_from_payout(self, status, expected):
        assert profit_cents(5000, payout_cents(5000, -150, status)) == expected


class TestValidation:
    def test_open_bet_has_no_payout(self):
        # Returning 0 here would be indistinguishable from a loss.
        with pytest.raises(ValueError, match="open bet has no payout"):
            payout_cents(5000, -150, BetStatus.OPEN)

    def test_rejects_nonpositive_stake(self):
        for stake in (0, -100):
            with pytest.raises(ValueError, match="stake must be positive"):
                payout_cents(stake, -150, BetStatus.WON)

    def test_rejects_invalid_odds(self):
        with pytest.raises(ValueError, match="not a valid American price"):
            payout_cents(5000, 50, BetStatus.WON)

    def test_accepts_status_as_string(self):
        assert payout_cents(5000, 130, "won") == 11500

    def test_rejects_unknown_status(self):
        with pytest.raises(ValueError):
            payout_cents(5000, 130, "cashed_out")
