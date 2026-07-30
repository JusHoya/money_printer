"""LEAN production config (2026-06-03 review) regression tests.

Phase 0 teardown (2026-07-24, PRD FR-0.1): the crypto bots and strategies this
file used to guard (btc_hourly_bot, crypto_15m_bot, ml_btc_15m, latency-arb
asset flags) were deleted; those tests were removed. What remains are the
trading kill switches: every registered bot must stay FEED-ONLY until its own
phase proves an edge.

WHY THE REGISTRY ASSERTION CHANGED IN PHASE 4 (2026-07-29)
----------------------------------------------------------
``test_only_weather_bot_registered`` asserted ``list_bots() == ["weather"]``.
That was the correct statement of the Phase 0 exit criterion *while weather was
the only engine*, which PRD Phases 1-3 assumed. PRD Phase 4 (FR-4.1/FR-4.3)
deliberately adds a second engine — the AAA gas convergence bot — so the
one-bot assertion now contradicts the PRD's own roadmap rather than protecting
it. It is therefore restated, not removed, and not loosened into a subset
check: the registry must equal the exact expected set below. An unexpected bot
(a revived crypto bot, a duplicate registration, a stray import) still fails
here, and a bot that silently stops registering fails here too.

What the criterion was actually protecting is that no bot can trade without a
phase verdict, and that is now asserted per bot in ``TestDisabledStrategies``
below — for gas as well as weather.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.bots  # noqa: F401  — trigger registration
from src.bots.registry import BotRegistry
from src.bots import gas_bot, weather_bot

#: Every bot the PRD sanctions as registered, as of Phase 4. Adding to this set
#: is a deliberate act that must come with the phase that authorizes the engine.
EXPECTED_REGISTERED_BOTS = {"weather", "gas"}


class TestDisabledStrategies(unittest.TestCase):
    def test_weather_trading_disabled(self):
        self.assertFalse(
            weather_bot.WEATHER_TRADING_ENABLED,
            "ML Weather + Meteorologist V2 must be disabled from live trading "
            "(feed-only until the Phase 1-3 rebuild)",
        )

    def test_gas_trading_disabled(self):
        """PRD Phase 4 EC-2 gates gas paper trading on a backtest EV verdict.

        The bot is registered so its feeds run and its ladders are harvested;
        the strategy waterfall stays switched off until that artifact exists.
        """
        self.assertFalse(
            gas_bot.GAS_TRADING_ENABLED,
            "The AAA gas bot must stay FEED-ONLY until the Phase 4 EC-2 "
            "backtest artifact reports EV > 0 net of maker fees",
        )


class TestRegistryContents(unittest.TestCase):
    def test_exactly_the_sanctioned_bots_are_registered(self):
        """The registry must equal the sanctioned set — no more, no fewer.

        Compared as a set because registration order is a function of which
        module a test session imported first, which is not a property worth
        pinning. Equality (not ``issubset``) is what keeps this a real test.
        """
        self.assertEqual(
            set(BotRegistry.list_bots()),
            EXPECTED_REGISTERED_BOTS,
            "Bot registry must contain exactly the PRD-sanctioned bots "
            f"({sorted(EXPECTED_REGISTERED_BOTS)}); got "
            f"{sorted(BotRegistry.list_bots())}",
        )

    def test_no_duplicate_registrations(self):
        """A double-registered name would be invisible to the set check above."""
        names = BotRegistry.list_bots()
        self.assertEqual(
            len(names), len(set(names)), f"duplicate bot registration: {names}"
        )


if __name__ == "__main__":
    unittest.main()
