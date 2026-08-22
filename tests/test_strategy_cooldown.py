"""
Tests for per-symbol loss cooldown in RiskManager.

Phase 0 teardown (2026-07-24): the Crypto15mTrendStrategyV2 anti-limit-cycle
tests (N-tick confirmation, post-trade cooldown, widened thresholds, mean
reversion threshold) were removed with the deleted crypto strategies. The
RiskManager cooldown tests below remain.
"""

import unittest
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestRiskManagerCooldown(unittest.TestCase):
    """Test per-symbol loss cooldown in RiskManager."""

    def setUp(self):
        from src.core.risk_manager import RiskManager

        self.rm = RiskManager(starting_balance=200.0)

    def test_loss_cooldown_set_on_close(self):
        """When a position closes with a loss, cooldown should be set for that exact ticker."""
        self.rm.record_execution(
            cost=5.0,
            symbol="KXBTC15M-26FEB151330-30",
            side="buy",
            quantity=10,
            price=0.50,
            stop_loss=0.45,
        )

        pos = {"symbol": "KXBTC15M-26FEB151330-30", "pnl": -2.0}
        self.rm._on_trade_close(pos)
        # Sprint 6: cooldown uses exact ticker, not series prefix
        self.assertIn("KXBTC15M-26FEB151330-30", self.rm.loss_cooldown)

    def test_no_cooldown_on_profitable_close(self):
        """No cooldown should be set for winning trades."""
        self.rm.record_execution(
            cost=5.0,
            symbol="KXBTC15M-26FEB151330-30",
            side="buy",
            quantity=10,
            price=0.50,
            stop_loss=0.45,
        )

        pos = {"symbol": "KXBTC15M-26FEB151330-30", "pnl": 3.0}
        self.rm._on_trade_close(pos)
        self.assertNotIn("KXBTC15M", self.rm.loss_cooldown)

    def test_rate_limit_increased(self):
        """Verify MIN_TRADE_INTERVAL_SEC is set to 10s."""
        self.assertEqual(self.rm.MIN_TRADE_INTERVAL_SEC, 10)


if __name__ == "__main__":
    unittest.main()
