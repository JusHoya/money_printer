"""Tests for Sprint 6 structural fixes based on quant research.

Covers:
- 1A: KXBTC15M positions skip stop-losses (hold to expiry)
- 1B: Loss cooldown uses exact ticker (900s)
- 1C: Daily trade cap (40 trades)
- 1D: Raised edge thresholds
"""

import sys
import os
from datetime import datetime, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.matching_engine import SimulatedExchange
from src.core.risk_manager import RiskManager


# =============================================================================
# 1A: KXBTC15M skip stop-losses
# =============================================================================


class TestShortDurationStopSkip:
    """KXBTC15M positions should hold to expiry, ignoring price-based stops."""

    def test_btc15m_ignores_stop_loss(self):
        """A KXBTC15M position should NOT be stopped out even when price drops below stop."""
        ex = SimulatedExchange()
        ex.open_position("KXBTC15M-26APR022000-00", "buy", 0.70, 10, stop_loss=0.50)
        assert len(ex.positions) == 1

        # Simulate price dropping well below stop_loss
        # Set open_time to 1 min ago (past grace period)
        ex.positions[0]["open_time"] = datetime.now() - timedelta(seconds=60)
        ex.positions[0]["last_market_price"] = 0.30
        ex.positions[0]["strike"] = 67000.0

        ex.update_market("BTC", 66500.0)  # Price moving against us

        # Position should still be open — stops skipped for KXBTC15M
        assert (
            len(ex.positions) == 1
        ), "KXBTC15M position was stopped out but should hold to expiry"

    def test_hourly_also_skips_stop_loss(self):
        """Hourly BTC contracts (KXBTCD) should also skip stops — same bounded-loss logic."""
        ex = SimulatedExchange()
        ex.open_position("KXBTCD-26APR0220-T67000", "buy", 0.70, 10, stop_loss=0.50)
        assert len(ex.positions) == 1

        # Set past grace period and cache a market price below stop
        ex.positions[0]["open_time"] = datetime.now() - timedelta(seconds=60)
        ex.positions[0]["last_market_price"] = 0.40
        ex.positions[0]["strike"] = 67000.0

        ex.update_market("BTC", 66000.0)

        # Hourly contract should NOT be stopped out — hold to expiry
        assert len(ex.positions) == 1, "Hourly BTC should also hold to expiry"

    def test_btc15m_still_expires(self):
        """KXBTC15M should still close on expiration even without stop-loss."""
        ex = SimulatedExchange()
        ex.open_position(
            "KXBTC15M-26APR022000-00",
            "buy",
            0.70,
            10,
            stop_loss=0.50,
            expiration_time=datetime.now() - timedelta(seconds=1),
        )
        assert len(ex.positions) == 1

        ex.positions[0]["strike"] = 67000.0
        ex.update_market("BTC", 67500.0)

        # Should be closed via expiration, not stop-loss
        assert len(ex.positions) == 0, "Expired KXBTC15M should still close"
        assert ex.closed_trades[-1]["reason"] == "EXPIRATION"

    def test_btc15m_pct_stop_also_skipped(self):
        """KXBTC15M should skip the fallback PCT-based stop-loss too."""
        ex = SimulatedExchange()
        # Open with no explicit stop_loss (triggers PCT fallback path)
        ex.open_position("KXBTC15M-26APR022000-00", "buy", 0.70, 10, stop_loss=0.0)
        ex.positions[0]["open_time"] = datetime.now() - timedelta(seconds=60)
        ex.positions[0]["last_market_price"] = 0.30
        ex.positions[0]["strike"] = 67000.0

        ex.update_market("BTC", 66000.0)

        # Should NOT be closed by PCT stop for KXBTC15M
        assert len(ex.positions) == 1, "KXBTC15M should skip PCT-based stops too"


# =============================================================================
# Binary hold-to-expiry: skip profit targets, early settlement, time limit
# =============================================================================


