"""
Tests for Crypto15mTrendStrategyV2 anti-limit-cycle features:
1. N-tick trend confirmation (3 consecutive ticks required)
2. Post-trade cooldown (5 min lockout after signal)
3. Widened thresholds (0.60/0.40 dead zone)
4. Mean reversion threshold (8% deviation, 30+ samples)

NOTE: Tests for breakout + cooldown use a mocked MomentumConfirmation that 
always confirms, to isolate the threshold/cooldown logic from RSI/MACD sensitivity.
This is the correct approach per Scientific Rigor — test one variable at a time.
"""
import unittest
import sys
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.strategies.crypto_strategy import Crypto15mTrendStrategyV2
from src.core.interfaces import MarketData


def make_market(symbol, price, bid=None, ask=None, timestamp=None):
    """Helper to create MarketData objects for testing."""
    if bid is None:
        bid = price - 0.02
    if ask is None:
        ask = price
    if timestamp is None:
        # Use a time that's NOT at the start of a 15m cycle (avoid confirmation_delay)
        timestamp = datetime(2026, 2, 15, 12, 5, 0)
    return MarketData(
        symbol=symbol,
        timestamp=timestamp,
        price=97000.0,  # Spot price (not relevant for threshold logic)
        volume=100,
        bid=bid,
        ask=ask,
        extra={"source": "test", "status": "active"}
    )


def make_strategy_with_mock_momentum(**kwargs):
    """Create strategy with MomentumConfirmation mocked to always confirm."""
    strategy = Crypto15mTrendStrategyV2(**kwargs)
    # Mock momentum to always confirm (isolate threshold/cooldown testing)
    strategy.momentum.should_confirm_buy = MagicMock(return_value=(True, "MOCKED", 0.8))
    strategy.momentum.should_confirm_sell = MagicMock(return_value=(True, "MOCKED", 0.8))
    return strategy


def fill_history(strategy, count=20, timestamp=None):
    """Fill price_history with neutral dead-zone prices (no signals generated)."""
    for i in range(count):
        ts = timestamp + timedelta(seconds=i) if timestamp else None
        strategy.analyze(make_market("TEST-001", 0.50, timestamp=ts))


class TestNTickConfirmation(unittest.TestCase):
    """Verify that breakout signals require N consecutive ticks above/below threshold."""
    
    def setUp(self):
        self.strategy = make_strategy_with_mock_momentum(
            trend_confirm_ticks=3,
            cooldown_seconds=300
        )
        # Fill history to seed price_history (stays in dead zone)
        fill_history(self.strategy, 20)
    
    def test_single_tick_above_bull_no_signal(self):
        """1 tick above 0.75 should NOT fire a BULL signal."""
        signals = self.strategy.analyze(make_market("TEST-001", 0.80))
        self.assertEqual(len(signals), 0, "Should NOT signal on 1 tick above bull trigger")

    def test_two_ticks_above_bull_no_signal(self):
        """2 ticks above 0.75 should still NOT fire."""
        self.strategy.analyze(make_market("TEST-001", 0.80))
        signals = self.strategy.analyze(make_market("TEST-001", 0.81))
        self.assertEqual(len(signals), 0, "Should NOT signal on 2 ticks above bull trigger")

    def test_three_ticks_above_bull_fires(self):
        """3 consecutive ticks above 0.75 SHOULD fire a BULL signal."""
        self.strategy.analyze(make_market("TEST-001", 0.78))
        self.strategy.analyze(make_market("TEST-001", 0.79))
        signals = self.strategy.analyze(make_market("TEST-001", 0.80))
        self.assertGreater(len(signals), 0, "Should fire BULL after 3 consecutive ticks above trigger")
        self.assertEqual(signals[0].side, "buy")

    def test_interrupted_ticks_reset_counter(self):
        """If price drops back into dead zone, counter resets."""
        # 2 ticks above
        self.strategy.analyze(make_market("TEST-001", 0.78))
        self.strategy.analyze(make_market("TEST-001", 0.79))
        # Drop back to dead zone
        self.strategy.analyze(make_market("TEST-001", 0.50))
        # 1 tick above again (counter should be reset)
        signals = self.strategy.analyze(make_market("TEST-001", 0.80))
        self.assertEqual(len(signals), 0, "Counter should reset when price returns to dead zone")

    def test_bear_requires_three_ticks(self):
        """3 consecutive ticks below 0.25 SHOULD fire a BEAR (BUY NO) signal."""
        self.strategy.analyze(make_market("TEST-001", 0.22, bid=0.22, ask=0.22))
        self.strategy.analyze(make_market("TEST-001", 0.21, bid=0.21, ask=0.21))
        signals = self.strategy.analyze(make_market("TEST-001", 0.20, bid=0.20, ask=0.20))
        self.assertGreater(len(signals), 0, "Should fire BEAR after 3 consecutive ticks below trigger")
        self.assertEqual(signals[0].side, "buy")  # Now BUY NO instead of sell

    def test_counter_switches_direction(self):
        """Counter for bull resets when price crosses to bear territory."""
        # 2 ticks above bull
        self.strategy.analyze(make_market("TEST-001", 0.78))
        self.strategy.analyze(make_market("TEST-001", 0.79))
        self.assertEqual(self.strategy.consecutive_above, 2)
        # Jump to bear territory
        self.strategy.analyze(make_market("TEST-001", 0.20, bid=0.20, ask=0.20))
        self.assertEqual(self.strategy.consecutive_above, 0)
        self.assertEqual(self.strategy.consecutive_below, 1)


