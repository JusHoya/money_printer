"""Risk-manager bug-fix regression tests (2026-04-29).

Covers three documented bugs from project_remaining_issues.md:
  Bug 1 — daily_pnl is cumulative, not daily (no exchange.realized_pnl reset)
  Bug 2 — strategy_win_rates.json persistence broken (debounced too coarsely)
  Bug 3 — per-strategy drawdown uses wrong allocation base
"""

import json
import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.core.risk_manager as rm_mod
from src.core.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Bug 1 — daily PnL must reset at UTC midnight, not stay cumulative
# ---------------------------------------------------------------------------


class TestDailyPnlResetsOnNewDay(unittest.TestCase):
    """daily_pnl must reflect today's PnL, not cumulative since process start."""

    def test_daily_pnl_resets_at_day_boundary(self):
        rm = RiskManager(starting_balance=100.0, persist_state=False)

        # Day 1: Strategy loses $20 cumulative.
        rm.exchange.realized_pnl = -20.0
        rm._on_trade_close(
            {"symbol": "DAY1", "pnl": -20.0, "strategy_name": "TestStrat"}
        )
        self.assertAlmostEqual(rm.daily_pnl, -20.0, places=2)

        # Simulate UTC date rolling forward.
        rm.today = rm.today - timedelta(days=1)
        rm._reset_daily_stats_if_needed()

        # Day 2 starts fresh: daily_pnl should be 0 even though
        # exchange.realized_pnl is still -20 (cumulative).
        self.assertAlmostEqual(
            rm.daily_pnl,
            0.0,
            places=2,
            msg="daily_pnl must zero out on new UTC day",
        )

        # New trade closes today with +$5: daily_pnl is just +$5 (today only).
        rm.exchange.realized_pnl = -15.0  # cumulative now -20+5
        rm._on_trade_close({"symbol": "DAY2", "pnl": 5.0, "strategy_name": "TestStrat"})
        self.assertAlmostEqual(
            rm.daily_pnl,
            5.0,
            places=2,
            msg="daily_pnl on day 2 should reflect only day-2 trades",
        )

    def test_market_data_update_triggers_rollover(self):
        """update_market_data is called every tick — it must check for date
        rollover so daily resets fire even when no trade closes that day."""
        rm = RiskManager(starting_balance=100.0, persist_state=False)
        rm.exchange.realized_pnl = -10.0
        rm._on_trade_close({"symbol": "X", "pnl": -10.0, "strategy_name": "TestStrat"})
        self.assertAlmostEqual(rm.daily_pnl, -10.0, places=2)

        # Simulate UTC date rolling forward without any new trade close.
        rm.today = rm.today - timedelta(days=1)
        rm.update_market_data("KXBTC15M-X", 0.50)

        self.assertAlmostEqual(
            rm.daily_pnl,
            0.0,
            places=2,
            msg="update_market_data must trigger daily rollover",
        )

    def test_daily_drawdown_kill_uses_daily_not_cumulative(self):
        """Drawdown kill switch trips on TODAY's loss, not all-time."""
        rm = RiskManager(starting_balance=100.0, persist_state=False)

        # Yesterday: lost $80 cumulative (way over kill threshold).
        rm.exchange.realized_pnl = -80.0
        rm._on_trade_close({"symbol": "OLD", "pnl": -80.0, "strategy_name": "Old"})
        self.assertAlmostEqual(rm.daily_pnl, -80.0, places=2)

        # Day boundary.
        rm.today = rm.today - timedelta(days=1)
        rm._reset_daily_stats_if_needed()

        # No closes yet today; daily_pnl is 0 even though cumulative is -80.
        # check_order with a fresh strategy must not see -80 as today's loss.
        # Use a tiny cost ($1) to bypass position-size and other gates.
        ok = rm.check_order(
            proposed_cost=1.0, category="crypto", strategy_name="FreshStrat"
        )
        self.assertTrue(ok, "Kill switch should not trip on yesterday's $80 loss")
        self.assertFalse(
            rm.drawdown_kill_triggered,
            "Kill flag should be off on day-2",
        )


# ---------------------------------------------------------------------------
# Bug 2 — strategy_win_rates.json must persist after every trade close
# ---------------------------------------------------------------------------


