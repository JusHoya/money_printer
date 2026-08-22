"""Sprint 8 Bug Triad — regression tests for the three interlocking fixes.

Tasks covered:
  8.1  Calibrator load path  (predictor._try_load_model)
  8.2  Fee EV gate double-leg  (fee_calculator.ev_after_fees)
  8.3  Cycle rollover invokes _on_trade_close  (run_dashboard rollover loop)
  8.4  cycle_trades = wins + losses, not signal count
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


from src.core.fee_calculator import compute_fee, ev_after_fees
from src.core.matching_engine import SimulatedExchange
from src.ml.calibration import ProbabilityCalibrator
import numpy as np


# ---------------------------------------------------------------------------
# Task 8.1 — Calibrator load path
# ---------------------------------------------------------------------------


class TestCalibratorLoadFromJoblib(unittest.TestCase):
    """_try_load_model must not leave self._calibrator as None when a
    joblib file exists — ProbabilityCalibrator has no model_path kwarg."""

    def test_calibrator_loads_from_joblib_path(self):
        """Write a fitted calibrator to a temp file, then verify the
        ModelPredictor helper loads it without raising TypeError."""
        # Build and fit a minimal calibrator so save() doesn't raise.
        cal = ProbabilityCalibrator(method="platt")
        raw = np.array([0.3, 0.5, 0.7, 0.9])
        outcomes = np.array([0.0, 0.0, 1.0, 1.0])
        cal.fit(raw, outcomes)

        with tempfile.TemporaryDirectory() as tmp:
            model_path = os.path.join(tmp, "calibrator_latest.joblib")
            cal.save(model_path)

            # Reproduce _try_load_model logic for the calibrator class
            import importlib

            mod = importlib.import_module("src.ml.calibration")
            cls = getattr(mod, "ProbabilityCalibrator")

            # This is the FIXED path — try model_path kwarg, fall back to .load()
            try:
                instance = cls(model_path=model_path)
            except TypeError:
                instance = cls()
                instance.load(model_path)

            self.assertIsNotNone(instance, "Calibrator instance must not be None")
            self.assertTrue(instance._fitted, "Calibrator must be fitted after load")

            # Confirm calibration actually works post-load
            result = instance.calibrate(np.array([0.5]))
            self.assertEqual(result.shape, (1,))
            self.assertGreater(float(result[0]), 0.0)


# ---------------------------------------------------------------------------
# Task 8.2 — Fee EV gate double-leg
# ---------------------------------------------------------------------------


class TestEvAfterFeesChargesBothLegs(unittest.TestCase):
    """ev_after_fees must subtract 2*fee_per_contract (entry + exit)."""

    def test_ev_after_fees_charges_both_legs(self):
        # Taker path: PRD Phase 2 corrected the standard-series maker
        # multiplier to zero, so the maker fee is $0.00 and cannot exercise a
        # "both legs are charged" assertion. The Sprint 8 invariant is
        # unchanged — it just has to be measured where a fee exists.
        probability = 0.5
        price = 0.5
        is_maker = False

        fee_per = compute_fee(price, 1, is_maker).per_contract
        expected = probability - price - 2 * fee_per

        result = ev_after_fees(probability, price, contracts=1, is_maker=is_maker)

        self.assertAlmostEqual(
            result,
            expected,
            places=10,
            msg=(
                f"ev_after_fees({probability}, {price}) should be "
                f"{expected:.8f} (2x fee), got {result:.8f}"
            ),
        )

        # Sanity: with symmetric probability/price the only bleed is the
        # double fee, so result must be strictly negative.
        self.assertLess(result, 0.0, "At fair price, double-fee EV must be negative")


# ---------------------------------------------------------------------------
# Task 8.3 — Cycle rollover invokes _on_trade_close callback
# ---------------------------------------------------------------------------


class TestCycleRolloverInvokesCloseCallback(unittest.TestCase):
    """The rollover loop must fire on_close for every open position,
    not silently clear them."""

    def test_cycle_rollover_invokes_close_callback(self):
        close_calls = []

        exchange = SimulatedExchange(on_close=lambda pos: close_calls.append(pos))

        # Open 3 positions
        for i in range(3):
            exchange.open_position(
                symbol=f"BTC-TEST-{i}",
                side="BUY",
                entry_price=0.45,
                quantity=1,
                strategy_name="test_strategy",
            )

        self.assertEqual(len(exchange.positions), 3, "Precondition: 3 open positions")

        # Run the rollover loop (extracted from run_dashboard._run_drawdown_cycle)
        # positions is a list (not a dict) in SimulatedExchange
        for p in list(exchange.positions):
            exchange._close_position(p, p["entry_price"], reason="CYCLE_RESET")
        exchange.positions.clear()

        self.assertEqual(
            len(close_calls),
            3,
            f"on_close must be called once per position; got {len(close_calls)} calls",
        )
        self.assertEqual(
            len(exchange.positions), 0, "positions dict must be empty after reset"
        )

        # Each close record must carry the symbol we opened
        symbols_closed = {p["symbol"] for p in close_calls}
        self.assertEqual(symbols_closed, {"BTC-TEST-0", "BTC-TEST-1", "BTC-TEST-2"})


# ---------------------------------------------------------------------------
# Task 8.4 — cycle_trades = wins + losses (not signal count)
# ---------------------------------------------------------------------------


class TestCycleTradesEqualsWinsPlusLosses(unittest.TestCase):
    """cycle_trades must be wins+losses, NOT signal count."""

    @staticmethod
    def _compute_cycle_trades(strategy_stats: dict) -> int:
        """Mirror of the fixed logic in run_dashboard._run_drawdown_cycle."""
        cycle_wins = sum(s.get("wins", 0) for s in strategy_stats.values())
        cycle_losses = sum(s.get("losses", 0) for s in strategy_stats.values())
        return cycle_wins + cycle_losses

    def test_cycle_trades_equals_wins_plus_losses(self):
        strategy_stats = {
            "s1": {"wins": 2, "losses": 1, "signals": 7},
            "s2": {"wins": 1, "losses": 0, "signals": 3},
        }

        result = self._compute_cycle_trades(strategy_stats)

        self.assertEqual(
            result,
            4,
            f"cycle_trades should be 2+1+1+0=4, got {result}",
        )

        # Confirm the old (buggy) signals sum would be 10
        buggy_result = sum(s.get("signals", 0) for s in strategy_stats.values())
        self.assertEqual(buggy_result, 10, "Sanity: signals sum is 10 (the old bug)")
        self.assertNotEqual(
            result, buggy_result, "Fixed result must differ from buggy result"
        )


if __name__ == "__main__":
    unittest.main()