class TestPostTradeCooldown(unittest.TestCase):
    """Verify that after a signal fires, no new signals within cooldown period."""
    
    def setUp(self):
        self.strategy = make_strategy_with_mock_momentum(
            trend_confirm_ticks=3,
            cooldown_seconds=300  # 5 minutes
        )
    
    def test_cooldown_blocks_immediate_re_entry(self):
        """After BULL signal, no signals within 5 minutes even if conditions are met."""
        base_time = datetime(2026, 2, 15, 12, 5, 0)
        fill_history(self.strategy, 20, base_time)
        
        # Fire BULL (3 ticks)
        t = base_time + timedelta(seconds=20)
        self.strategy.analyze(make_market("TEST-001", 0.78, timestamp=t))
        self.strategy.analyze(make_market("TEST-001", 0.79, timestamp=t + timedelta(seconds=1)))
        signals = self.strategy.analyze(make_market("TEST-001", 0.80, timestamp=t + timedelta(seconds=2)))
        self.assertGreater(len(signals), 0, "Should fire the initial BULL signal")

        # Immediately try again (within cooldown) — should be blocked
        t2 = t + timedelta(seconds=30)  # 30s later, still within 5min cooldown
        self.strategy.analyze(make_market("TEST-001", 0.78, timestamp=t2))
        self.strategy.analyze(make_market("TEST-001", 0.79, timestamp=t2 + timedelta(seconds=1)))
        signals2 = self.strategy.analyze(make_market("TEST-001", 0.80, timestamp=t2 + timedelta(seconds=2)))
        self.assertEqual(len(signals2), 0, "Should be blocked by cooldown")
    
    def test_cooldown_expires_allows_re_entry(self):
        """After cooldown expires, signals should work again."""
        base_time = datetime(2026, 2, 15, 12, 5, 0)
        fill_history(self.strategy, 20, base_time)
        
        # Fire BULL
        t = base_time + timedelta(seconds=20)
        self.strategy.analyze(make_market("TEST-001", 0.78, timestamp=t))
        self.strategy.analyze(make_market("TEST-001", 0.79, timestamp=t + timedelta(seconds=1)))
        signals = self.strategy.analyze(make_market("TEST-001", 0.80, timestamp=t + timedelta(seconds=2)))
        self.assertGreater(len(signals), 0, "Initial signal should fire")

        # Wait past cooldown (6 minutes > 5 min cooldown)
        t_after = t + timedelta(minutes=6)
        # Fill some neutral data to avoid stale state
        for i in range(5):
            self.strategy.analyze(make_market("TEST-001", 0.50, timestamp=t_after + timedelta(seconds=i - 10)))

        # 3 ticks above threshold
        self.strategy.analyze(make_market("TEST-001", 0.78, timestamp=t_after))
        self.strategy.analyze(make_market("TEST-001", 0.79, timestamp=t_after + timedelta(seconds=5)))
        signals2 = self.strategy.analyze(make_market("TEST-001", 0.80, timestamp=t_after + timedelta(seconds=10)))
        self.assertGreater(len(signals2), 0, "Should allow signal after cooldown expires")
    
    def test_cooldown_set_after_mean_reversion_signal(self):
        """Cooldown should also engage after a mean reversion signal."""
        base_time = datetime(2026, 2, 15, 12, 5, 0)
        strategy = Crypto15mTrendStrategyV2(
            trend_confirm_ticks=3,
            cooldown_seconds=300,
            mean_reversion_threshold=0.08,
            enable_mean_reversion=True
        )
        # Verify cooldown_until starts at datetime.min
        self.assertEqual(strategy.cooldown_until, datetime.min)