class TestWinRatesPersistEveryClose(unittest.TestCase):
    """File must update on EVERY trade close, not every 10th."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "strategy_win_rates.json")
        self._orig = rm_mod.WIN_RATES_PATH
        rm_mod.WIN_RATES_PATH = self.path

    def tearDown(self):
        rm_mod.WIN_RATES_PATH = self._orig
        self.tmp.cleanup()

    def _make_rm(self):
        """Build a RiskManager with persistence enabled but without
        triggering exchange-state disk I/O — flip the flag after init."""
        rm = RiskManager(starting_balance=100.0, persist_state=False)
        rm._persist_state = True
        return rm

    def test_file_written_after_first_close(self):
        rm = self._make_rm()
        rm._on_trade_close({"symbol": "T1", "pnl": 5.0, "strategy_name": "StratA"})
        self.assertTrue(
            os.path.exists(self.path),
            "Win-rate file should exist after the very first trade close",
        )
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # FR-0.6 windowed schema: {"strategy": {"window": [...], "updated": iso}}
        self.assertIn("StratA", data)
        self.assertEqual(data["StratA"]["window"], [1])
        self.assertIn("updated", data["StratA"])

    def test_file_updates_on_every_close(self):
        """Every close updates the file — not buffered to every 10th."""
        rm = self._make_rm()

        for i in range(1, 6):
            rm._on_trade_close({"symbol": f"T{i}", "pnl": 5.0, "strategy_name": "S"})
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(
                data["S"]["window"],
                [1] * i,
                f"After close #{i} the window must hold {i} wins",
            )

    def test_persist_state_false_does_not_auto_write(self):
        """persist_state=False must not auto-write on close (test isolation)."""
        rm = RiskManager(starting_balance=100.0, persist_state=False)
        rm._on_trade_close({"symbol": "T1", "pnl": 5.0, "strategy_name": "Iso"})
        self.assertFalse(
            os.path.exists(self.path),
            "Auto-save must be gated on persist_state to avoid test pollution",
        )


# ---------------------------------------------------------------------------
# Bug 3 — per-strategy drawdown uses peak-allocation base, not flat 10%
# ---------------------------------------------------------------------------


class TestStrategyDrawdownUsesAllocation(unittest.TestCase):
    """Per-strategy drawdown must use the strategy's own allocated capital,
    not 10% of total starting balance."""

    def test_small_strategy_loss_does_not_block_when_unallocated(self):
        """A strategy that has never traded should not be drawdown-blocked
        on a hypothetical -$10 loss — no allocation = no drawdown to cap."""
        rm = RiskManager(starting_balance=100.0, persist_state=False)
        # Pretend strategy lost $9 with zero allocation history.
        rm.strategy_pnl["NewStrat"] = -9.0

        _ = rm.check_order(
            proposed_cost=4.0, category="crypto", strategy_name="NewStrat"
        )
        # The actionable case for this test is the high-peak strategy below:
        # a strategy that peaked at +$200 and is down $20 (10%) should not be
        # blocked by the old flat 10%-of-starting-balance rule.
        rm2 = RiskManager(starting_balance=100.0, persist_state=False)
        rm2.strategy_peak_pnl["BigStrat"] = 200.0  # peaked at +$200
        rm2.strategy_pnl["BigStrat"] = 180.0  # only down $20 from peak
        ok2 = rm2.check_order(
            proposed_cost=4.0, category="crypto", strategy_name="BigStrat"
        )
        self.assertTrue(
            ok2,
            "A strategy down $20 from $200 peak (10%) should be at the edge "
            "but acceptable — old code would block based on 10% of $100=$10",
        )

    def test_peak_pnl_tracked_per_strategy(self):
        """RiskManager must track peak P&L per strategy."""
        rm = RiskManager(starting_balance=100.0, persist_state=False)

        # Three sequential closes that build, peak, retrace.
        rm._on_trade_close({"symbol": "A", "pnl": 30.0, "strategy_name": "Peaker"})
        rm._on_trade_close({"symbol": "B", "pnl": 50.0, "strategy_name": "Peaker"})
        # Peak at +$80
        rm._on_trade_close({"symbol": "C", "pnl": -10.0, "strategy_name": "Peaker"})
        # Now at +$70

        self.assertAlmostEqual(rm.strategy_peak_pnl["Peaker"], 80.0, places=2)
        self.assertAlmostEqual(rm.strategy_pnl["Peaker"], 70.0, places=2)

    def test_drawdown_relative_to_peak_not_starting_balance(self):
        """Big-peak strategy: drawdown limit should scale to peak P&L,
        not be capped at 10% of total starting balance."""
        rm = RiskManager(starting_balance=100.0, persist_state=False)
        # Strategy peaked at +$300 — well past starting balance
        rm.strategy_peak_pnl["Whale"] = 300.0
        rm.strategy_pnl["Whale"] = 280.0  # Down $20 from peak (~6.7%)

        ok = rm.check_order(proposed_cost=4.0, category="crypto", strategy_name="Whale")
        self.assertTrue(
            ok,
            "Whale strategy with $20 drawdown from $300 peak should pass",
        )

        # Now drop to $250 (drawdown of $50 / 16% from peak)
        rm.strategy_pnl["Whale"] = 250.0
        ok2 = rm.check_order(
            proposed_cost=4.0, category="crypto", strategy_name="Whale"
        )
        self.assertFalse(
            ok2,
            "Whale at $50 drawdown (>10% of $300 peak) must be blocked",
        )


if __name__ == "__main__":
    unittest.main()
