"""LEAN production config (2026-06-03 review) regression tests.

Phase 0 teardown (2026-07-24, PRD FR-0.1): the crypto bots and strategies this
file used to guard (btc_hourly_bot, crypto_15m_bot, ml_btc_15m, latency-arb
asset flags) were deleted; those tests were removed. What remains is the
weather-trading kill switch: the weather bot must stay FEED-ONLY until the
Phase 1-3 rebuild proves an edge.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.bots  # noqa: F401  — trigger registration
from src.bots.registry import BotRegistry
from src.bots import weather_bot


class TestDisabledStrategies(unittest.TestCase):
    def test_weather_trading_disabled(self):
        self.assertFalse(
            weather_bot.WEATHER_TRADING_ENABLED,
            "ML Weather + Meteorologist V2 must be disabled from live trading "
            "(feed-only until the Phase 1-3 rebuild)",
        )


class TestRegistryWeatherOnly(unittest.TestCase):
    def test_only_weather_bot_registered(self):
        """Post-teardown the registry must contain ONLY the weather bot."""
        self.assertEqual(
            BotRegistry.list_bots(),
            ["weather"],
            "Bot registry must register only the weather bot after Phase 0",
        )


if __name__ == "__main__":
    unittest.main()
