"""Bot plugin system. Import all bot modules for auto-registration.

Phase 0 teardown (2026-07-24, PRD FR-0.1): the crypto bots (btc_15m,
btc_hourly, eth/sol/doge/xrp_15m) were deleted. The weather bot is the
only registered bot; it runs feed-only until the Phase 1-3 rebuild.
"""

from src.bots.weather_bot import WeatherBot

__all__ = ["WeatherBot"]