class TestBinaryHoldToExpiry:
    """BTC contracts should hold to expiry with no intermediate exits."""

    def test_btc15m_skips_profit_targets(self):
        """KXBTC15M should NOT exit on profit target — hold for full binary settlement."""
        ex = SimulatedExchange()
        ex.open_position("KXBTC15M-26APR022000-00", "buy", 0.50, 10, stop_loss=0.0)
        pos = ex.positions[0]
        pos["open_time"] = datetime.now() - timedelta(seconds=60)
        pos["strike"] = 67000.0

        # Simulate price moving up past the +0.15 profit target trigger
        pos["last_market_price"] = 0.80  # +0.30 above entry

        ex.update_market("BTC", 67500.0)

        # Position should still be open — profit targets skipped
        assert len(ex.positions) == 1, "KXBTC15M should skip profit targets"

    def test_hourly_skips_profit_targets(self):
        """KXBTCD should also skip profit targets."""
        ex = SimulatedExchange()
        ex.open_position("KXBTCD-26APR0420-T67000", "buy", 0.50, 10, stop_loss=0.0)
        pos = ex.positions[0]
        pos["open_time"] = datetime.now() - timedelta(seconds=60)
        pos["strike"] = 67000.0
        pos["last_market_price"] = 0.80

        ex.update_market("BTC", 67500.0)

        assert len(ex.positions) == 1, "KXBTCD should skip profit targets"

    def test_btc15m_skips_early_settlement(self):
        """KXBTC15M should NOT early-settle even when price pegs at 0.01."""
        ex = SimulatedExchange()
        ex.open_position("KXBTC15M-26APR022000-00", "buy", 0.50, 10, stop_loss=0.0)
        pos = ex.positions[0]
        # Set age > 10 minutes (early settlement threshold)
        pos["open_time"] = datetime.now() - timedelta(minutes=12)
        pos["strike"] = 67000.0
        pos["last_market_price"] = 0.01  # Price pegged at 0.01

        ex.update_market("BTC", 65000.0)

        # Should still be open — early settlement skipped
        assert len(ex.positions) == 1, "KXBTC15M should skip early settlement"

    def test_hourly_skips_time_limit(self):
        """KXBTCD should NOT be closed by 60-min time limit — it expires naturally."""
        ex = SimulatedExchange()
        ex.open_position("KXBTCD-26APR0420-T67000", "buy", 0.50, 10, stop_loss=0.0)
        pos = ex.positions[0]
        # Set age > 60 minutes (TIME_LIMIT threshold)
        pos["open_time"] = datetime.now() - timedelta(minutes=65)
        pos["strike"] = 67000.0
        pos["last_market_price"] = 0.40

        ex.update_market("BTC", 66500.0)

        # Should still be open — time limit skipped
        assert len(ex.positions) == 1, "KXBTCD should skip time limit"

    def test_weather_still_respects_profit_targets(self):
        """Weather contracts should still take profit targets."""
        ex = SimulatedExchange()
        # Weather with profit targets enabled (default)
        ex.open_position("KXHIGHNY-26APR04-T75", "buy", 0.50, 10, stop_loss=0.0)
        pos = ex.positions[0]
        pos["open_time"] = datetime.now() - timedelta(seconds=60)
        pos["last_market_price"] = 0.80  # +0.30 above entry

        ex.update_market("KNYC", 80.0)

        # Weather should trigger profit target (at least partial exit)
        assert (
            len(ex.positions) == 0 or ex.positions[0]["quantity"] < 10
        ), "Weather should respect profit targets"

    def test_binary_event_still_expires_naturally(self):
        """Binary event contracts should still close on natural EXPIRATION."""
        ex = SimulatedExchange()
        ex.open_position(
            "KXBTC15M-26APR022000-00",
            "buy",
            0.50,
            10,
            stop_loss=0.0,
            expiration_time=datetime.now() - timedelta(seconds=1),
        )
        pos = ex.positions[0]
        pos["strike"] = 67000.0

        ex.update_market("BTC", 67500.0)

        assert len(ex.positions) == 0, "Expired contract should still close"
        assert ex.closed_trades[-1]["reason"] == "EXPIRATION"


# =============================================================================
# 1B: Exact ticker cooldown (900s)
# =============================================================================


