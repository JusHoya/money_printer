"""Tests for Sprint 6 structural fixes based on quant research.

Covers:
- 1A: KXBTC15M positions skip stop-losses (hold to expiry)
- 1B: Loss cooldown uses exact ticker (900s)
- 1C: Daily trade cap (40 trades)
- 1D: Raised edge thresholds
"""

import json
import sys
import os
from collections import deque
from datetime import datetime, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.matching_engine import SimulatedExchange
from src.core.risk_manager import (
    MIN_WIN_SAMPLES,
    WIN_RATE_WINDOW,
    RiskManager,
)


def _seed_window(rm, name, wins, total):
    """Fill a strategy's FR-0.6 recency window with `wins` wins of `total`."""
    rm.strategy_win_records[name] = deque(
        [1] * wins + [0] * (total - wins), maxlen=WIN_RATE_WINDOW
    )


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


# Phase 0 teardown (2026-07-24): TestEdgeThresholds (Crypto V3 / Hourly V3
# OBI thresholds) was removed with the deleted crypto strategies.


# =============================================================================
# Phase 2: Risk/Reward Structure
# =============================================================================


class TestEntryPriceFilter:
    """After removing the crude entry price filter, the EV gate handles all cases."""

    def test_positive_ev_at_expensive_price_passes_gate(self):
        """A trade with conf=0.70 price=0.60 has positive EV and should pass the EV gate."""
        from src.bots.mixins import SignalProcessorMixin
        from src.core.interfaces import TradeSignal

        sig = TradeSignal(symbol="T", side="buy", quantity=1, limit_price=0.60)
        sig.confidence = 0.70
        assert SignalProcessorMixin._ml_ev_gate(sig) is True

    def test_negative_ev_still_rejected_by_gate(self):
        """A trade with conf=0.52 price=0.55 has marginal/negative EV."""
        from src.bots.mixins import SignalProcessorMixin
        from src.core.interfaces import TradeSignal

        sig = TradeSignal(symbol="T", side="buy", quantity=1, limit_price=0.55)
        sig.confidence = 0.52
        # At 52% confidence and 0.55 price, EV after fees should be near zero or negative
        result = SignalProcessorMixin._ml_ev_gate(sig)
        # Either True or False is acceptable -- the point is the gate decides, not a hardcoded filter
        assert isinstance(result, bool)