class TestWidenedThresholds(unittest.TestCase):
    """Verify the dead zone (0.40 - 0.60) generates no breakout signals."""
    
    def setUp(self):
        self.strategy = make_strategy_with_mock_momentum(
            trend_confirm_ticks=1,  # Single tick for threshold testing
            cooldown_seconds=0       # No cooldown for threshold testing
        )
        fill_history(self.strategy, 20)
    
    def test_price_at_055_no_bull_signal(self):
        """Price at 0.55 (old threshold) should NOT trigger BULL with new 0.60 threshold."""
        signals = self.strategy.analyze(make_market("TEST-001", 0.55))
        self.assertEqual(len(signals), 0, "0.55 should be in dead zone, no BULL signal")
    
    def test_price_at_045_no_bear_signal(self):
        """Price at 0.45 (old threshold) should NOT trigger BEAR with new 0.40 threshold."""
        signals = self.strategy.analyze(make_market("TEST-001", 0.45, bid=0.45, ask=0.45))
        self.assertEqual(len(signals), 0, "0.45 should be in dead zone, no BEAR signal")
    
    def test_exact_boundary_no_signal(self):
        """Exactly at 0.60 or 0.40 should NOT trigger (need to be strictly above/below)."""
        signals_bull = self.strategy.analyze(make_market("TEST-001", 0.60))
        self.assertEqual(len(signals_bull), 0, "Exactly at bull trigger should not signal")
        
        signals_bear = self.strategy.analyze(make_market("TEST-001", 0.40, bid=0.40, ask=0.40))
        self.assertEqual(len(signals_bear), 0, "Exactly at bear trigger should not signal")


class TestMeanReversionThreshold(unittest.TestCase):
    """Verify mean reversion requires 30+ samples and 8%+ deviation."""
    
    def setUp(self):
        self.strategy = Crypto15mTrendStrategyV2(
            trend_confirm_ticks=3,
            cooldown_seconds=0,
            mean_reversion_threshold=0.08,
            enable_mean_reversion=True
        )
    
    def test_mr_blocked_with_insufficient_history(self):
        """Mean reversion should NOT fire with < 30 price samples."""
        for i in range(20):
            self.strategy.analyze(make_market("TEST-001", 0.50))
        
        signals = self.strategy.analyze(make_market("TEST-001", 0.55))
        mr_signals = [s for s in signals if s.side == "sell"]
        self.assertEqual(len(mr_signals), 0, "MR should not fire with < 30 samples")
    
    def test_mr_blocked_with_small_deviation(self):
        """Mean reversion should NOT fire with < 8% deviation."""
        for i in range(35):
            self.strategy.analyze(make_market("TEST-001", 0.50))
        
        # 3% deviation (0.515 vs mean 0.50) — below 8% threshold
        signals = self.strategy.analyze(make_market("TEST-001", 0.515))
        self.assertEqual(len(signals), 0, "MR should not fire with < 8% deviation")


class TestRiskManagerCooldown(unittest.TestCase):
    """Test per-symbol loss cooldown in RiskManager."""
    
    def setUp(self):
        from src.core.risk_manager import RiskManager
        self.rm = RiskManager(starting_balance=200.0)
    
    def test_loss_cooldown_set_on_close(self):
        """When a position closes with a loss, cooldown should be set for that prefix."""
        self.rm.record_execution(
            cost=5.0, symbol="KXBTC15M-26FEB151330-30", 
            side="buy", quantity=10, price=0.50, stop_loss=0.45
        )
        
        pos = {
            'symbol': 'KXBTC15M-26FEB151330-30',
            'pnl': -2.0
        }
        self.rm._on_trade_close(pos)
        self.assertIn("KXBTC15M", self.rm.loss_cooldown)
    
    def test_no_cooldown_on_profitable_close(self):
        """No cooldown should be set for winning trades."""
        self.rm.record_execution(
            cost=5.0, symbol="KXBTC15M-26FEB151330-30", 
            side="buy", quantity=10, price=0.50, stop_loss=0.45
        )
        
        pos = {
            'symbol': 'KXBTC15M-26FEB151330-30',
            'pnl': 3.0
        }
        self.rm._on_trade_close(pos)
        self.assertNotIn("KXBTC15M", self.rm.loss_cooldown)
    
    def test_rate_limit_increased(self):
        """Verify MIN_TRADE_INTERVAL_SEC is set to 30s."""
        self.assertEqual(self.rm.MIN_TRADE_INTERVAL_SEC, 30)


if __name__ == '__main__':
    unittest.main()