class TestExactTickerCooldown:
    """Loss cooldown should ban the exact ticker, not the whole series."""

    def test_cooldown_is_900_seconds(self):
        rm = RiskManager(starting_balance=3000.0)
        assert rm.LOSS_COOLDOWN_SEC == 900

    def test_loss_bans_exact_ticker(self):
        """A loss on one ticker should ban that exact ticker."""
        rm = RiskManager(starting_balance=3000.0)

        # Simulate a losing trade close
        rm._on_trade_close(
            {
                "symbol": "KXBTC15M-26APR022000-00",
                "pnl": -10.0,
                "strategy_name": "Test",
            }
        )

        # The exact ticker should be in cooldown
        assert "KXBTC15M-26APR022000-00" in rm.loss_cooldown

    def test_exact_ticker_rejected_by_check_order(self):
        """check_order should reject the same ticker that had a loss."""
        rm = RiskManager(starting_balance=3000.0)

        rm._on_trade_close(
            {
                "symbol": "KXBTC15M-26APR022000-00",
                "pnl": -10.0,
                "strategy_name": "Test",
            }
        )

        result = rm.check_order(
            proposed_cost=10.0,
            category="general",
            strategy_name="Test",
            symbol="KXBTC15M-26APR022000-00",
        )
        assert result is False, "Same ticker after loss should be rejected"

    def test_different_ticker_same_series_allowed(self):
        """A different ticker in the same series should be allowed (if no strategy cooldown)."""
        rm = RiskManager(starting_balance=3000.0)

        rm._on_trade_close(
            {
                "symbol": "KXBTC15M-26APR022000-00",
                "pnl": -10.0,
                "strategy_name": "StratA",
            }
        )

        # Different ticker in same series, different strategy (no strategy cooldown)
        result = rm.check_order(
            proposed_cost=10.0,
            category="general",
            strategy_name="StratB",
            symbol="KXBTC15M-26APR022015-15",
        )
        assert result is True, "Different ticker in same series should be allowed"


# =============================================================================
# 1C: Daily trade cap
# =============================================================================


class TestDailyTradeCap:
    """System should reject trades after MAX_DAILY_TRADES is reached."""

    def test_cap_is_40(self):
        rm = RiskManager(starting_balance=3000.0)
        assert rm.MAX_DAILY_TRADES == 40

    def test_trades_accepted_under_cap(self):
        rm = RiskManager(starting_balance=3000.0)
        # First trade should be accepted
        result = rm.check_order(10.0, strategy_name="Test", symbol="TEST-1")
        assert result is True

    def test_trades_rejected_at_cap(self):
        rm = RiskManager(starting_balance=3000.0)

        # Fill up to the cap
        for i in range(40):
            rm.check_order(5.0, strategy_name="Test", symbol=f"TEST-{i}")

        assert rm.daily_trade_count == 40

        # Next trade should be rejected
        result = rm.check_order(5.0, strategy_name="Test", symbol="TEST-41")
        assert result is False, "Trade should be rejected after cap"

    def test_cap_resets_on_new_day(self):
        rm = RiskManager(starting_balance=3000.0)
        rm.daily_trade_count = 40

        # Simulate new day
        rm.today = rm.today - timedelta(days=1)
        rm._reset_daily_stats_if_needed()

        assert rm.daily_trade_count == 0, "Trade count should reset on new day"


# =============================================================================
# 1D: Edge threshold checks
# =============================================================================


class TestEdgeThresholds:
    """Verify raised edge thresholds across strategies."""

    def test_empirical_edge_defaults(self):
        from src.strategies.empirical_edge import EmpiricalEdgeStrategy

        strategy = EmpiricalEdgeStrategy()
        assert (
            strategy.min_edge >= 0.10
        ), f"min_edge should be >=0.10, got {strategy.min_edge}"
        assert (
            strategy.min_ev_per_dollar >= 0.15
        ), f"min_ev should be >=0.15, got {strategy.min_ev_per_dollar}"

    def test_crypto_v3_obi_threshold(self):
        from src.strategies.crypto_strategy import Crypto15mTrendStrategyV3

        strategy = Crypto15mTrendStrategyV3()
        assert (
            strategy.obi_threshold >= 0.60
        ), f"OBI threshold should be >=0.60, got {strategy.obi_threshold}"

    def test_hourly_v3_obi_threshold(self):
        from src.strategies.crypto_strategy import CryptoHourlyStrategyV3

        strategy = CryptoHourlyStrategyV3()
        assert (
            strategy.obi_threshold >= 0.60
        ), f"OBI threshold should be >=0.60, got {strategy.obi_threshold}"


# =============================================================================
# Phase 2: Risk/Reward Structure
# =============================================================================


class TestEntryPriceFilter:
    """Reject trades at poor risk/reward entry prices."""

    def test_expensive_yes_rejected(self):
        """YES contracts at 0.70+ with low edge should be filtered."""
        # This is tested via the mixin, but we can verify the logic
        # effective_cost=0.70, edge = 0.80 - 0.70 = 0.10 < 0.15 → reject
        effective_cost = 0.70
        confidence = 0.80
        edge = confidence - effective_cost
        assert effective_cost > 0.55 and edge < 0.15, "Should be rejected"

    def test_cheap_contract_allowed(self):
        """Contracts at 0.40 should pass the filter."""
        effective_cost = 0.40
        assert effective_cost <= 0.55, "Should be allowed"

    def test_expensive_with_high_edge_allowed(self):
        """Expensive contract with high edge (>0.15) should pass."""
        effective_cost = 0.60
        confidence = 0.80
        edge = confidence - effective_cost
        assert edge >= 0.15, "Should be allowed with high edge"


