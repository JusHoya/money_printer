"""
Comprehensive PnL Flow Tests
Tests the entire financial data flow from position open to dashboard display.
"""

import unittest
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.risk_manager import RiskManager
from src.core.matching_engine import SimulatedExchange


class TestPnLFlow(unittest.TestCase):
    """Tests covering the complete PnL lifecycle."""

    def setUp(self):
        self.rm = RiskManager(starting_balance=100.0)
        # Disable auto-stops for predictable testing
        self.rm.exchange.TAKE_PROFIT_PCT = 100.0
        self.rm.exchange.STOP_LOSS_PCT = 100.0
        self.rm.exchange.TIME_LIMIT_MIN = 9999

    # === TEST 1: Basic Position Opening ===
    def test_open_position_reduces_balance(self):
        """Opening a position should reduce available balance by cost."""
        print("\n--- Test: Open Position Reduces Balance ---")

        initial_balance = self.rm.balance
        # risk_manager hard-caps entries at MAX_CONTRACTS=50, so 50 qty @ 0.50 = $25 cost
        cost = 25.0  # 50 qty @ 0.50

        self.rm.record_execution(cost, "KXBTC-TEST-50000", "buy", 50, 0.50)

        self.assertEqual(self.rm.balance, initial_balance - cost)
        self.assertEqual(len(self.rm.exchange.positions), 1)
        print(f"✅ Balance reduced from ${initial_balance} to ${self.rm.balance}")

    # === TEST 2: Unrealized PnL Updates ===
    def test_unrealized_pnl_updates_on_price_move(self):
        """Unrealized PnL should update when market price changes."""
        print("\n--- Test: Unrealized PnL Updates ---")

        # Open position: BUY 100 @ 0.50 on a $50000 strike
        self.rm.record_execution(50.0, "KXBTC-TEST-50000", "buy", 100, 0.50)
        initial_unrealized = self.rm.unrealized_pnl

        # Price moves up (spot > strike by $5000)
        # With tanh formula: diff=5000, scale=1000, norm=5, tanh(5)≈0.9999
        # probability_shift = 0.9999 * 0.49 ≈ 0.49, estimated = 0.50 + 0.49 = 0.99
        self.rm.update_market_data("BTC", 55000.0)

        # Unrealized should be positive
        self.assertGreater(self.rm.unrealized_pnl, initial_unrealized)
        print(f"✅ Unrealized PnL updated: ${self.rm.unrealized_pnl:.2f}")

    # === TEST 3: Realized PnL on Close ===
    def test_realized_pnl_on_position_close(self):
        """Closing a position should move PnL from unrealized to realized."""
        print("\n--- Test: Realized PnL on Close ---")

        # Open position
        self.rm.record_execution(50.0, "KXBTC-TEST-50000", "buy", 100, 0.50)
        pos = self.rm.exchange.positions[0]

        # Close with profit (spot > strike) — use EXPIRATION for binary settlement
        self.rm.exchange._close_position(pos, 55000.0, reason="EXPIRATION")

        # Sync stats
        self.rm.update_market_data("BTC", 55000.0)

        stats = self.rm.exchange.get_stats()
        self.assertGreater(stats["realized"], 0)
        self.assertEqual(stats["open_count"], 0)
        print(
            f"✅ Realized PnL: ${stats['realized']:.2f}, Open positions: {stats['open_count']}"
        )

    # === TEST 4: Balance Recovery After Profitable Close ===
    def test_balance_increases_on_profitable_close(self):
        """Balance should increase when a profitable trade closes."""
        print("\n--- Test: Balance Increases on Profit ---")

        # Open position. risk_manager caps entries at MAX_CONTRACTS=50, so
        # 50 qty @ 0.50 = $25 cost → balance 75.
        self.rm.record_execution(25.0, "KXBTC-TEST-50000", "buy", 50, 0.50)
        self.assertEqual(self.rm.balance, 75.0)

        # Close with WIN (exit @ 1.00)
        pos = self.rm.exchange.positions[0]
        self.rm.exchange._close_position(
            pos, 60000.0, reason="EXPIRATION"
        )  # Binary settles to 1.00
        self.rm.update_market_data("BTC", 60000.0)

        # PnL = (1.00 - 0.50) * 50 = $25, minus entry fee
        # Balance should be: 100 + 25 - fee ≈ 125 - fee
        fees = self.rm.exchange.total_fees_paid
        self.assertAlmostEqual(self.rm.balance, 125.0 - fees, places=2)
        print(f"✅ Balance after win: ${self.rm.balance:.2f} (fees: ${fees:.2f})")

    # === TEST 5: Balance Decrease After Loss ===
    def test_balance_decreases_on_loss(self):
        """Balance should decrease when a losing trade closes."""
        print("\n--- Test: Balance Decreases on Loss ---")

        # Open position. risk_manager caps entries at MAX_CONTRACTS=50, so
        # 50 qty @ 0.50 = $25 cost.
        self.rm.record_execution(25.0, "KXBTC-TEST-50000", "buy", 50, 0.50)

        # Close with LOSS (exit @ 0.00 - binary settles NO)
        pos = self.rm.exchange.positions[0]
        self.rm.exchange._close_position(
            pos, 45000.0, reason="EXPIRATION"
        )  # Spot < Strike
        self.rm.update_market_data("BTC", 45000.0)

        # PnL = (0.00 - 0.50) * 50 = -$25, minus entry fee
        # Balance should be: 100 - 25 - fee ≈ 75 - fee
        fees = self.rm.exchange.total_fees_paid
        self.assertAlmostEqual(self.rm.balance, 75.0 - fees, places=2)
        print(f"✅ Balance after loss: ${self.rm.balance:.2f} (fees: ${fees:.2f})")

    # === TEST 6: Equity Calculation ===
    def test_equity_calculation(self):
        """Equity = Cash + Exposure (unrealized is now folded into cash by _sync_balance)."""
        print("\n--- Test: Equity Calculation ---")

        # Open position
        self.rm.record_execution(50.0, "KXBTC-TEST-50000", "buy", 100, 0.50)

        # Update market (profitable)
        self.rm.update_market_data("BTC", 55000.0)

        cash = self.rm.balance
        exposure = self.rm.get_current_exposure()
        unrealized = self.rm.unrealized_pnl

        # Dashboard/state_manager formula: equity = cash + exposure
        # (cash already includes unrealized via _sync_balance)
        equity = cash + exposure

        # Exposure = 0.50 * 100 = 50
        # Cash = starting(100) + realized(0) + unrealized(~49) - exposure(50) = ~99
        # Equity = ~99 + 50 = ~149

        print(f"  Cash: ${cash:.2f}")
        print(f"  Exposure: ${exposure:.2f}")
        print(f"  Unrealized: ${unrealized:.2f}")
        print(f"  Equity: ${equity:.2f}")

        # Equity should be close to starting balance + unrealized gains
        self.assertGreater(equity, 100.0)
        print("✅ Equity correctly calculated")

    # === TEST 7: Tanh Price Formula Behavior ===
    def test_weather_mark_is_observed_price_never_tanh(self):
        """REWRITTEN 2026-07-25 (PRD FR-1.2 / Phase 1 B3).

        This test used to assert the tanh estimator's temperature scaling on a
        KXHIGH position: 0.50 at the strike, ~0.73 five degrees above, >0.90
        twenty degrees above. Those marks were manufactured — a daily-high
        bracket's value is P(YES) over a band, which a signed distance to one
        number cannot express, and the ``less``/``between`` types are not even
        monotone in that distance. Weather is no longer tanh-marked at all: the
        mark is the observed Kalshi price, or entry when there is none. The
        tanh path itself is unchanged and still covered for crypto by
        ``test_tanh_price_formula_btc``.
        """
        print("\n--- Test: Weather Mark (observed price, never tanh) ---")

        exchange = SimulatedExchange()
        exchange.TAKE_PROFIT_PCT = 100.0
        exchange.STOP_LOSS_PCT = 100.0

        exchange.open_position(
            "KXHIGHNY-TEST-75",
            "buy",
            0.50,
            100,
            strike_type="between",
            floor_strike=75,
            cap_strike=76,
        )
        pos = exchange.positions[0]

        # No observed price: the mark holds at entry no matter the temperature.
        for temp in (75.0, 80.0, 95.0):
            exchange.update_market("TEMP_KNYC", temp)
            self.assertAlmostEqual(pos["current_price"], 0.50, places=6)
        print(f"  No observed price, 75-95°F: {pos['current_price']:.4f}")

        # An observed Kalshi price becomes the mark verbatim.
        exchange.update_market_price("KXHIGHNY-TEST-75", 0.81)
        exchange.update_market("TEMP_KNYC", 95.0)
        self.assertAlmostEqual(pos["current_price"], 0.81, places=6)
        print(f"  Observed $0.81: {pos['current_price']:.4f}")

        print("✅ Weather marks track the market, not a synthetic curve")


