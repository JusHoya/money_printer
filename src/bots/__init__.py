"""Bot plugin system. Import all bot modules for auto-registration.

Phase 0 teardown (2026-07-24, PRD FR-0.1): the crypto bots (btc_15m,
btc_hourly, eth/sol/doge/xrp_15m) were deleted. The weather bot ran as the
only registered bot, feed-only, through Phases 1-3.

Phase 4 (2026-07-29, PRD FR-4.1/FR-4.3) adds the AAA gas bot as the second
registered engine. It is also **feed-only** — ``gas_bot.GAS_TRADING_ENABLED``
stays ``False`` until the Phase 4 EC-2 backtest artifact reports an EV above
zero net of maker fees — so registering it starts data harvesting with
provenance and nothing else. Registration is what makes the bot appear in
``--bot gas``, in ``create_all()`` and in the FR-0.4 cycle status line; without
it the bot exists on disk and never runs.
"""

from src.bots.gas_bot import GasBot
from src.bots.registry import BotRegistry
from src.bots.weather_bot import WeatherBot

# Registered HERE rather than with an ``@BotRegistry.register("gas")`` decorator
# on the class. ``BotRegistry.register`` returns a wrapper that registers and
# returns the class, so calling it directly is the same operation — but keeping
# it in this module means registration happens only when the bot package is
# imported, which is the sanctioned trigger every other bot uses, and it leaves
# ``gas_bot.py`` (workstream C's file) untouched.
BotRegistry.register("gas")(GasBot)

__all__ = ["GasBot", "WeatherBot"]
