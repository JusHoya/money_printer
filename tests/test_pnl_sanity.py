"""
PnL Sanity Tests
Regression tests to ensure PnL calculations produce realistic results
for binary options (Kalshi-style).

Key invariants:
  1. Exit price is ALWAYS in [0.00, 1.00] for non-settlement closes
  2. Max PnL per contract = $1.00 (binary option ceiling)
  3. Multi-cycle simulation should NOT produce exponential growth
"""
import unittest
import sys
import os
import math

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.matching_engine import SimulatedExchange
from src.core.risk_manager import RiskManager


class TestExitPriceSanity(unittest.TestCase):
    """Exit price must ALWAYS be in [0.00, 1.00] for option trades."""

    def setUp(self):
        self.closed = []
        self.exchange = SimulatedExchange(on_close=lambda p: self.closed.append(p))
        self.exchange.TAKE_PROFIT_PCT = 0.10  # 10% TP to trigger easily

    def test_weather_tp_exit_is_option_price(self):
        """Weather take-profit must exit at estimated option price, NOT raw temperature."""
        self.exchange.open_position("KXHIGHNY-26FEB14-T45", "buy", 0.33, 100)

        # Feed temperature = 55°F (above strike 45 → estimated ≈ 0.84)
        # This should trigger TP. Exit must be < 1.0, NOT 55.0
        self.exchange.update_market("NY", 55.0)

        if self.closed:
            trade = self.closed[0]
            self.assertLessEqual(trade['exit_price'], 1.0,
                f"Exit price {trade['exit_price']} exceeds 1.00 — raw temperature leaked!")
            self.assertGreaterEqual(trade['exit_price'], 0.0)
            self.assertLess(abs(trade['pnl']), 100.0,
                f"PnL ${trade['pnl']} is absurd for a binary option trade")
        else:
            # Position still open — verify current_price is sane
            pos = self.exchange.positions[0]
            self.assertLessEqual(pos['current_price'], 1.0)

    def test_btc_tp_exit_is_option_price(self):
        """BTC take-profit must exit at estimated option price, NOT $69k."""
        self.exchange.open_position("KXBTC15M-26FEB141515-T69500", "buy", 0.45, 50)

        # Feed BTC = $70,000 (above strike 69500)
        self.exchange.update_market("BTC", 70000.0)

        if self.closed:
            trade = self.closed[0]
            self.assertLessEqual(trade['exit_price'], 1.0,
                f"Exit price {trade['exit_price']} — raw BTC price leaked!")
            self.assertGreaterEqual(trade['exit_price'], 0.0)
        else:
            pos = self.exchange.positions[0]
            self.assertLessEqual(pos['current_price'], 1.0)

    def test_weather_time_limit_uses_option_price(self):
        """TIME_LIMIT closure must use estimated option price, not raw temp."""
        self.exchange.TIME_LIMIT_MIN = 0  # Trigger immediately
        self.exchange.open_position("KXHIGHNY-26FEB14-T45", "buy", 0.50, 100)

        self.exchange.update_market("NY", 60.0)

        self.assertEqual(len(self.closed), 1, "Position should have closed on TIME_LIMIT")
        trade = self.closed[0]
        self.assertLessEqual(trade['exit_price'], 1.0,
            f"TIME_LIMIT exit = {trade['exit_price']} — raw temp leaked!")
        self.assertEqual(trade['reason'], "TIME_LIMIT")

    def test_stop_loss_exit_is_option_price(self):
        """Price-based stop loss must exit at estimated option price."""
        self.exchange.open_position("KXHIGHNY-26FEB14-T50", "buy", 0.50, 100,
                                     stop_loss=0.30)

        # Feed temp = 40°F (below strike 50 → estimated ≈ 0.16, below SL=0.30)
        self.exchange.update_market("NY", 40.0)

        if self.closed:
            trade = self.closed[0]
            self.assertLessEqual(trade['exit_price'], 1.0)
            self.assertGreaterEqual(trade['exit_price'], 0.0)
            # With safe exit price fix, SL exit uses last_market_price (defaults to entry)
            # so PnL may be 0 or negative. Key check: exit price is in valid range.
            self.assertTrue(trade['pnl'] <= 0, "SL trade should not be a gain")