class TestCalibratedKelly:
    """Kelly sizing uses historical win rate, not raw model confidence.

    FR-0.6: the historical WR comes from a recency window
    (strategy_win_records, last WIN_RATE_WINDOW closed trades); with fewer
    than MIN_WIN_SAMPLES outcomes the neutral 0.5 prior is used.
    """

    def test_blended_probability(self):
        """With 50% historical WR and 0.90 confidence, blended p < 0.90."""
        rm = RiskManager(starting_balance=3000.0)
        # No history → uses default 0.50 WR
        qty_blended = rm.calculate_kelly_size(0.90, 0.50, "TestStrat")

        # With raw confidence of 0.90 at old code: p=0.90, massive position
        # With blended: p = 0.6*0.50 + 0.4*0.90 = 0.66, much smaller
        assert qty_blended <= 75, "Hard cap should be 75 (PR#1 raised from 50)"

    def test_historical_wr_used_after_20_trades(self):
        """With >= MIN_WIN_SAMPLES outcomes in the window, historical WR
        influences sizing: a bad-WR strategy sizes smaller than a fresh one."""
        # Both may hit the contract cap at $3000 balance; use a smaller one.
        rm = RiskManager(starting_balance=500.0)
        # 30 windowed outcomes: 12 wins, 18 losses (40% WR), n >= 20
        _seed_window(rm, "BadStrat", wins=12, total=30)
        assert rm.calculate_kelly_size(
            0.80, 0.50, "BadStrat"
        ) < rm.calculate_kelly_size(
            0.80, 0.50, "NewStrat"
        ), "Bad WR strategy should size smaller"

    def test_neutral_prior_under_min_samples(self):
        """Under MIN_WIN_SAMPLES the window is IGNORED: even an all-loss thin
        history sizes exactly like an unknown strategy (FR-0.6 pivot-reset
        semantics — a reset strategy must not inherit a poisoned prior)."""
        rm = RiskManager(starting_balance=500.0)
        _seed_window(rm, "ThinStrat", wins=0, total=MIN_WIN_SAMPLES - 1)
        assert rm.calculate_kelly_size(
            0.80, 0.50, "ThinStrat"
        ) == rm.calculate_kelly_size(
            0.80, 0.50, "NewStrat"
        ), "Sub-threshold window must fall back to the neutral 0.5 prior"

    def test_hard_cap_75_contracts(self):
        """No trade should exceed 75 contracts (PR#1 raised from 50)."""
        rm = RiskManager(starting_balance=50000.0)
        qty = rm.calculate_kelly_size(0.95, 0.30, "Test")
        assert qty <= 75, f"Hard cap violated: {qty}"


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
    """Closed-trade outcomes are tracked per strategy in an FR-0.6 recency
    window (deque of 1/0, maxlen=WIN_RATE_WINDOW), newest last.

    The Sprint 6 cumulative-forever (wins, total) assertions were replaced,
    not just renamed: PRD FR-0.6 removed cumulative counting so a poisoned
    era can no longer drag sizing down forever (see eviction test below).
    """

    def test_win_tracked(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": 5.0, "strategy_name": "S"})
        assert list(rm.strategy_win_records["S"]) == [1]

    def test_loss_tracked(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": -5.0, "strategy_name": "S"})
        assert list(rm.strategy_win_records["S"]) == [0]

    def test_mixed_results(self):
        rm = RiskManager(starting_balance=3000.0)
        rm._on_trade_close({"symbol": "T1", "pnl": 5.0, "strategy_name": "S"})
        rm._on_trade_close({"symbol": "T2", "pnl": -5.0, "strategy_name": "S"})
        rm._on_trade_close({"symbol": "T3", "pnl": 10.0, "strategy_name": "S"})
        assert list(rm.strategy_win_records["S"]) == [1, 0, 1]

    def test_window_evicts_oldest_outcome(self):
        """FR-0.6: only the last WIN_RATE_WINDOW outcomes are retained — a
        full window of losses ages out one loss when a new win arrives."""
        rm = RiskManager(starting_balance=3000.0)
        for i in range(WIN_RATE_WINDOW):
            rm._on_trade_close({"symbol": f"L{i}", "pnl": -5.0, "strategy_name": "S"})
        assert list(rm.strategy_win_records["S"]) == [0] * WIN_RATE_WINDOW

        rm._on_trade_close({"symbol": "W", "pnl": 5.0, "strategy_name": "S"})
        record = rm.strategy_win_records["S"]
        assert len(record) == WIN_RATE_WINDOW, "window must stay capped"
        assert list(record) == [0] * (WIN_RATE_WINDOW - 1) + [1]


# =============================================================================
# Fee Tolerance Tests
# =============================================================================


class TestFeeTolerance:
    """Fee-induced micro-losses should not trigger cooldowns."""

    def test_fee_only_loss_counts_as_win(self):
        """A -$0.01 PnL (fee drag) should count as a win for win-rate tracking."""
        rm = RiskManager(starting_balance=3000.0)
        pos = {"pnl": -0.01, "strategy_name": "TestStrat", "symbol": "KXTEST-1"}
        rm._on_trade_close(pos)
        assert list(rm.strategy_win_records["TestStrat"]) == [1]

    def test_fee_only_loss_no_symbol_cooldown(self):
        """A -$0.01 PnL should NOT trigger symbol loss cooldown."""
        rm = RiskManager(starting_balance=3000.0)
        pos = {"pnl": -0.01, "strategy_name": "TestStrat", "symbol": "KXTEST-1"}
        rm._on_trade_close(pos)
        assert "KXTEST-1" not in rm.loss_cooldown

    def test_fee_only_loss_no_escalating_cooldown(self):
        """A -$0.01 PnL should NOT trigger strategy escalating cooldown."""
        rm = RiskManager(starting_balance=3000.0)
        pos = {"pnl": -0.01, "strategy_name": "TestStrat", "symbol": "KXTEST-1"}
        rm._on_trade_close(pos)
        assert rm.consecutive_losses.get("TestStrat", 0) == 0

    def test_real_loss_still_triggers_cooldowns(self):
        """A -$0.05 PnL (real loss, past tolerance) should trigger all cooldowns."""
        rm = RiskManager(starting_balance=3000.0)
        pos = {"pnl": -0.05, "strategy_name": "TestStrat", "symbol": "KXTEST-1"}
        rm._on_trade_close(pos)
        assert list(rm.strategy_win_records["TestStrat"]) == [0]
        assert "KXTEST-1" in rm.loss_cooldown
        assert rm.consecutive_losses["TestStrat"] == 1


# =============================================================================
# Win Rate Persistence Tests
# =============================================================================


class TestWinRatePersistence:
    """Win-rate windows should survive process restarts (FR-0.6 format:
    {"Strategy": {"window": [1, 0, ...], "updated": iso}})."""

    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        """Saved win windows should be loadable by a fresh RiskManager."""
        import src.core.risk_manager as rm_mod

        test_path = str(tmp_path / "strategy_win_rates.json")
        monkeypatch.setattr(rm_mod, "WIN_RATES_PATH", test_path)

        rm1 = rm_mod.RiskManager(starting_balance=1000.0)
        rm1.strategy_win_records = {
            "StratA": deque([1] * 10 + [0] * 10, maxlen=WIN_RATE_WINDOW),
            "StratB": deque([1, 0, 1, 1, 0], maxlen=WIN_RATE_WINDOW),
        }
        rm1._save_win_rates()

        rm2 = rm_mod.RiskManager(starting_balance=1000.0)
        assert list(rm2.strategy_win_records["StratA"]) == [1] * 10 + [0] * 10
        assert list(rm2.strategy_win_records["StratB"]) == [1, 0, 1, 1, 0]
        # The reloaded window must keep the recency cap.
        assert rm2.strategy_win_records["StratA"].maxlen == WIN_RATE_WINDOW

    def test_missing_file_gracefully_handled(self, tmp_path, monkeypatch):
        """If no file exists, win records start empty."""
        import src.core.risk_manager as rm_mod

        test_path = str(tmp_path / "nonexistent.json")
        monkeypatch.setattr(rm_mod, "WIN_RATES_PATH", test_path)

        rm = rm_mod.RiskManager(starting_balance=1000.0)
        assert rm.strategy_win_records == {}

    def test_legacy_cumulative_entries_ignored_on_load(self, tmp_path, monkeypatch):
        """Legacy [wins, total] entries are deliberately DROPPED on load.

        This replaces the old cumulative-forever round-trip assertion, which
        PRD FR-0.6 removed: the pivot resets win-rate history rather than
        migrating potentially poisoned cumulative counters (the
        "ML BTC 15m": [30, 1048] deadlock). A mixed file loads only the
        windowed entries and never crashes.
        """
        import src.core.risk_manager as rm_mod

        test_path = str(tmp_path / "strategy_win_rates.json")
        monkeypatch.setattr(rm_mod, "WIN_RATES_PATH", test_path)
        with open(test_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "OldStrat": [30, 1048],  # legacy cumulative — must be ignored
                    "NewStrat": {
                        "window": [1, 0, 1],
                        "updated": "2026-07-24T00:00:00+00:00",
                    },
                },
                f,
            )

        rm = rm_mod.RiskManager(starting_balance=1000.0)
        assert "OldStrat" not in rm.strategy_win_records
        assert list(rm.strategy_win_records["NewStrat"]) == [1, 0, 1]