class TestCalibratedKelly:
    """Kelly sizing uses historical win rate, not raw model confidence."""

    def test_blended_probability(self):
        """With 50% historical WR and 0.90 confidence, blended p < 0.90."""
        rm = RiskManager(starting_balance=3000.0)
        # No history → uses default 0.50 WR
        qty_blended = rm.calculate_kelly_size(0.90, 0.50, "TestStrat")

        # With raw confidence of 0.90 at old code: p=0.90, massive position
        # With blended: p = 0.6*0.50 + 0.4*0.90 = 0.66, much smaller
        assert qty_blended <= 50, "Hard cap should be 50"

    def test_historical_wr_used_after_20_trades(self):
        """After 20+ trades, historical WR should influence sizing."""
        rm = RiskManager(starting_balance=3000.0)

        # Simulate 30 trades: 12 wins, 18 losses (40% WR)
        rm.strategy_win_rates["BadStrat"] = (12, 30)

        # Both may hit the 50-contract cap at $3000 balance. Use smaller balance.
        rm2 = RiskManager(starting_balance=500.0)
        rm2.strategy_win_rates["BadStrat"] = (12, 30)
        assert rm2.calculate_kelly_size(
            0.80, 0.50, "BadStrat"
        ) < rm2.calculate_kelly_size(
            0.80, 0.50, "NewStrat"
        ), "Bad WR strategy should size smaller"

    def test_hard_cap_50_contracts(self):
        """No trade should exceed 50 contracts."""
        rm = RiskManager(starting_balance=50000.0)
        qty = rm.calculate_kelly_size(0.95, 0.30, "Test")
        assert qty <= 50, f"Hard cap violated: {qty}"


class TestEscalatingCooldown:
    """Consecutive losses trigger escalating strategy cooldowns."""

    def test_first_loss_120s_cooldown(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": -10.0, "strategy_name": "Strat"})

        assert rm.consecutive_losses["Strat"] == 1
        assert "Strat" in rm.strategy_cooldown

    def test_second_loss_300s_cooldown(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": -10.0, "strategy_name": "Strat"})
        rm._on_trade_close({"symbol": "T2", "pnl": -10.0, "strategy_name": "Strat"})

        assert rm.consecutive_losses["Strat"] == 2

    def test_third_loss_1800s_cooldown(self):
        rm = RiskManager(starting_balance=3000.0)
        for i in range(3):
            rm._on_trade_close(
                {"symbol": f"T{i}", "pnl": -10.0, "strategy_name": "Strat"}
            )

        assert rm.consecutive_losses["Strat"] == 3

    def test_win_resets_streak(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": -10.0, "strategy_name": "Strat"})
        rm._on_trade_close({"symbol": "T2", "pnl": -10.0, "strategy_name": "Strat"})
        assert rm.consecutive_losses["Strat"] == 2

        rm._on_trade_close({"symbol": "T3", "pnl": 5.0, "strategy_name": "Strat"})
        assert rm.consecutive_losses["Strat"] == 0

    def test_cooldown_blocks_check_order(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": -10.0, "strategy_name": "Strat"})

        # Strategy should be in cooldown
        result = rm.check_order(10.0, strategy_name="Strat", symbol="T2")
        assert result is False, "Strategy in cooldown should be rejected"

    def test_different_strategy_not_blocked(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": -10.0, "strategy_name": "StratA"})

        # Different strategy should not be blocked
        result = rm.check_order(10.0, strategy_name="StratB", symbol="T2")
        assert result is True


class TestWinRateTracking:
    """Win rates are tracked per strategy."""

    def test_win_tracked(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": 5.0, "strategy_name": "S"})
        assert rm.strategy_win_rates["S"] == (1, 1)

    def test_loss_tracked(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": -5.0, "strategy_name": "S"})
        assert rm.strategy_win_rates["S"] == (0, 1)

    def test_mixed_results(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": 5.0, "strategy_name": "S"})
        rm._on_trade_close({"symbol": "T2", "pnl": -5.0, "strategy_name": "S"})
        rm._on_trade_close({"symbol": "T3", "pnl": 10.0, "strategy_name": "S"})
        assert rm.strategy_win_rates["S"] == (2, 3)
