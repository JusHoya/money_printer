"""LEAN production config (2026-06-03 review) regression tests.

Phase 0 teardown (2026-07-24, PRD FR-0.1): the crypto bots and strategies this
file used to guard (btc_hourly_bot, crypto_15m_bot, ml_btc_15m, latency-arb
asset flags) were deleted; those tests were removed. What remains are the
trading kill switches: this file pins each registered bot's sanctioned trading
posture, whichever direction that posture points.

WHY THE REGISTRY ASSERTION CHANGED IN PHASE 4 (2026-07-29)
----------------------------------------------------------
``test_only_weather_bot_registered`` asserted ``list_bots() == ["weather"]``.
That was the correct statement of the Phase 0 exit criterion *while weather was
the only engine*, which PRD Phases 1-3 assumed. PRD Phase 4 (FR-4.1/FR-4.3)
deliberately added a second engine — the AAA gas convergence bot — so the
one-bot assertion was restated as an exact-set check, not loosened into a
subset check. An unexpected bot (a revived crypto bot, a duplicate
registration, a stray import) still fails here, and a bot that silently stops
registering fails here too.

WHY THE WEATHER FLAG ASSERTION FLIPPED (2026-09-01)
---------------------------------------------------
The revival (revival/pleiades-2026-09) re-enabled weather PAPER trading on the
sandbox: HANDOFF.md §2 flags the settlement path *through the simulator* as
the one unverified leg ("No weather position has ever been opened"), and
exercising it plus generating real PnL/time-history is the sanctioned purpose.
This is not a reversal of the Phase 2 HALT — live capital remains structurally
impossible (KalshiProvider read_only=True; place_order raises; all execution
is SimulatedExchange). The test now pins the flag ON so an accidental
re-disable is as loud as an accidental enable used to be.

The three 2026-09-01 harvester additions (mention, crypto_annual, tweets) ship
feed-only and are pinned OFF below, each with the evidence that would flip it.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.bots  # noqa: F401  — trigger registration
from src.bots.registry import BotRegistry
from src.bots import crypto_annual_bot, gas_bot, mention_bot, tweets_bot, weather_bot

#: Every bot the project sanctions as registered, as of the 2026-09-01 revival.
#: Adding to this set is a deliberate act that must come with the decision that
#: authorizes the engine.
EXPECTED_REGISTERED_BOTS = {"weather", "gas", "mention", "crypto_annual", "tweets"}


class TestTradingFlags(unittest.TestCase):
    def test_weather_paper_trading_enabled(self):
        """2026-09-01 sandbox paper activation (see module docstring).

        Pinned ON deliberately: the settlement-leg exercise and PnL history
        this activation exists for both stop silently if the flag regresses.
        """
        self.assertTrue(
            weather_bot.WEATHER_TRADING_ENABLED,
            "Weather PAPER trading was activated 2026-09-01 (sandbox "
            "settlement-leg exercise; live capital impossible via read_only). "
            "If this flag is being turned back off, that is a posture decision "
            "— record it in the flag's comment and update this test with the "
            "reasoning, as the activation did.",
        )

    def test_gas_trading_disabled(self):
        """PRD Phase 4 EC-2 gates gas paper trading on a backtest EV verdict.

        The Phase 4 verdict was HALT (2026-07-30): model Brier 0.1332 vs
        market 0.0775. The bot stays registered so its feeds run; the strategy
        waterfall stays switched off.
        """
        self.assertFalse(
            gas_bot.GAS_TRADING_ENABLED,
            "The AAA gas bot must stay FEED-ONLY: Phase 4 closed with a HALT "
            "(the market mid out-forecast the model at 10 of 10 settlements)",
        )

    def test_mention_trading_disabled(self):
        """The mention engine has no transcript-derived base rates yet.

        The activation path (mention_strategy docstring) requires base rates
        built from historical settlement-source transcripts with the
        MENTION.pdf grammar encoded, then settlement-true evidence — none of
        which exists.
        """
        self.assertFalse(
            mention_bot.MENTION_TRADING_ENABLED,
            "The mention bot must stay FEED-ONLY until transcript-derived "
            "base rates exist and a settlement-true verdict authorizes it",
        )

    def test_crypto_annual_trading_disabled(self):
        """No strategy exists for the KXBTCY/KXETHY annual ladders at all."""
        self.assertFalse(
            crypto_annual_bot.CRYPTO_ANNUAL_TRADING_ENABLED,
            "The crypto annual bot is a harvester; no strategy has been "
            "proposed, let alone adjudicated, for the annual ladders",
        )

    def test_tweets_trading_disabled(self):
        """No strategy exists for the X-settled markets; the X poller behind
        the bot is itself gated by ``X_FEED_ENABLED`` (off by default)."""
        self.assertFalse(
            tweets_bot.TWEETS_TRADING_ENABLED,
            "The tweets bot is a harvester (Kalshi X-settled ladders + the X "
            "timeline tape); no strategy has been proposed for it",
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
            "Bot registry must contain exactly the sanctioned bots "
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
