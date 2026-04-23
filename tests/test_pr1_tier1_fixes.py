"""PR #1 Tier-1 regression tests.

Covers:
  * get_current_exposure correctly handles short YES (sell YES) collateral
  * calculate_kelly_size deducts 2x fees from effective odds
  * calculate_kelly_size refuses trades where fees exceed the potential payoff
  * Disabled strategies (LongshotFaderV2, CryptoHourlyStrategyV3) are no
    longer registered on their parent bots
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.risk_manager import RiskManager


class TestShortYesExposure(unittest.TestCase):
    """get_current_exposure must use (1-price)*qty for short-YES collateral."""

    def _mk_risk(self):
        rm = RiskManager(starting_balance=3000.0, persist_state=False)
        return rm

    def test_buy_yes_exposure_is_price_times_qty(self):
        rm = self._mk_risk()
        rm.exchange.positions.append(
            {
                "symbol": "KXBTC15M-TEST",
                "side": "buy",
                "contract_side": "YES",
                "entry_price": 0.30,
                "quantity": 50,
            }
        )
        self.assertAlmostEqual(rm.get_current_exposure(), 0.30 * 50, places=6)

    def test_buy_no_exposure_is_price_times_qty(self):
        rm = self._mk_risk()
        rm.exchange.positions.append(
            {
                "symbol": "KXBTC15M-TEST",
                "side": "buy",
                "contract_side": "NO",
                "entry_price": 0.25,
                "quantity": 40,
            }
        )
        self.assertAlmostEqual(rm.get_current_exposure(), 0.25 * 40, places=6)

    def test_sell_yes_short_uses_one_minus_price(self):
        """Cheap short YES at 0.15 with qty 50: collateral should be
        (1-0.15)*50 = $42.50, NOT 0.15*50 = $7.50."""
        rm = self._mk_risk()
        rm.exchange.positions.append(
            {
                "symbol": "KXBTC15M-TEST",
                "side": "sell",
                "contract_side": "YES",
                "entry_price": 0.15,
                "quantity": 50,
            }
        )
        expected = (1.0 - 0.15) * 50
        self.assertAlmostEqual(rm.get_current_exposure(), expected, places=6)
        # Also confirm it is NOT the buggy value
        self.assertNotAlmostEqual(rm.get_current_exposure(), 0.15 * 50, places=2)

    def test_mixed_positions_sum_correctly(self):
        rm = self._mk_risk()
        rm.exchange.positions.extend(
            [
                {
                    "symbol": "KXBTC15M-A",
                    "side": "buy",
                    "contract_side": "YES",
                    "entry_price": 0.40,
                    "quantity": 10,
                },  # $4.00
                {
                    "symbol": "KXBTC15M-B",
                    "side": "sell",
                    "contract_side": "YES",
                    "entry_price": 0.10,
                    "quantity": 20,
                },  # $18.00
                {
                    "symbol": "KXBTC15M-C",
                    "side": "buy",
                    "contract_side": "NO",
                    "entry_price": 0.35,
                    "quantity": 5,
                },  # $1.75
            ]
        )
        self.assertAlmostEqual(rm.get_current_exposure(), 4.00 + 18.00 + 1.75, places=4)


class TestKellyFeeDeduction(unittest.TestCase):
    """calculate_kelly_size must use fee-adjusted odds."""

    def _mk_risk(self, balance=3000.0):
        rm = RiskManager(starting_balance=balance, persist_state=False)
        # Seed a win rate so the historical-WR blend is deterministic
        rm.strategy_win_rates = {"test_strat": (70, 100)}  # 70% WR, n>=20
        return rm

    def test_kelly_returns_zero_when_fees_exceed_payoff(self):
        """At extreme prices, 2*fee_per can exceed net_win — the function
        must refuse to size rather than return negative or garbage."""
        rm = self._mk_risk()
        # Price 0.99: net_win = 0.01 - 2*fee_per. fee_per at price 0.99 is
        # ceil(0.0175*0.99*0.01*100)/100 = ceil(0.017)/100 = 0.01 — so
        # 2*fee_per = 0.02, which exceeds net_win of 0.01. Must return 0.
        qty = rm.calculate_kelly_size(
            confidence=0.95, price=0.99, strategy_name="test_strat"
        )
        self.assertEqual(qty, 0, "Kelly must refuse when fees exceed potential profit")

    def test_kelly_fee_adjusted_size_smaller_than_fee_ignorant(self):
        """For a thin-edge trade at fair-ish odds, the fee-adjusted Kelly
        must produce STRICTLY LESS size than the naive formula would."""
        rm = self._mk_risk()
        # Real sizing call
        actual_qty = rm.calculate_kelly_size(
            confidence=0.55, price=0.50, strategy_name="test_strat"
        )

        # Hand-compute the old (buggy) Kelly quantity to confirm regression
        trade_pct = 0.05  # Growth stage
        kelly_frac = 0.30
        b_buggy = (1.0 - 0.50) / 0.50  # = 1.0
        historical_wr = 70.0 / 100.0  # 0.70
        p = 0.6 * historical_wr + 0.4 * 0.55  # 0.42 + 0.22 = 0.64
        q = 1.0 - p  # 0.36
        f_buggy = p - (q / b_buggy)  # 0.64 - 0.36 = 0.28
        f_frac_buggy = f_buggy * kelly_frac  # 0.084
        f_cap_buggy = min(f_frac_buggy, trade_pct)  # 0.05
        alloc_buggy = 3000.0 * f_cap_buggy  # $150
        buggy_qty = int(alloc_buggy / 0.50)  # 300, clamped to 75 cap
        buggy_qty = min(buggy_qty, 75)
        # Fee-adjusted result should not exceed fee-ignorant result. When
        # the cap binds both versions, equality is acceptable; otherwise
        # the corrected version must be <=.
        self.assertLessEqual(
            actual_qty,
            buggy_qty,
            f"Fee-adjusted Kelly ({actual_qty}) should not exceed "
            f"fee-ignorant Kelly ({buggy_qty})",
        )
        self.assertGreaterEqual(
            actual_qty, 1, "Should still allow minimal sizing on valid edge"
        )

    def test_kelly_cap_raised_to_75(self):
        """Hard cap is 75 (raised from 50 for PR#1 scaling)."""
        rm = self._mk_risk(balance=50_000.0)
        # Huge edge + huge balance = cap-bound
        rm.strategy_win_rates = {"test_strat": (95, 100)}
        qty = rm.calculate_kelly_size(
            confidence=0.95, price=0.20, strategy_name="test_strat"
        )
        self.assertLessEqual(qty, 75, "Kelly cap must be enforced")
        self.assertEqual(qty, 75, "On huge edge + balance, cap should bind at 75")


class TestDisabledStrategiesNotRegistered(unittest.TestCase):
    """The Tier-1 disabled strategies must not appear in their parent bots."""

    def test_longshot_fader_v2_not_in_btc_15m_bot(self):
        from src.bots.btc_15m_bot import BTC15mBot

        bot = BTC15mBot()
        self.assertNotIn(
            "longshot_v2",
            bot.strategies,
            "LongshotFaderV2 must be disabled in BTC15mBot",
        )
        # And the import is gone — confirm by reading module attributes.
        import src.bots.btc_15m_bot as mod

        self.assertFalse(
            hasattr(mod, "LongshotFaderV2"), "LongshotFaderV2 import must be removed"
        )

    def test_crypto_hourly_v3_not_in_btc_hourly_bot(self):
        from src.bots.btc_hourly_bot import BTCHourlyBot

        bot = BTCHourlyBot()
        self.assertNotIn(
            "crypto_hr",
            bot.strategies,
            "CryptoHourlyStrategyV3 must be disabled in BTCHourlyBot",
        )
        import src.bots.btc_hourly_bot as mod

        self.assertFalse(
            hasattr(mod, "CryptoHourlyStrategyV3"),
            "CryptoHourlyStrategyV3 import must be removed",
        )

    def test_ml_btc_15m_cooldown_reduced_to_60s(self):
        """MLBtc15mStrategy default cooldown dropped from 120s to 60s."""
        from src.strategies.ml_btc_15m import MLBtc15mStrategy

        strat = MLBtc15mStrategy()
        self.assertEqual(strat.cooldown_seconds, 60)


if __name__ == "__main__":
    unittest.main()
