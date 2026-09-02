"""Bot plugin system. Import all bot modules for auto-registration.

Phase 0 teardown (2026-07-24, PRD FR-0.1): the crypto bots (btc_15m,
btc_hourly, eth/sol/doge/xrp_15m) were deleted. The weather bot ran as the
only registered bot, feed-only, through Phases 1-3.

Phase 4 (2026-07-29, PRD FR-4.1/FR-4.3) adds the AAA gas bot as the second
registered engine, feed-only.

Revival 2026-09-01 (revival/pleiades-2026-09) adds two more FEED-ONLY
harvesters: the mention bot (Kalshi Mentions category) and the crypto annual
bot (KXBTCY/KXETHY range ladders). Both trading flags are ``False`` — see each
module's docstring for what evidence would flip them. In the same change the
weather bot's flag flipped to PAPER trading on the sandbox (settlement-leg
exercise; live capital remains impossible via read_only). Later the same day
the FEED-ONLY tweets bot joined: the Kalshi X-settled series plus the X
timeline tape (``X_FEED_ENABLED``-gated) that would price them. Registration
is what makes a bot appear in ``--bot <name>``, in ``create_all()`` and in the
FR-0.4 cycle status line; without it the bot exists on disk and never runs.
"""

from src.bots.crypto_annual_bot import CryptoAnnualBot
from src.bots.gas_bot import GasBot
from src.bots.mention_bot import MentionBot
from src.bots.registry import BotRegistry
from src.bots.tweets_bot import TweetsBot
from src.bots.weather_bot import WeatherBot

# Registered HERE rather than with an ``@BotRegistry.register("...")`` decorator
# on each class. ``BotRegistry.register`` returns a wrapper that registers and
# returns the class, so calling it directly is the same operation — but keeping
# it in this module means registration happens only when the bot package is
# imported, which is the sanctioned trigger every other bot uses, and it keeps
# the harvester modules registration-free (the gas precedent).
BotRegistry.register("gas")(GasBot)
BotRegistry.register("mention")(MentionBot)
BotRegistry.register("crypto_annual")(CryptoAnnualBot)
BotRegistry.register("tweets")(TweetsBot)

__all__ = ["CryptoAnnualBot", "GasBot", "MentionBot", "TweetsBot", "WeatherBot"]