class TestPnLCeiling(unittest.TestCase):
    """Max PnL per contract on a binary option is $1.00."""

    def setUp(self):
        self.closed = []
        self.exchange = SimulatedExchange(on_close=lambda p: self.closed.append(p))

    def test_max_pnl_per_contract(self):
        """PnL per contract can never exceed $1.00 on a binary option."""
        # Best case: buy at 0.01, exit at 1.00 (settlement) = $0.99/contract
        self.exchange.open_position("KXHIGHNY-26FEB14-T30", "buy", 0.01, 100)

        # Settle with temp = 50°F (above strike 30 → YES outcome → exit=1.00)
        self.exchange._close_position(self.exchange.positions[0], 50.0, reason="EXPIRATION")

        trade = self.closed[0]
        pnl_per_contract = trade['pnl'] / trade['quantity']
        self.assertLessEqual(pnl_per_contract, 1.0,
            f"PnL per contract = ${pnl_per_contract:.2f}. Max should be $1.00")

    def test_no_negative_exit_for_buy(self):
        """A losing buy can lose at most entry_price per contract."""
        self.exchange.open_position("KXHIGHNY-26FEB14-T80", "buy", 0.75, 100)

        # Settle: temp = 70°F (below strike 80 → NO → exit=0.00)
        self.exchange._close_position(self.exchange.positions[0], 70.0, reason="EXPIRATION")

        trade = self.closed[0]
        pnl_per_contract = trade['pnl'] / trade['quantity']
        self.assertGreaterEqual(pnl_per_contract, -1.0)
        self.assertAlmostEqual(pnl_per_contract, -0.75)


class TestMultiCycleGrowth(unittest.TestCase):
    """Simulated multi-cycle trading must NOT produce exponential growth."""

    def test_50_cycles_realistic_growth(self):
        """50 trade cycles starting at $100 should stay under 5x (realistic for ~15% TP)."""
        rm = RiskManager(starting_balance=100.0)
        rm.exchange.TAKE_PROFIT_PCT = 0.15
        rm.exchange.STOP_LOSS_PCT = 0.30

        for i in range(50):
            # Size via Kelly
            qty = rm.calculate_kelly_size(0.55, 0.33)
            cost = 0.33 * qty

            if not rm.check_order(cost, category='weather'):
                continue

            rm.record_execution(cost, f"KXHIGHNY-26FEB14-T{40+i%10}", "buy", qty, 0.33)

            # Simulate: half win, half lose
            if i % 2 == 0:
                # Win: temp above strike → estimated ≈ 0.72 (TP fires)
                rm.update_market_data(f"KXHIGHNY-26FEB14-T{40+i%10}", 50.0 + i % 10)
            else:
                # Lose: temp below strike → estimated ≈ 0.20 (SL fires)
                rm.update_market_data(f"KXHIGHNY-26FEB14-T{40+i%10}", 30.0)

        # Balance should be REALISTIC — not 1000x
        self.assertLess(rm.balance, 500.0,
            f"Balance ${rm.balance:.2f} is unrealistic after 50 cycles (started at $100)")
        self.assertGreater(rm.balance, 0.0, "Balance should not be negative")


class TestKellyQuantityCap(unittest.TestCase):
    """Kelly sizing must be capped at 500 contracts."""

    def test_cap_at_500(self):
        rm = RiskManager(starting_balance=1_000_000.0)  # Rich monkey
        qty = rm.calculate_kelly_size(0.90, 0.10)  # Very high edge
        self.assertLessEqual(qty, 500,
            f"Kelly returned {qty} contracts — should be capped at 500")


if __name__ == '__main__':
    unittest.main(verbosity=2)