class TestExposureCalculation(unittest.TestCase):
    """Tests for exposure tracking."""

    def setUp(self):
        self.rm = RiskManager(starting_balance=100.0)
        self.rm.exchange.TAKE_PROFIT_PCT = 100.0
        self.rm.exchange.STOP_LOSS_PCT = 100.0

    def test_exposure_sums_position_costs(self):
        """Exposure should equal sum of (entry_price × quantity)."""
        print("\n--- Test: Exposure Calculation ---")

        # Open two positions. The first requests 100 qty but risk_manager caps
        # entries at MAX_CONTRACTS=50, so it is recorded as 50 qty.
        self.rm.record_execution(20.0, "KXBTC-TEST-50000", "buy", 100, 0.20)
        self.rm.record_execution(15.0, "KXHIGH-TEST-75", "buy", 50, 0.30)

        exposure = self.rm.get_current_exposure()

        # Expected: (0.20 * 50 capped) + (0.30 * 50) = 10 + 15 = 25
        self.assertEqual(exposure, 25.0)
        print(f"✅ Exposure correctly calculated: ${exposure:.2f}")

    def test_exposure_by_category(self):
        """Exposure filtering by category should work."""
        print("\n--- Test: Exposure by Category ---")

        # First entry requests 100 qty but risk_manager caps at MAX_CONTRACTS=50.
        self.rm.record_execution(20.0, "KXBTC-TEST-50000", "buy", 100, 0.20)
        self.rm.record_execution(15.0, "KXHIGH-TEST-75", "buy", 50, 0.30)

        crypto_exp = self.rm.get_current_exposure(category="crypto")
        weather_exp = self.rm.get_current_exposure(category="weather")

        # Crypto: 0.20 * 50 (capped) = 10; Weather: 0.30 * 50 = 15
        self.assertEqual(crypto_exp, 10.0)
        self.assertEqual(weather_exp, 15.0)
        print(f"✅ Crypto: ${crypto_exp:.2f}, Weather: ${weather_exp:.2f}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
