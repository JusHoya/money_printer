"""LEAN production config (2026-06-03 review) regression tests.

Asserts that the net-negative / statistically-dead strategies are disabled
from live trading while the primary edge (ML BTC 15m) keeps running, and that
the previously-deleted strategies remain fully unregistered.

Disabled per the 2026-06-03 review:
  - ML Weather + Meteorologist V2 (weather_bot)        -> WEATHER_TRADING_ENABLED
  - ML BTC Hourly (btc_hourly_bot)                     -> BTC_HOURLY_TRADING_ENABLED
  - SOL / DOGE Latency Arb (crypto_15m_bot)            -> LATENCY_ARB_DISABLED_ASSETS

Kept (watch-only / unproven, n<12): ETH + XRP Latency Arb.
Kept (primary edge): ML BTC 15m.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.bots  # noqa: F401  — trigger registration
from src.bots.registry import BotRegistry
from src.bots import weather_bot, btc_hourly_bot, crypto_15m_bot


class TestDisabledStrategies(unittest.TestCase):
    def test_weather_trading_disabled(self):
        self.assertFalse(
            weather_bot.WEATHER_TRADING_ENABLED,
            "ML Weather + Meteorologist V2 must be disabled from live trading",
        )

    def test_btc_hourly_trading_disabled(self):
        self.assertFalse(
            btc_hourly_bot.BTC_HOURLY_TRADING_ENABLED,
            "ML BTC Hourly must be disabled from live trading",
        )

    def test_sol_doge_latency_arb_disabled(self):
        disabled = crypto_15m_bot.LATENCY_ARB_DISABLED_ASSETS
        self.assertIn("SOL", disabled, "SOL Latency Arb must be disabled (dead)")
        self.assertIn("DOGE", disabled, "DOGE Latency Arb must be disabled (dead/neg)")

    def test_eth_xrp_latency_arb_kept(self):
        """ETH + XRP latency arb are watch-only/unproven — kept enabled."""
        disabled = crypto_15m_bot.LATENCY_ARB_DISABLED_ASSETS
        self.assertNotIn(
            "ETH", disabled, "ETH Latency Arb must stay enabled (watch-only)"
        )
        self.assertNotIn(
            "XRP", disabled, "XRP Latency Arb must stay enabled (watch-only)"
        )


class TestPrimaryEdgeKept(unittest.TestCase):
    def test_ml_btc_15m_still_active(self):
        """ML BTC 15m is the primary edge and must remain in the waterfall."""
        btc = BotRegistry.create("btc_15m")
        self.assertIn(
            "ml_btc_15m",
            btc.strategies,
            "ML BTC 15m must remain in the BTC 15m waterfall",
        )


class TestDeletedStrategiesUnregistered(unittest.TestCase):
    """Empirical Edge / Crypto Hourly V3 / Late Sniper / Time Decay were deleted
    and must not be importable or wired into any bot's strategy dict."""

    def test_no_deleted_strategy_modules_importable(self):
        for mod in (
            "src.strategies.empirical_edge",
            "src.strategies.time_decay",
            "src.strategies.late_sniper",
            "src.strategies.crypto_hourly_v3",
        ):
            with self.assertRaises(
                ModuleNotFoundError, msg=f"{mod} should be deleted/unimportable"
            ):
                __import__(mod)

    def test_no_bot_wires_deleted_strategy(self):
        banned = {
            "empirical",
            "late_sniper",
            "time_decay",
            "crypto_hourly_v3",
            "hourly_v3",
        }
        for name in BotRegistry.list_bots():
            bot = BotRegistry.create(name)
            for key in bot.strategies:
                self.assertNotIn(
                    key.lower(),
                    banned,
                    f"Bot '{name}' must not wire deleted strategy '{key}'",
                )


if __name__ == "__main__":
    unittest.main()
