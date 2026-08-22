import unittest
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.matching_engine import SimulatedExchange


class TestMatchingEngineV2(unittest.TestCase):
    def setUp(self):
        self.exchange = SimulatedExchange()
        self.exchange.TAKE_PROFIT_PCT = 10.0  # Disable TP for testing
        self.exchange.STOP_LOSS_PCT = 10.0  # Disable SL for testing

    def test_precip_pnl_update(self):
        print("\n--- Testing KXPRECIP PnL Update ---")
        # 1. Open Position: Buy YES (Long) on Precip NYC
        # Symbol must contain city fragment AND PRECIP for routing
        self.exchange.open_position("KXPRECIPNYC-TestPeriod", "buy", 0.20, 100)
        # Disable profit targets for this test (testing raw PnL updates)
        self.exchange.positions[0]["profit_targets"] = []

        # Initial PnL should be 0
        self.assertEqual(self.exchange.unrealized_pnl, 0.0)

        # 2. Update Market: PoP increases to 0.50
        # Precip uses direct price pass-through
        # PnL = (0.50 - 0.20) * 100 = 30.0
        self.exchange.update_market("PRECIP_KNYC", 0.50)

        stats = self.exchange.get_stats()
        print(f"Stats after update: {stats}")

        self.assertAlmostEqual(stats["unrealized"], 30.0)

        # 3. Update Market: PoP drops to 0.10
        # Loss. Current Price -> 0.10.
        # PnL = (0.10 - 0.20) * 100 = -10.0
        self.exchange.update_market("PRECIP_KNYC", 0.10)
        stats = self.exchange.get_stats()
        self.assertAlmostEqual(stats["unrealized"], -10.0)

    def test_temp_pnl_update(self):
        """REWRITTEN 2026-07-25 (PRD FR-1.2/B3): weather is never tanh-marked.

        This test used to assert the tanh estimator's output on a KXHIGH
        position: temp 5F above the parsed suffix "strike" produced an
        unrealized +$22.6 on a 100-lot. That mark was fiction — a binary
        bracket's value is P(YES), which a signed distance to one number
        cannot express, and the profit-target ladder then traded against it.
        The engine now marks a weather position at the observed Kalshi price,
        or holds it at entry when there is none. The assertions move
        accordingly; the phantom-mark behaviour they pinned was the defect.
        """
        print("\n--- Testing KXHIGH (Temp) PnL Update ---")
        # 1. Open Position: Buy YES on the 75-76 bracket for NYC.
        # Symbol must contain city fragment (NY) and KXHIGH for routing.
        self.exchange.open_position(
            "KXHIGHNY-TestPeriod-75",
            "buy",
            0.50,
            100,
            strike_type="between",
            floor_strike=75,
            cap_strike=76,
        )

        # 2. No observed Kalshi price yet: the mark holds at entry regardless
        #    of the temperature, so unrealized PnL is exactly zero.
        self.exchange.update_market("TEMP_KNYC", 75.0)
        stats = self.exchange.get_stats()
        self.assertAlmostEqual(stats["unrealized"], 0.0)

        # 3. A blistering temperature must NOT manufacture a mark.
        self.exchange.update_market("TEMP_KNYC", 80.0)
        stats = self.exchange.get_stats()
        self.assertAlmostEqual(stats["unrealized"], 0.0)

        # 4. Once a real Kalshi price is observed, THAT is the mark.
        self.exchange.update_market_price("KXHIGHNY-TestPeriod-75", 0.73)
        self.exchange.update_market("TEMP_KNYC", 80.0)
        stats = self.exchange.get_stats()
        self.assertAlmostEqual(stats["unrealized"], (0.73 - 0.50) * 100)
        print(f"Unrealized at observed $0.73: {stats['unrealized']:.2f}")

    def test_cross_contamination(self):
        print("\n--- Testing Cross Contamination ---")
        # Open Precip Position (must contain city fragment for routing)
        self.exchange.open_position("KXPRECIPNYC-Test", "buy", 0.20, 100)

        # Update TEMP for NYC. Should NOT affect Precip.
        self.exchange.update_market("TEMP_KNYC", 99.0)  # Extreme temp

        stats = self.exchange.get_stats()
        self.assertEqual(stats["unrealized"], 0.0)  # Should be untouched


if __name__ == "__main__":
    unittest.main()
