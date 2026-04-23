"""PR #2 — multi-asset 15-minute crypto bot regression tests.

Confirms Crypto15mBot registers one bot per supported asset, each pointing
at the correct KX{asset}15M Kalshi series and the correct Coinbase spot
pair, without breaking the existing BTC15mBot.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.bots  # noqa: F401  — trigger registration
from src.bots.registry import BotRegistry
from src.bots.crypto_15m_bot import SUPPORTED_ASSETS


class TestMultiAssetRegistration(unittest.TestCase):
    def test_all_supported_assets_register(self):
        registered = BotRegistry.list_bots()
        for asset in SUPPORTED_ASSETS:
            self.assertIn(
                f"{asset.lower()}_15m",
                registered,
                f"{asset.lower()}_15m must be in registry",
            )

    def test_btc_15m_bot_still_works(self):
        """PR #2 must not regress the existing BTC 15m bot."""
        self.assertIn("btc_15m", BotRegistry.list_bots())
        btc = BotRegistry.create("btc_15m")
        self.assertEqual(btc.get_symbols(), ["KXBTC15M"])
        self.assertIn(
            "ml_btc_15m", btc.strategies, "BTC must still have the ML strategy"
        )
        self.assertIn("cross_arb", btc.strategies)
        self.assertIn("latency_arb", btc.strategies)

    def test_each_non_btc_asset_has_correct_series_base(self):
        for asset in SUPPORTED_ASSETS:
            bot = BotRegistry.create(f"{asset.lower()}_15m")
            self.assertEqual(bot.asset, asset)
            self.assertEqual(bot.series_base, f"KX{asset}15M")
            self.assertEqual(bot.get_symbols(), [f"KX{asset}15M"])

    def test_non_btc_bots_do_not_include_btc_ml_strategy(self):
        """ML BTC 15m strategy is BTC-specific and must NOT be wired into
        non-BTC bots until per-asset models are trained."""
        for asset in SUPPORTED_ASSETS:
            bot = BotRegistry.create(f"{asset.lower()}_15m")
            self.assertNotIn(
                "ml_btc_15m",
                bot.strategies,
                f"{asset} bot must not use BTC-specific ML strategy",
            )
            # But it should still have the asset-agnostic arb strategies
            self.assertIn("cross_arb", bot.strategies)
            self.assertIn("latency_arb", bot.strategies)

    def test_bot_name_tags_asset(self):
        for asset in SUPPORTED_ASSETS:
            bot = BotRegistry.create(f"{asset.lower()}_15m")
            self.assertIn(
                asset, bot.name, f"Bot name '{bot.name}' should contain {asset}"
            )


class TestCoinbaseWiring(unittest.TestCase):
    """setup() must default Coinbase to the matching asset pair."""

    def test_coinbase_product_id_matches_asset(self):
        # Use a no-op setup that captures the Coinbase product_id
        bot = BotRegistry.create("eth_15m")

        # Dummy kalshi so setup doesn't early-exit
        class _DummyKalshi:
            pass

        # Provide no coinbase — setup() must create one
        bot.setup(kalshi=_DummyKalshi(), coinbase=None)
        self.assertEqual(bot.coinbase.product_id, "ETH-USD")

    def test_coinbase_injection_respected(self):
        """Explicit coinbase arg must not be overwritten."""
        bot = BotRegistry.create("sol_15m")

        class _MockCoinbase:
            product_id = "SOL-USD"

        mock = _MockCoinbase()

        class _DummyKalshi:
            pass

        bot.setup(kalshi=_DummyKalshi(), coinbase=mock)
        self.assertIs(bot.coinbase, mock)


if __name__ == "__main__":
    unittest.main()
